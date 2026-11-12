"""The HiMedia Player integration."""

import asyncio
import json
import logging
import socket
import time
from typing import Any

import serial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo

from .const import (
    CONF_BAUDRATE,
    CONF_HOST,
    CONF_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_HTTP_PORT,
    DOMAIN,
    HTTP_AUDIO_PLAYER_INFO,
    HTTP_BROADCAST_MUSIC,
    HTTP_CONNECT_TIMEOUT,
    HTTP_DEVICE_INFO,
    HTTP_GET_POSTER_LIST,
    HTTP_PLAY_POSTER,
    HTTP_PLAYER_INFO,
    HTTP_RETRY_INTERVAL,
    HTTP_SEND_KEY,
    HTTP_TIMEOUT,
    KEY_MAP,
    NAMED_COMMANDS,
    SERIAL_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

_PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER, Platform.SELECT]

HiMediaConfigEntry = ConfigEntry["HiMediaPlayer"]


class HiMediaPlayer:
    """海美迪播放器 API，同时封装 RS-232 串口与 HiChannelTV HTTP 两种通路."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str | None,
        port: str | None,
        baudrate: int,
    ) -> None:
        """初始化播放器实例；host 与 port 至少配置一个."""
        self._hass = hass
        self._host = host or None
        self._port = port or None
        self._baudrate = baudrate
        self._serial_connection: serial.Serial | None = None
        self._available_ip = False
        self._available_serial = False
        # HTTP 故障退避：monotonic 时间戳，早于该时间的请求直接跳过
        self._ip_down_until = 0.0
        # 最近一次 HTTP 失败的原因（给 setup 降级日志与警告用）
        self._last_ip_error: str | None = None
        # 当前故障周期是否已在警告级别提示过
        self._ip_warned = False
        # 连续失败的周期数：首个周期告警，后续降为 INFO 降低噪音
        self._ip_failure_cycles = 0
        # 设备 HTTP 服务只撑得住单连接，所有请求串行下发
        self._http_lock = asyncio.Lock()

        self.mac_address: str | None = None
        self.model: str | None = None
        self.device_name: str | None = None
        self.sw_version: str | None = None

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def host(self) -> str | None:
        """返回配置的 IP 地址."""
        return self._host

    @property
    def port(self) -> str | None:
        """返回配置的串口."""
        return self._port

    @property
    def available(self) -> bool:
        """任意一条已配置的通路可用即视为在线."""
        if self._host is not None and self._available_ip:
            return True
        if self._port is not None and self._available_serial:
            return True
        return False

    # ------------------------------------------------------------------
    # RS-232
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """建立或探活串口连接."""
        if self._port is None:
            return False

        if self._serial_connection is not None and self._serial_connection.is_open:
            try:
                # 访问 in_waiting 触发实际探活，串口已掉线会抛异常
                _ = self._serial_connection.in_waiting
            except (serial.SerialException, OSError) as err:
                _LOGGER.debug("串口已断开: %s", err)
                self._close_serial()
            else:
                self._available_serial = True
                return True

        try:
            self._serial_connection = serial.Serial(
                self._port, self._baudrate, timeout=SERIAL_TIMEOUT
            )
        except (serial.SerialException, ValueError) as err:
            self._serial_connection = None
            self._available_serial = False
            _LOGGER.debug("无法打开串口 %s: %s", self._port, err)
            return False

        self._available_serial = True
        _LOGGER.info("已连接串口 %s (%s bps)", self._port, self._baudrate)
        return True

    def _sync_send(self, command: str) -> str:
        """发送一条 RS-232 命令并同步读取返回值."""
        if not self.connect() or self._serial_connection is None:
            return ""
        try:
            self._serial_connection.reset_input_buffer()
            self._serial_connection.write(command.encode("ascii"))
            self._serial_connection.flush()
            response = self._serial_connection.read(512)
        except (serial.SerialException, OSError) as err:
            _LOGGER.debug("串口发送 %s 失败: %s", command, err)
            self._available_serial = False
            self._close_serial()
            return ""
        text = response.decode("ascii", errors="ignore").strip()
        _LOGGER.debug("串口发送 %s -> %s", command, text)
        return text

    def send_serial(self, command: str) -> bool:
        """发送 RS-232 命令；设备返回 OK 代表成功，ER 代表失败."""
        response = self._sync_send(command)
        if not response:
            return False
        return not response.startswith("ER")

    def _sync_get_device_info_serial(self) -> dict[str, Any] | None:
        """通过 #getDeviceInfo# 读取设备 JSON 信息."""
        response = self._sync_send("#getDeviceInfo#")
        if not response:
            return None
        return _parse_json_object(response)

    def _close_serial(self) -> None:
        """关闭串口连接."""
        if self._serial_connection is not None and self._serial_connection.is_open:
            try:
                self._serial_connection.close()
            except OSError as err:
                _LOGGER.debug("关闭串口失败: %s", err)
        self._serial_connection = None

    # ------------------------------------------------------------------
    # HTTP（HiChannelTV，默认 8899）
    # ------------------------------------------------------------------
    def _url(self, path: str) -> str:
        """拼接设备 HTTP 地址；host 已带端口时原样使用，否则补 8899."""
        host, port = self._host_port()
        return f"http://{host}:{port}{path}"

    def _host_port(self) -> tuple[str, int]:
        """解析配置中的 host 与端口."""
        host = self._host or ""
        if ":" in host:
            host_part, _, port_part = host.rpartition(":")
            return host_part, int(port_part)
        return host, DEFAULT_HTTP_PORT

    def _sync_http_request(
        self, path: str, method: str, payload: dict[str, Any] | None
    ) -> tuple[int, str]:
        """用裸 socket 发送 HTTP 请求，行为与 curl 一致.

        分阶段设置超时，便于从错误信息区分「TCP 连不上」与
        「已连接但设备不返回数据」两类问题.
        """
        host, port = self._host_port()
        body = json.dumps(payload).encode() if payload is not None else b""
        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Connection: close",
            "Accept-Encoding: identity",
            "User-Agent: HomeAssistant-HiMediaPlayer",
        ]
        if body:
            lines.append("Content-Type: application/json")
            lines.append(f"Content-Length: {len(body)}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode() + body

        try:
            sock = socket.create_connection(
                (host, port), timeout=HTTP_CONNECT_TIMEOUT
            )
        except TimeoutError as err:
            raise OSError(
                f"TCP 连接超时：{host}:{port} 不可达（HA 与设备不在同一网络或被防火墙拦截）"
            ) from err
        except OSError as err:
            raise OSError(f"TCP 连接失败：{host}:{port}（{err}）") from err

        try:
            sock.settimeout(HTTP_TIMEOUT)
            sock.sendall(request)
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            header_bytes, _, body_bytes = buffer.partition(b"\r\n\r\n")
            header_text = header_bytes.decode("iso-8859-1", "ignore")
            headers: dict[str, str] = {}
            for line in header_text.split("\r\n")[1:]:
                key, _, value = line.partition(":")
                if key:
                    headers[key.strip().lower()] = value.strip()

            if headers.get("transfer-encoding", "").lower() == "chunked":
                while b"\r\n0\r\n\r\n" not in body_bytes:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    body_bytes += chunk
            elif "content-length" in headers:
                # 设备不会主动断开连接（HTTP/1.1 keep-alive），
                # 必须按 Content-Length 精确读取，不能等 EOF
                remaining = int(headers["content-length"]) - len(body_bytes)
                while remaining > 0:
                    chunk = sock.recv(min(4096, remaining))
                    if not chunk:
                        break
                    body_bytes += chunk
                    remaining -= len(chunk)
            else:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    body_bytes += chunk
        except TimeoutError as err:
            raise OSError(
                f"TCP 已连接但设备 {HTTP_TIMEOUT} 秒内无响应：{host}:{port}"
            ) from err
        finally:
            sock.close()

        status_line = header_text.split("\r\n", 1)[0]
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError) as err:
            raise OSError(f"设备返回非法响应：{header_text[:120]!r}") from err
        body_text = body_bytes.decode("utf-8", "ignore")
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body_text = _decode_chunked(body_text)
        return status, body_text

    async def async_http(
        self, path: str, method: str = "GET", payload: Any | None = None
    ) -> Any | None:
        """请求设备 HTTP 接口；成功返回解析后的 JSON 或纯文本.

        失败后进入 60 秒退避：期间所有请求直接跳过（立即返回 None），
        避免每次轮询/按键都干等超时，拖慢整个实体更新.
        """
        if self._host is None:
            return None
        if time.monotonic() < self._ip_down_until:
            return None
        # 设备 HTTP 服务只撑得住单连接，并发请求会互相挤占导致全部超时，
        # 因此所有请求串行下发
        async with self._http_lock:
            if time.monotonic() < self._ip_down_until:
                return None
            started = time.monotonic()
            try:
                status, body = await self._hass.async_add_executor_job(
                    self._sync_http_request, path, method, payload
                )
            except (OSError, ValueError) as err:
                self._last_ip_error = f"{type(err).__name__}: {err}"
                self._mark_ip_down(err)
                _LOGGER.debug("%s 请求失败: %s: %s", self._url(path), type(err).__name__, err)
                return None

            elapsed_ms = int((time.monotonic() - started) * 1000)
            _LOGGER.debug("%s -> HTTP %s，用时 %d ms", self._url(path), status, elapsed_ms)

            if status != 200:
                self._last_ip_error = (
                    f"HTTP {status}（响应前 120 字符: {body[:120]!r}）"
                )
                self._mark_ip_down()
                return None

            self._available_ip = True
            self._ip_down_until = 0.0
            self._last_ip_error = None
            self._ip_warned = False
            self._ip_failure_cycles = 0
            text = body.strip()
            if not text:
                return ""
            return _parse_json_object(text) or text

    def _mark_ip_down(self, err: Exception | None = None) -> None:
        """标记 IP 通道进入退避期；每个故障周期在警告级别提示一次原因."""
        self._available_ip = False
        self._ip_down_until = time.monotonic() + HTTP_RETRY_INTERVAL
        if not self._ip_warned:
            self._ip_warned = True
            detail = self._last_ip_error or (
                f"{type(err).__name__}: {err}" if err else "status 非 200"
            )
            host, port = self._host_port()
            message = (
                "HiChannelTV HTTP 不可达（%s:%s）[%s]（设备可能已关机），"
                "%s 秒内改走串口，之后自动重试 IP"
            )
            log = (
                _LOGGER.warning
                if self._ip_failure_cycles == 0
                else _LOGGER.info
            )
            log(
                message,
                host,
                port,
                detail,
                HTTP_RETRY_INTERVAL,
            )
        self._ip_failure_cycles += 1

    async def async_get_device_info(self) -> dict[str, Any] | None:
        """读取设备信息（型号、版本、MAC 等）."""
        data = await self.async_http(HTTP_DEVICE_INFO)
        return data if isinstance(data, dict) else None

    async def async_play_poster(self, payload: dict[str, Any]) -> bool:
        """通过 /playPoster 播放海报/影片（支持 smb、nfs 路径）."""
        result = await self.async_http(HTTP_PLAY_POSTER, method="POST", payload=payload)
        return result is not None

    async def async_get_poster_list(self) -> list[dict[str, Any]]:
        """读取海报墙数据（/getPosterList）；失败或格式异常时返回空列表."""
        data = await self.async_http(HTTP_GET_POSTER_LIST)
        if not isinstance(data, dict):
            return []
        posters = data.get("poster")
        if not isinstance(posters, list):
            return []
        return [item for item in posters if isinstance(item, dict)]

    async def async_broadcast_music(self, path: str) -> bool:
        """通过 /broadCastMusic 播放本地/网络音乐."""
        result = await self.async_http(
            HTTP_BROADCAST_MUSIC, method="POST", payload={"posterPicString": path}
        )
        return result is not None

    # ------------------------------------------------------------------
    # 命令下发
    # ------------------------------------------------------------------
    async def async_send_command(self, name: str) -> bool:
        """按命令名下发指令；优先 IP，失败或未配置时降级到串口."""
        key = KEY_MAP.get(name)
        if key is not None:
            if self._host is not None:
                result = await self.async_http(HTTP_SEND_KEY.format(key=key))
                if result is not None:
                    return True
            if self._port is not None:
                return await self._hass.async_add_executor_job(
                    self.send_serial, f"#{key}#"
                )
            return False

        named = NAMED_COMMANDS.get(name)
        if named is None:
            _LOGGER.debug("未知命令: %s", name)
            return False

        serial_command, http_path = named
        if self._host is not None and http_path is not None:
            result = await self.async_http(http_path)
            if result is not None:
                return True
        if self._port is not None and serial_command is not None:
            return await self._hass.async_add_executor_job(
                self.send_serial, serial_command
            )
        return False

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    async def async_poll_state(self) -> dict[str, Any] | None:
        """轮询播放状态（仅 IP 通路可用）."""
        if self._host is None:
            return None

        video = await self.async_http(HTTP_PLAYER_INFO)
        video = video if isinstance(video, dict) else None

        audio = None
        if video is None or not video.get("filename"):
            data = await self.async_http(HTTP_AUDIO_PLAYER_INFO)
            audio = data if isinstance(data, dict) else None

        device = None
        if audio is None or not audio.get("filename"):
            device = await self.async_get_device_info()
            if device is not None:
                self._apply_device_info(device)

        return {"video": video, "audio": audio, "device": device}

    async def async_connect(self) -> bool:
        """启动时建立连接并读取设备信息."""
        if self._host is not None:
            info = await self.async_get_device_info()
            if info is not None:
                self._apply_device_info(info)
                _LOGGER.info(
                    "HiChannelTV HTTP 控制已就绪（%s）", self._host
                )
                return True
            _LOGGER.warning(
                "HiChannelTV 8899 端口不可达（%s）[%s]（设备可能已关机），尝试串口降级",
                self._host,
                self._last_ip_error or "未知原因",
            )

        if self._port is not None:
            info = await self._hass.async_add_executor_job(
                self._sync_get_device_info_serial
            )
            if info is not None:
                self._available_serial = True
                self._apply_device_info(info)
                _LOGGER.info("RS-232 控制已就绪（%s）", self._port)
                return True
            # 部分固件不支持 #getDeviceInfo#，串口能打开即视为可用
            if await self._hass.async_add_executor_job(self.connect):
                _LOGGER.info(
                    "RS-232 已连接（%s），设备信息读取不可用但控制命令可下发",
                    self._port,
                )
                return True

        return False

    def _apply_device_info(self, data: dict[str, Any]) -> None:
        """把设备返回的信息写入实例属性."""
        self.mac_address = data.get("mac") or self.mac_address
        self.model = data.get("DeivcesName") or data.get("pid") or self.model
        self.device_name = data.get("DeivcesName") or self.device_name
        version = data.get("version")
        self.sw_version = str(version) if version is not None else self.sw_version

    def close(self) -> None:
        """释放串口资源."""
        self._close_serial()


def _decode_chunked(body: str) -> str:
    """解码 HTTP 分块传输响应体."""
    parts: list[str] = []
    while body:
        line, sep, rest = body.partition("\r\n")
        if not sep:
            break
        try:
            size = int(line.split(";", 1)[0].strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        parts.append(rest[:size])
        body = rest[size + 2 :]
    return "".join(parts)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """从返回文本中截取并解析 JSON 对象."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def build_device_info(device: HiMediaPlayer, entry_id: str) -> DeviceInfo:
    """构造设备注册信息，供各平台实体复用."""
    name = device.device_name or device.model or "HiMedia Player"
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        connections={(CONNECTION_NETWORK_MAC, device.mac_address)}
        if device.mac_address
        else set(),
        name=name,
        manufacturer="HiMedia",
        model=device.model,
        sw_version=device.sw_version,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: HiMediaConfigEntry
) -> bool:
    """Set up HiMedia Player from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    player = HiMediaPlayer(
        hass,
        host=entry.data.get(CONF_HOST) or None,
        port=entry.data.get(CONF_PORT) or None,
        baudrate=entry.data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
    )

    # 任一通路可用即可启动；都失败时让 HA 自动重试
    if not await player.async_connect():
        await hass.async_add_executor_job(player.close)
        raise ConfigEntryNotReady(
            "无法连接海美迪播放器，请检查 IP 或串口连接"
        )

    entry.runtime_data = player
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HiMediaConfigEntry
) -> bool:
    """Unload a config entry."""
    player = entry.runtime_data
    await hass.async_add_executor_job(player.close)
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
