"""Config flow for the HiMedia Player integration."""

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
import probatio as vol
import serial
import serial.tools.list_ports

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BAUDRATE,
    CONF_HOST,
    CONF_PORT,
    DEFAULT_BAUDRATE,
    DOMAIN,
    HTTP_DEVICE_INFO,
    HTTP_TIMEOUT,
    SUPPORTED_USB_VIDS,
)

_LOGGER = logging.getLogger(__name__)

NO_SERIAL = ""
NO_SERIAL_LABEL = "不使用串口（仅 IP 控制）"


class HiMediaPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HiMedia Player."""

    VERSION = 1

    def __init__(self) -> None:
        """初始化配置流程."""
        self._detected_port: str | None = None
        self._detected_info: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """选择自动识别串口还是手动配置."""
        if user_input is not None:
            if user_input.get("auto_detect"):
                return await self.async_step_auto_detect()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("auto_detect", default=True): bool}),
        )

    async def async_step_auto_detect(self, user_input: Any = None) -> ConfigFlowResult:
        """扫描串口并发送 #getDeviceInfo# 验证海美迪播放器."""
        ports = await self.hass.async_add_executor_job(
            serial.tools.list_ports.comports
        )
        candidates = [
            port.device for port in ports if self._is_likely_serial_device(port)
        ]
        _LOGGER.debug("待检测串口: %s", candidates)

        for port in candidates:
            info = await self.hass.async_add_executor_job(self._sync_probe_port, port)
            if info is None:
                continue
            _LOGGER.info("在 %s 上发现海美迪播放器", port)
            self._detected_port = port
            self._detected_info = info
            return await self.async_step_host()

        # 没找到串口设备时退回手动配置，仍可只填 IP
        return await self.async_step_manual()

    async def async_step_host(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """串口识别成功后，可选填写 IP 启用 HTTP 控制.

        HTTP 8899 不通时仍允许保存（IP 失败会在运行时自动降级到串口）.
        """
        assert self._detected_port is not None

        if user_input is not None:
            host = _normalize_host(user_input.get(CONF_HOST, ""))
            info = self._detected_info
            if host:
                http_info = await self._async_test_http(host)
                if http_info is not None:
                    info = http_info
                else:
                    _LOGGER.warning(
                        "IP %s 的 HiChannelTV 8899 端口不可达，"
                        "运行时将仅使用串口控制",
                        host,
                    )

            await self.async_set_unique_id(
                self._unique_id(info, self._detected_port, host)
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._build_title(info),
                data={
                    CONF_PORT: self._detected_port,
                    CONF_BAUDRATE: DEFAULT_BAUDRATE,
                    CONF_HOST: host,
                },
            )

        return self.async_show_form(
            step_id="host",
            data_schema=vol.Schema({vol.Optional(CONF_HOST, default=""): str}),
            description_placeholders={
                "name": self._build_title(self._detected_info)
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """手动选择串口/IP，二者至少配置一个.

        串口必须能成功打开；IP 可选，8899 不通时如串口可用则允许继续.
        """
        errors: dict[str, str] = {}
        warnings: dict[str, str] = {}
        ports = await self.hass.async_add_executor_job(
            serial.tools.list_ports.comports
        )
        port_options: dict[str, str] = {NO_SERIAL: NO_SERIAL_LABEL}
        port_options |= {
            port.device: f"{port.device} - {port.description}" for port in ports
        }

        if user_input is not None:
            port = user_input.get(CONF_PORT, NO_SERIAL)
            baudrate = user_input.get(CONF_BAUDRATE, DEFAULT_BAUDRATE)
            host = _normalize_host(user_input.get(CONF_HOST, ""))

            if not port and not host:
                errors["base"] = "no_connection"
            else:
                info: dict[str, Any] = {}
                serial_ok = False

                if port:
                    serial_info = await self.hass.async_add_executor_job(
                        self._sync_probe_port, port, baudrate
                    )
                    if serial_info is None:
                        # 部分固件不支持 #getDeviceInfo#，退而校验串口能否打开
                        try:
                            with serial.Serial(port, baudrate, timeout=0.5):
                                pass
                            serial_ok = True
                            info = {}
                        except (serial.SerialException, OSError, ValueError):
                            errors["base"] = "cannot_connect_serial"
                    else:
                        serial_ok = True
                        info = serial_info

                if host and not errors:
                    http_info = await self._async_test_http(host)
                    if http_info is None:
                        if serial_ok:
                            # IP 不通但串口可用 → 允许保存，打 warning
                            _LOGGER.warning(
                                "IP %s 的 HiChannelTV 8899 不可达，"
                                "运行时将仅使用串口",
                                host,
                            )
                            warnings["host"] = "http_unreachable_fallback_serial"
                        else:
                            # 只有 IP 且不通 → 拒绝
                            errors["base"] = "cannot_connect_http"
                    else:
                        info = http_info

            if not errors:
                await self.async_set_unique_id(
                    self._unique_id(info, port or None, host)
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._build_title(info),
                    data={
                        CONF_PORT: port,
                        CONF_BAUDRATE: baudrate,
                        CONF_HOST: host,
                    },
                )

        previous = user_input or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_PORT, default=previous.get(CONF_PORT, NO_SERIAL)
                    ): vol.In(port_options),
                    vol.Optional(
                        CONF_BAUDRATE,
                        default=previous.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
                    ): vol.In([9600, 19200, 38400, 57600, 115200]),
                    vol.Optional(
                        CONF_HOST, default=previous.get(CONF_HOST, "")
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "warnings": warnings.get("host", ""),
            },
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _build_title(self, info: dict[str, Any]) -> str:
        """根据设备信息构造 HA 中显示的标题."""
        return info.get("DeivcesName") or info.get("pid") or "HiMedia"

    def _unique_id(
        self, info: dict[str, Any], port: str | None, host: str
    ) -> str:
        """优先用 MAC 做唯一 ID，其次串口，最后 IP."""
        return info.get("mac") or port or host

    def _is_likely_serial_device(self, port: Any) -> bool:
        """排除蓝牙、调制解调器等无关串口."""
        exclude_keywords = ["Bluetooth", "Modem", "Fax", "Keyboard", "Mouse"]
        if any(keyword in port.description for keyword in exclude_keywords):
            return False
        if port.vid in SUPPORTED_USB_VIDS:
            return True
        if "USB" in port.description or "UART" in port.description:
            return True
        return port.device.startswith("/dev/ttyUSB") or port.device.startswith(
            "/dev/ttyS"
        )

    def _sync_probe_port(
        self, port: str, baudrate: int = DEFAULT_BAUDRATE
    ) -> dict[str, Any] | None:
        """发送 #getDeviceInfo# 验证串口上是否为海美迪播放器."""
        try:
            with serial.Serial(port, baudrate, timeout=1) as ser:
                time.sleep(0.2)
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                ser.write(b"#getDeviceInfo#")
                ser.flush()
                response = ser.read(512)
        except (serial.SerialException, OSError, ValueError):
            _LOGGER.debug("串口 %s 检测失败", port, exc_info=True)
            return None

        text = response.decode("ascii", errors="ignore").strip()
        _LOGGER.debug("串口 %s 返回: %s", port, text)
        if not text or text.startswith("ER"):
            return None
        try:
            data = json.loads(text[text.find("{") : text.rfind("}") + 1])
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def _async_test_http(self, host: str) -> dict[str, Any] | None:
        """请求 /get 验证 HiChannelTV 服务."""
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                async with async_get_clientsession(self.hass).get(
                    f"http://{host}{HTTP_DEVICE_INFO}"
                ) as response:
                    if response.status != 200:
                        return None
                    body = await response.text()
        except (TimeoutError, aiohttp.ClientError):
            _LOGGER.debug("HTTP 探测 %s 失败", host, exc_info=True)
            return None

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not data:
            return None
        if not data.get("mac") and not data.get("DeivcesName"):
            return None
        return data


def _normalize_host(value: str) -> str:
    """去掉协议前缀、路径与尾部斜杠."""
    host = value.strip().removesuffix("/")
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
    return host.split("/", 1)[0]
