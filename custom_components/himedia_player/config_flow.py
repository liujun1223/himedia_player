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
)

_LOGGER = logging.getLogger(__name__)

NO_SERIAL = ""
NO_SERIAL_LABEL = "不使用串口（仅 IP 控制）"


class HiMediaPlayerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HiMedia Player."""

    VERSION = 1

    async def async_step_user(
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
                        except (serial.SerialException, OSError, ValueError):
                            errors["base"] = "cannot_connect_serial"
                    else:
                        serial_ok = True
                        info = serial_info

                if host and not errors:
                    http_info = await self._async_test_http(host)
                    if http_info is None:
                        if serial_ok:
                            _LOGGER.warning(
                                "IP %s 的 HiChannelTV 8899 不可达，"
                                "运行时将仅使用串口",
                                host,
                            )
                        else:
                            errors["base"] = "cannot_connect_http"
                    else:
                        info = http_info

            if not errors:
                unique_id = info.get("mac") or port or host
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                title = info.get("DeivcesName") or info.get("pid") or "HiMedia"
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_PORT: port,
                        CONF_BAUDRATE: baudrate,
                        CONF_HOST: host,
                    },
                )

        previous = user_input or {}
        return self.async_show_form(
            step_id="user",
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
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
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
