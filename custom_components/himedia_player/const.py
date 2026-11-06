"""Constants for the HiMedia Player integration.

协议来源：《海美迪智能控制协议 8K-2.1》
- HiChannelTV HTTP 控制：http://<ip>:8899
- RS-232 控制：115200 8N1，命令格式 #<Android 标准键值>#
"""

DOMAIN = "himedia_player"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_BAUDRATE = "baudrate"

DEFAULT_BAUDRATE = 115200
DEFAULT_HTTP_PORT = 8899
HTTP_TIMEOUT = 5
SERIAL_TIMEOUT = 1

# ---------------------------------------------------------------------------
# HiChannelTV HTTP 接口（端口 8899）
# ---------------------------------------------------------------------------
HTTP_DEVICE_INFO = "/get"
HTTP_PLAYER_INFO = "/getPlayerInfo"
HTTP_AUDIO_PLAYER_INFO = "/getAudioPlayerInfo"
HTTP_PLAY_POSTER = "/playPoster"
HTTP_BROADCAST_MUSIC = "/broadCastMusic"
HTTP_GET_POSTER_LIST = "/getPosterList"
HTTP_SEND_KEY = "/send?key={key}"

# ---------------------------------------------------------------------------
# Android 标准键值（IP: /send?key=xx，RS-232: #xx#）
# ---------------------------------------------------------------------------
KEY_MAP: dict[str, int] = {
    # 电源与系统
    "power": 26,
    "standby": 1030,
    "power_on": 1031,
    "home": 3,
    "back": 4,
    "menu": 82,
    "clear": 67,
    # 导航
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "ok": 23,
    # 音量
    "volume_up": 24,
    "volume_down": 25,
    "mute": 164,
    # 播放控制
    "play_pause": 85,
    "play": 126,
    "pause": 127,
    "stop": 86,
    "fast_forward": 90,
    "rewind": 89,
    "previous": 92,
    "next": 93,
    # 显示与音轨
    "subtitle": 14,
    "audio_track": 16,
    "loop": 1016,
    "output": 278,
    # 彩色键
    "red": 297,
    "green": 298,
    "yellow": 299,
    "blue": 300,
    # 数字键
    "digit_0": 7,
    "digit_1": 8,
    "digit_2": 9,
    "digit_3": 10,
    "digit_4": 11,
    "digit_5": 12,
    "digit_6": 13,
    "digit_7": 14,
    "digit_8": 15,
    "digit_9": 16,
}

# 字母键 A-Z：29-54
KEY_MAP |= {f"letter_{chr(ord('a') + i)}": 29 + i for i in range(26)}

# ---------------------------------------------------------------------------
# 复合命令：name -> (RS-232 命令, HTTP 路径)
# ---------------------------------------------------------------------------
NAMED_COMMANDS: dict[str, tuple[str | None, str | None]] = {
    # 本地视频
    "play_video": ("#playVideo#", "/playVideo"),
    "pause_video": ("#pauseVideo#", "/pauseVideo"),
    "stop_video": ("#stopVideo#", "/stopVideo"),
    "previous_video": ("#preVideo#", "/preVideo"),
    "next_video": ("#nextVideo#", "/nextVideo"),
    # 本地音乐（文档中的下一首接口为 nextAudioo，按原样实现）
    "play_audio": ("#playAudio#", "/playAudio"),
    "pause_audio": ("#pauseAudio#", "/pauseAudio"),
    "stop_audio": ("#stopAudio#", "/stopAudio"),
    "previous_audio": ("#preAudio#", "/preAudio"),
    "next_audio": ("#nextAudio#", "/nextAudioo"),
    # 播放中切换
    "switch_audio": (None, "/switchAudio"),
    "switch_subtitle": (None, "/switchSub"),
    # 电源与系统
    "power_off": ("#26#", "/poweroff"),
    "restart": ("RST", None),
    # 长按（232 使用专用键值，IP 使用 longdown/longup）
    "hold_left": ("#1035#", "/send?longdown=21"),
    "hold_right": ("#1034#", "/send?longdown=22"),
    "hold_stop": ("#1036#", "/send?longup=21&t=22"),
}

# 设备主动上报（仅 RS-232）
SERIAL_EVENT_STATE: dict[str, str] = {
    "#videoPlay#": "playing",
    "#videoPause#": "paused",
    "#videoStop#": "idle",
    "#audioPlay#": "playing",
    "#audioPause#": "paused",
    "#audioStop#": "idle",
    "#poweron#": "on",
    "#poweroff#": "off",
}
