"""Media Player platform for HiMedia Player."""

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HiMediaConfigEntry, HiMediaPlayer, build_device_info

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)

# 海报墙节点的 media_content_id 约定：根节点用 POSTER_ROOT，
# 单个影片用 "poster|<path>"，便于播放时回查原始海报数据
POSTER_ROOT = "posters"
POSTER_PREFIX = "poster|"

_SHARE_TYPE = {
    "smb": "SMB",
    "nfs": "NFS",
    "http": "HTTP",
    "https": "HTTP",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HiMediaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the HiMedia media player platform."""
    async_add_entities([HiMediaMediaPlayer(entry.entry_id, entry.runtime_data)])


class HiMediaMediaPlayer(MediaPlayerEntity):
    """Representation of a HiMedia player."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = True

    def __init__(self, entry_id: str, device: HiMediaPlayer) -> None:
        """Initialize the media player."""
        self._device = device
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_media_player"

        features = (
            MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.VOLUME_MUTE
        )
        if device.host is not None:
            features |= (
                MediaPlayerEntityFeature.PLAY_MEDIA
                | MediaPlayerEntityFeature.BROWSE_MEDIA
            )
        self._attr_supported_features = features

        # 海报墙缓存：media_content_id -> getPosterList 返回的原始海报数据
        self._posters: dict[str, dict[str, Any]] = {}

        self._attr_state = MediaPlayerState.IDLE
        self._attr_is_volume_muted = False
        self._attr_media_title = None
        self._attr_media_duration = None
        self._attr_media_position = None
        self._attr_media_position_updated_at = None

    @property
    def device_info(self):
        """返回设备注册信息."""
        return build_device_info(self._device, self._entry_id)

    @property
    def available(self) -> bool:
        """返回设备是否在线."""
        return self._device.available

    async def async_update(self) -> None:
        """轮询播放状态；IP 未配置时依赖命令后的状态推断."""
        if self._device.host is None:
            # 串口模式没有状态查询接口，只做连接探活
            await self.hass.async_add_executor_job(self._device.connect)
            return

        data = await self._device.async_poll_state()
        if data is None:
            return

        media = data.get("video") or {}
        if not media.get("filename"):
            media = data.get("audio") or {}

        filename = media.get("filename")
        if not filename:
            if self._attr_state in (
                MediaPlayerState.PLAYING,
                MediaPlayerState.PAUSED,
            ):
                self._attr_state = MediaPlayerState.IDLE
            self._attr_media_title = None
            self._attr_media_duration = None
            self._attr_media_position = None
            return

        self._attr_media_title = filename
        duration = media.get("duration") or 0
        position = media.get("playtime") or 0
        self._attr_media_duration = round(duration / 1000) if duration else None
        self._attr_media_position = round(position / 1000)
        self._attr_media_position_updated_at = dt_util.utcnow()
        self._attr_state = (
            MediaPlayerState.PLAYING
            if media.get("isPlaying")
            else MediaPlayerState.PAUSED
        )

    async def _send(self, name: str) -> bool:
        """下发命令，失败时抛出可读错误."""
        if not await self._device.async_send_command(name):
            if self._device.host and not self._device.port:
                raise HomeAssistantError(
                    f"发送命令 {name} 失败：当前仅配置了 IP，"
                    "且设备 8899 端口不可达；请确认 IP 或补充串口"
                )
            if self._device.port and not self._device.host:
                raise HomeAssistantError(
                    f"发送命令 {name} 失败：串口 {self._device.port} 无响应"
                )
            raise HomeAssistantError(
                f"发送命令 {name} 失败：HTTP 与串口均未响应"
            )
        return True

    async def async_turn_on(self) -> None:
        """Turn on the player."""
        await self._send("power_on")
        self._attr_state = MediaPlayerState.ON
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn off the player."""
        await self._send("power_off")
        self._attr_state = MediaPlayerState.OFF
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        """Send play command."""
        await self._send("play_video")
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        """Send pause command."""
        await self._send("pause_video")
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self._send("stop_video")
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self._send("previous_video")

    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self._send("next_video")

    async def async_volume_up(self) -> None:
        """Send volume up command."""
        await self._send("volume_up")

    async def async_volume_down(self) -> None:
        """Send volume down command."""
        await self._send("volume_down")

    async def async_mute_volume(self, mute: bool) -> None:
        """Protocol only supports mute; unmute is done by volume up."""
        await self._send("mute" if mute else "volume_up")
        self._attr_is_volume_muted = mute
        self.async_write_ha_state()

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """浏览设备上的海报墙（仅 IP 通道可用）."""
        if self._device.host is None:
            raise HomeAssistantError("浏览海报墙需要配置设备 IP 地址")

        if media_content_id == POSTER_ROOT:
            return await self._async_browse_posters()

        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id="root",
            media_content_type=MediaType.VIDEO,
            title=self.name or "海美迪",
            can_play=False,
            can_expand=True,
            children=[
                BrowseMedia(
                    media_class=MediaClass.DIRECTORY,
                    media_content_id=POSTER_ROOT,
                    media_content_type=MediaType.VIDEO,
                    title="海报墙",
                    can_play=False,
                    can_expand=True,
                )
            ],
        )

    async def _async_browse_posters(self) -> BrowseMedia:
        """读取 getPosterList 并转换成可播放的媒体条目."""
        posters = await self._device.async_get_poster_list()
        self._posters = {}
        children: list[BrowseMedia] = []
        for index, poster in enumerate(posters):
            key = str(poster.get("path") or f"item_{index}")
            media_id = f"{POSTER_PREFIX}{key}"
            self._posters[media_id] = poster
            title = str(poster.get("title") or poster.get("name") or key).strip()
            thumbnail = poster.get("extra1") or poster.get("extra2")
            if not isinstance(thumbnail, str) or not thumbnail.startswith("http"):
                thumbnail = None
            children.append(
                BrowseMedia(
                    media_class=MediaClass.VIDEO,
                    media_content_id=media_id,
                    media_content_type=MediaType.VIDEO,
                    title=title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumbnail,
                )
            )

        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id=POSTER_ROOT,
            media_content_type=MediaType.VIDEO,
            title=f"海报墙（{len(children)}）",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _async_play_poster(self, media_id: str) -> None:
        """把海报原始数据原样回传给设备以开始播放."""
        poster = self._posters.get(media_id)
        if poster is None:
            await self._async_browse_posters()
            poster = self._posters.get(media_id)
        if poster is None:
            raise HomeAssistantError(f"海报墙里找不到该影片: {media_id}")
        if not await self._device.async_play_poster(poster):
            raise HomeAssistantError(
                f"播放失败: {poster.get('title') or media_id}"
            )
        self._attr_state = MediaPlayerState.PLAYING
        self._attr_media_title = str(poster.get("title") or "")
        self._attr_media_content_id = media_id
        self._attr_media_content_type = MediaType.VIDEO
        self.async_write_ha_state()

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: object
    ) -> None:
        """播放 SMB/NFS/HTTP 媒体、本地音乐或海报墙里的影片."""
        if self._device.host is None:
            raise HomeAssistantError("播放媒体需要配置设备 IP 地址")

        if media_id.startswith(POSTER_PREFIX):
            await self._async_play_poster(media_id)
            return

        if media_type in (MediaType.MUSIC, MediaType.TRACK, "audio", "music"):
            if not await self._device.async_broadcast_music(media_id):
                raise HomeAssistantError(f"播放音乐失败: {media_id}")
        else:
            scheme = media_id.split("://", 1)[0].lower() if "://" in media_id else ""
            share_type = _SHARE_TYPE.get(scheme, scheme.upper() or "LOCAL")
            payload = {
                "url": media_id,
                "type": share_type,
                "machineLinkString": media_id.rsplit("/", 1)[0]
                if "/" in media_id
                else media_id,
            }
            if not await self._device.async_play_poster(payload):
                raise HomeAssistantError(f"播放媒体失败: {media_id}")

        self._attr_state = MediaPlayerState.PLAYING
        self._attr_media_content_id = media_id
        self._attr_media_content_type = media_type
        self.async_write_ha_state()
