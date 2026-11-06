"""协议常量测试：确认键值表与官方文档一致。."""

from custom_components.himedia_player.const import (
    KEY_MAP,
    NAMED_COMMANDS,
    SERIAL_EVENT_STATE,
)


def test_navigation_key_codes() -> None:
    """遥控键值需与《海美迪智能控制协议 8K-2.1》一致。."""
    assert KEY_MAP["up"] == 19
    assert KEY_MAP["down"] == 20
    assert KEY_MAP["left"] == 21
    assert KEY_MAP["right"] == 22
    assert KEY_MAP["ok"] == 23
    assert KEY_MAP["home"] == 3
    assert KEY_MAP["back"] == 4
    assert KEY_MAP["menu"] == 82


def test_power_and_volume_key_codes() -> None:
    """电源、音量与静音键值。."""
    assert KEY_MAP["power"] == 26
    assert KEY_MAP["standby"] == 1030
    assert KEY_MAP["power_on"] == 1031
    assert KEY_MAP["volume_up"] == 24
    assert KEY_MAP["volume_down"] == 25
    assert KEY_MAP["mute"] == 164

    # 长按使用专用键值，放在复合命令表中
    assert NAMED_COMMANDS["hold_right"][0] == "#1034#"
    assert NAMED_COMMANDS["hold_left"][0] == "#1035#"
    assert NAMED_COMMANDS["hold_stop"][0] == "#1036#"


def test_digit_and_letter_key_codes() -> None:
    """数字 0-9 为 7-16，字母 A-Z 为 29-54。."""
    for digit, code in enumerate([7, 8, 9, 10, 11, 12, 13, 14, 15, 16]):
        assert KEY_MAP[f"digit_{digit}"] == code
    assert KEY_MAP["letter_a"] == 29
    assert KEY_MAP["letter_z"] == 54


def test_named_command_pairs() -> None:
    """复合命令需同时覆盖串口与 HTTP。."""
    serial_cmd, http_path = NAMED_COMMANDS["play_video"]
    assert serial_cmd == "#playVideo#"
    assert http_path == "/playVideo"

    serial_cmd, http_path = NAMED_COMMANDS["next_audio"]
    assert serial_cmd == "#nextAudio#"
    assert http_path == "/nextAudioo"  # 官方文档写法

    serial_cmd, http_path = NAMED_COMMANDS["power_off"]
    assert serial_cmd == "#26#"
    assert http_path == "/poweroff"


def test_command_names_do_not_collide() -> None:
    """键值命令与复合命令不能重名，否则选择器会重复。."""
    assert not set(KEY_MAP) & set(NAMED_COMMANDS)


def test_device_report_events() -> None:
    """设备主动上报事件应覆盖播放与开关机。."""
    assert SERIAL_EVENT_STATE["#videoPlay#"] == "playing"
    assert SERIAL_EVENT_STATE["#videoPause#"] == "paused"
    assert SERIAL_EVENT_STATE["#poweroff#"] == "off"
