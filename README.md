# HiMedia Player

海美迪（HiMedia）播放器的 Home Assistant 自定义集成，依据官方文档《海美迪智能控制协议 8K-2.1》实现，**同时支持 RS-232 串口与 IP（HiChannelTV，8899 端口）两种控制通道**。

## 功能

- 双通道控制：可只配串口、只配 IP，或两者同时配置（优先走 IP，失败自动降级到串口）
- 电源开关、播放/暂停/停止、上一部/下一部、音量加减与静音
- 完整遥控键值：方向键、OK、主页、返回、菜单、数字键、字母键、彩色键、字幕/音轨/循环、长按左右键
- 串口自动识别（发送 `#getDeviceInfo#` 校验设备）
- IP 状态轮询：当前影片名、时长、播放进度、播放/暂停状态
- 点播：`media_player.play_media` 通过 `/playPoster` 播放 SMB/NFS/HTTP 视频，或通过 `/broadCastMusic` 播放音乐
- 海报墙：在媒体库（浏览媒体）里读取设备扫描的影片，只列出视频文件，带封面与片名，点击任意一部直接在电视上播放
- 遥控命令选择器，方便在自动化中逐条调试协议

## 安装

### HACS（推荐）

1. HACS → 集成 → 右上角菜单 → 自定义存储库
2. 添加 `https://github.com/liujun1223/himedia_player`，类别选择「集成」
3. 搜索 “HiMedia Player” 安装，重启 Home Assistant

### 手动安装

把 `custom_components/himedia_player` 复制到 Home Assistant 的 `config/custom_components/` 目录，重启。

## 配置

「设置 → 设备与服务 → 添加集成 → HiMedia Player」，有两种方式：

| 方式 | 说明 |
| --- | --- |
| 自动识别串口 | 扫描本机串口并发送 `#getDeviceInfo#` 校验，识别成功后可选填 IP |
| 手动配置 | 自行选择串口（可留空）与/或填写 IP，波特率默认 115200 |

配置项：

| 参数 | 说明 |
| --- | --- |
| `port` | RS-232 串口，例如 `/dev/ttyUSB0` 或 `COM3`；留空表示不使用串口 |
| `baudrate` | 串口波特率，默认 115200（8N1，无流控） |
| `host` | 播放器 IP（可带端口），例如 `192.168.1.100`；留空表示仅用串口 |

> 串口与 IP 至少填写一个。两者都填写时，控制命令优先走 IP，HTTP 不通时自动改用串口。

## 实体

| 实体 | 说明 |
| --- | --- |
| `media_player.<设备名>` | 播放器本体：开关机、播放控制、音量、点播 |
| `select.<设备名>_remote` | 遥控命令选择器，选中后立即下发 |

支持的遥控命令名与协议键值一一对应，例如 `up`、`down`、`left`、`right`、`ok`、`home`、`back`、`menu`、`volume_up`、`mute`、`digit_1`、`letter_a`、`red`、`subtitle`、`audio_track`、`loop`、`play_video`、`pause_video`、`stop_video`、`previous_video`、`next_video`、`play_audio`、`switch_audio`、`switch_subtitle`、`power_off`、`hold_left`、`hold_right`、`hold_stop` 等。

## 点播示例

```yaml
service: media_player.play_media
target:
  entity_id: media_player.hd950b
data:
  media_content_type: video
  media_content_id: "smb://NAS/f/影片/疯狂动物城.mp4"
```

```yaml
service: media_player.play_media
target:
  entity_id: media_player.hd950b
data:
  media_content_type: music
  media_content_id: "/sdcard/aimei.mp3"
```

视频走 `/playPoster`（自动根据 `smb://`、`nfs://`、`http(s)://` 推断 type），音乐走 `/broadCastMusic`。

## 协议说明

### IP（HiChannelTV，端口 8899）

| 接口 | 说明 |
| --- | --- |
| `GET /get` | 设备信息（mac、DeivcesName、version 等） |
| `GET /getPlayerInfo` | 视频播放信息（filename、duration、playtime、isPlaying） |
| `GET /getAudioPlayerInfo` | 音乐播放信息 |
| `GET /send?key=xx` | 发送遥控键值 |
| `POST /playPoster` | 按 url 播放视频（支持 SMB/NFS） |
| `GET /getPosterList` | 海报墙影片列表（片名、简介、封面、播放地址） |
| `POST /broadCastMusic` | 播放音乐 |
| `GET /playVideo` 等 | 视频/音乐控制、`/switchAudio`、`/switchSub`、`/poweroff` |

### RS-232（115200 8N1）

- 命令格式：`#<Android 标准键值>#`，成功返回 `OK`，失败返回 `ER`
- 复合命令：`#playVideo#`、`#pauseVideo#`、`#stopVideo#`、`#preVideo#`、`#nextVideo#`、`#playAudio#`、`#preAudio#`、`#nextAudio#` 等
- 设备主动上报：`#videoPlay#`、`#videoPause#`、`#videoStop#`、`#audioPlay#`、`#poweron#`、`#poweroff#` 等

## 已知限制

- 仅 IP 通道支持状态轮询与点播；串口模式依赖命令下发后的状态推断
- `/get` 返回的 `state` 字段含义官方未说明，因此开关机状态以最后一次操作为准
- 文档中音乐「下一首」接口写作 `/nextAudioo`，按原始文档实现
- 部分型号才支持 RS-232（`#1030#` 待机、`#1031#` 开机等键值），以实机为准

## 许可

MIT
