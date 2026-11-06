"""The HiMedia Player integration."""

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
import serial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
    HTTP_DEVICE_INFO,
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
        self._session = async_get_clientsession(hass)
        self._serial_connection: serial.Serial | None = None
        self._available_ip = False
        self._available_serial = False
        # HTTP 故障退避：monotonic 时间戳，早于该时间的请求直接跳过
        self._ip_down_until = 0.0

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
        """拼接设备 HTTP 地址."""
        return f"http://{self._host}{path}"

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
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                if method == "POST":
                    request = self._session.post(self._url(path), json=payload)
                else:
                    request = self._session.get(self._url(path))
                async with request as response:
                    status = response.status
                    body = await response.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            self._mark_ip_down()
            _LOGGER.debug("HTTP %s 失败: %r", path, err)
            return None

        if status != 200:
            self._mark_ip_down()
            _LOGGER.debug("HTTP %s 返回状态码 %s", path, status)
            return None

        self._available_ip = True
        self._ip_down_until = 0.0
        text = body.strip()
        if not text:
            return ""
        return _parse_json_object(text) or text

    def _mark_ip_down(self) -> None:
        """标记 IP 通道进入退避期."""
        was_available = self._available_ip
        self._available_ip = False
        self._ip_down_until = time.monotonic() + HTTP_RETRY_INTERVAL
        if was_available:
            _LOGGER.warning(
                "HiChannelTV HTTP 不可达（%s:%s），%s 秒内改走串口，之后自动重试 IP",
                self._host,
                DEFAULT_HTTP_PORT,
                HTTP_RETRY_INTERVAL,
            )

    async def async_get_device_info(self) -> dict[str, Any] | None:
        """读取设备信息（型号、版本、MAC 等）."""
        data = await self.async_http(HTTP_DEVICE_INFO)
        return data if isinstance(data, dict) else None

    async def async_play_poster(self, payload: dict[str, Any]) -> bool:
        """通过 /playPoster 播放海报/影片（支持 smb、nfs 路径）."""
        result = await self.async_http(HTTP_PLAY_POSTER, method="POST", payload=payload)
        return result is not None

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
                "HiChannelTV 8899 端口不可达（%s），尝试串口降级", self._host
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
