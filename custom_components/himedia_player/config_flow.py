"""Config flow for the HiMedia Player integration."""

import json
import logging
import time
from typing import Any

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
        """初始化配置流程，记录串口探测结果."""
        super().__init__()
        self._detected_info: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """入口：直接进入手动配置，由用户自己选择串口.

        这里刻意不做串口自动扫描：FTDI / CH340 等 USB 转串口芯片同样被
        Zigbee、Z-Wave 等集成使用，未经用户选择就打开端口、清空输入缓冲
        并写入探测字节会破坏其它无线电的通信。只探测用户在表单里选中的
        那一个端口。
        """
        return await self.async_step_manual()

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """手动配置：串口、波特率、IP 至少填一个."""
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
            elif port and self._port_in_use(port):
                # 端口已被其它 config entry 占用，直接报错，不再去打开它
                errors["base"] = "port_in_use"
            elif port:
                serial_ok = await self.hass.async_add_executor_job(
                    self._sync_probe_port, port, baudrate
                )
                if serial_ok is None:
                    # 部分固件不支持 #getDeviceInfo#，退而校验串口能否打开
                    try:
                        await self.hass.async_add_executor_job(
                            _try_open_serial, port, baudrate
                        )
                    except (serial.SerialException, OSError, ValueError):
                        errors["base"] = "cannot_connect_serial"

            if not errors:
                info = self._detected_info if port else {}
                await self.async_set_unique_id(
                    info.get("mac") or port or host
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
        # 全部用 Optional：避免前端把空值当必填拦截；
        # IP 不回填 previous，避免用户清空后被自动回填
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
                    ): vol.In(BAUDRATE_OPTIONS),
                    vol.Optional(CONF_HOST, default=""): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _port_in_use(self, port: str) -> bool:
        """串口是否已被其它已配置的条目占用."""
        return any(
            entry.data.get(CONF_PORT) == port
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

    def _build_title(self, info: dict[str, Any]) -> str:
        """根据设备信息构造 HA 中显示的标题."""
        return info.get("DeivcesName") or info.get("pid") or "HiMedia"

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
        if isinstance(data, dict):
            self._detected_info = data
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
