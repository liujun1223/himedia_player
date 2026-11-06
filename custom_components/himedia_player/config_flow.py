"""Config flow for the HiMedia Player integration."""

import json
import logging
import time

import serial
import serial.tools.list_ports
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from .const import CONF_BAUDRATE, CONF_HOST, CONF_PORT, DEFAULT_BAUDRATE, DOMAIN

_LOGGER = logging.getLogger(__name__)

NO_SERIAL = ""
NO_SERIAL_LABEL = "不使用串口（仅 IP 控制）"

BAUDRATE_OPTIONS = [9600, 19200, 38400, 57600, 115200]


class HiMediaPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HiMedia Player."""

    VERSION = 1

    def __init__(self) -> None:
        """初始化配置流程，缓存串口探测到的设备信息."""
        super().__init__()
        self._last_serial_info: dict = {}

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """手动配置：串口、波特率、IP 至少填一个.

        IP 不做预验证——浏览器能通就说明设备侧没问题，
        HA 侧的网络差异不该在这里堵用户.
        """
        errors: dict[str, str] = {}
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
            # 串口必须能打开；IP 不预验证
            elif port:
                serial_ok = await self.hass.async_add_executor_job(
                    self._sync_probe_port, port, baudrate
                )
                if serial_ok is None:
                    try:
                        await self.hass.async_add_executor_job(
                            _try_open_serial, port, baudrate
                        )
                    except (serial.SerialException, OSError, ValueError):
                        errors["base"] = "cannot_connect_serial"

            if not errors:
                info = self._last_serial_info if port else {}
                if port:
                    unique_id = info.get("mac") or port or host
                    title = (
                        info.get("DeivcesName")
                        or info.get("pid")
                        or "HiMedia"
                    )
                else:
                    unique_id = host
                    title = "HiMedia"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_PORT: port,
                        CONF_BAUDRATE: baudrate,
                        CONF_HOST: host,
                    },
                )

        previous = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PORT, default=previous.get(CONF_PORT, NO_SERIAL)
                ): vol.In(port_options),
                vol.Required(
                    CONF_BAUDRATE,
                    default=previous.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
                ): vol.In(BAUDRATE_OPTIONS),
                # IP 不回填 previous，避免清空后被自动回填
                vol.Optional(CONF_HOST, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    def _sync_probe_port(
        self, port: str, baudrate: int = DEFAULT_BAUDRATE
    ) -> dict | None:
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
        if isinstance(data, dict):
            self._last_serial_info = data
            return data
        return None


def _try_open_serial(port: str, baudrate: int) -> None:
    """尝试打开串口；失败时抛异常."""
    with serial.Serial(port, baudrate, timeout=0.5):
        pass


def _normalize_host(value: str) -> str:
    """去掉协议前缀、路径与尾部斜杠."""
    host = value.strip().removesuffix("/")
    for prefix in ("http://", "https://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix) :]
    return host.split("/", 1)[0]
