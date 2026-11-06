"""Select platform for HiMedia Player."""

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HiMediaConfigEntry, HiMediaPlayer, build_device_info
from .const import KEY_MAP, NAMED_COMMANDS

_LOGGER = logging.getLogger(__name__)
PLACEHOLDER = "-选择命令-"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HiMediaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """创建遥控命令选择器."""
    async_add_entities([HiMediaCommandSelect(entry.entry_id, entry.runtime_data)])


class HiMediaCommandSelect(SelectEntity):
    """遥控命令选择器，选中后立即通过 IP 或串口下发."""

    _attr_has_entity_name = True
    _attr_name = "Remote"
    _attr_should_poll = False

    def __init__(self, entry_id: str, device: HiMediaPlayer) -> None:
        """初始化实体."""
        self._device = device
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_command"
        self._attr_options = [
            PLACEHOLDER,
            *KEY_MAP.keys(),
            *NAMED_COMMANDS.keys(),
        ]
        self._attr_current_option = PLACEHOLDER
        self._attr_icon = "mdi:remote"

    @property
    def device_info(self):
        """返回设备注册信息."""
        return build_device_info(self._device, self._entry_id)

    @property
    def available(self) -> bool:
        """设备是否在线."""
        return self._device.available

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """返回支持的命令数量."""
        return {"supported_commands": len(KEY_MAP) + len(NAMED_COMMANDS)}

    async def async_select_option(self, option: str) -> None:
        """用户选中命令后立即下发."""
        if option == PLACEHOLDER:
            return

        if await self._device.async_send_command(option):
            _LOGGER.info("命令已执行: %s", option)
        else:
            _LOGGER.warning("命令执行失败: %s", option)

        self._attr_current_option = PLACEHOLDER
        self.async_write_ha_state()
