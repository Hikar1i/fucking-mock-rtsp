# Local Mock RTSP

本地 RTSP 流模拟器，用于摄像头异常检测项目的开发调试。支持将本地摄像头或视频文件编码为 RTSP 流，并可通过 Web 页面实时预览和控制，同时支持对视频帧施加多种图像干扰效果。

---

## 目录

- [技术栈](#技术栈)
- [项目功能](#项目功能)
- [安装与启动](#安装与启动)
- [访问地址](#访问地址)
- [Web 监控页面使用教程](#web-监控页面使用教程)
- [配置文件参考](#配置文件参考)
- [REST API](#rest-api)
- [关键技术与架构](#关键技术与架构)

---

## 技术栈

| 层次 | 技术 |
|------|------|
| **语言** | Python 3.11+ |
| **包管理** | [uv](https://docs.astral.sh/uv/) |
| **Web 框架** | FastAPI + Uvicorn |
| **模板引擎** | Jinja2 |
| **视频处理** | OpenCV (`opencv-python`) + NumPy |
| **视频编码/推流** | FFmpeg（libx264 / h264_nvenc 自动选择） |
| **RTSP 服务器** | [MediaMTX](https://github.com/bluenviron/mediamtx)（首次启动自动下载） |
| **并发** | Python `threading` + `ThreadPoolExecutor` + `asyncio` |
| **配置** | PyYAML |
| **前端** | 原生 HTML/CSS/JavaScript（无框架） |

---

## 项目功能

- **摄像头 RTSP 推流**：将本地 USB 摄像头编码推送至 RTSP 路径 `/webcam`，无摄像头环境自动跳过（不阻塞启动）
- **视频文件 RTSP 推流**：将 `videos/` 目录中的视频文件推送至 `/video`，支持：
  - 播放 / 暂停 / 跳转 / 快进 / 快退
  - 播放速度调节（0.1× ~ 4.0×）
  - 循环模式：无循环 / 单文件循环 / 播放列表循环
  - 切换到下一个视频时 RTSP 流无缝续流（不断开客户端连接）
- **Web 监控页面**（`/`）：双路 MJPEG 实时预览 + 全套播放控件
- **图像干扰效果**（摄像头和视频均支持）：
  - 高斯噪声 / 椒盐噪声
  - 高斯模糊 / 运动模糊
  - 随机色块遮挡
  - 亮度/对比度调节
  - 色相偏移/饱和度调节
  - 全黑画面
- **IP 白名单访问控制**：Web UI 和 RTSP 流均可限制允许访问的 IP 地址
- **跨平台**：Windows（libx264 / h264_nvenc） 与 Linux（同）均可运行

---

## 安装与启动

### 前置依赖

| 依赖 | 说明 |
|------|------|
| Python 3.11+ | [python.org](https://www.python.org/downloads/) |
| uv | `pip install uv` 或见 [官方文档](https://docs.astral.sh/uv/getting-started/installation/) |
| FFmpeg | 需在系统 `PATH` 中可用（[下载](https://ffmpeg.org/download.html)） |

> **MediaMTX** 无需手动安装，程序首次启动时自动下载至 `bin/` 目录。

### 安装步骤

```bash
# 1. 克隆项目
git clone <repo-url>
cd local_mock_rtsp

# 2. 安装 Python 依赖（使用 uv.lock 精确还原）
uv sync
```

### 启动

**Windows（推荐）：**

```bat
run.bat
```

**命令行：**

```bash
uv run python main.py
```

启动后控制台会打印各服务地址，MediaMTX 若未下载会自动下载。

### 停止

按 `Ctrl+C` 优雅退出，所有子进程（MediaMTX、FFmpeg）会自动清理。

---

## 访问地址

> 以下为默认配置地址，端口可在 `config.yaml` 中修改。

| 用途 | 地址 |
|------|------|
| Web 监控页面 | `http://localhost:8080` |
| 摄像头 RTSP 流 | `rtsp://localhost:8554/webcam` |
| 视频文件 RTSP 流 | `rtsp://localhost:8554/video` |

RTSP 流可通过 VLC、PotPlayer、ffplay 等播放器打开：

```bash
ffplay rtsp://localhost:8554/video
```

---

## Web 监控页面使用教程

### 布局

页面分为左右两列：
- **左列**：摄像头预览 + 摄像头控制
- **右列**：视频文件预览 + 播放控件 + 效果控制

### 视频播放控制

| 控件 | 说明 |
|------|------|
| ▶ / ⏸ | 播放 / 暂停 |
| ⏮ / ⏭ | 后退 / 前进 3 秒（可修改） |
| 进度条 | 拖动跳转到任意位置 |
| 速度 `−` / `+` | 每次调整 0.1×，范围 0.1× ~ 4.0× |
| 视频下拉列表 | 切换 `videos/` 目录中的视频文件 |
| 循环模式 | 无循环 / 单文件循环 / 播放列表 |

### RTSP 地址复制

点击每路预览下方的地址按钮可一键复制 RTSP 地址。

### 图像干扰效果

每路画面（摄像头 / 视频）有独立的效果面板：

1. **主开关**（Enable Effects）：关闭时所有效果暂停，再次开启时恢复之前的参数设置
2. **预览开关**（Preview in page）：控制 Web 页面预览中是否显示效果（RTSP 流始终包含效果）
3. **效果子项**（均需主开关开启才生效）：

| 效果 | 关键参数 |
|------|---------|
| Black Screen | — 全黑画面，优先级最高 |
| Noise | Gaussian Sigma（高斯噪声强度）、S&P Probability（椒盐概率） |
| Blur | 类型（Gaussian / Motion）、Kernel Size、运动角度/长度 |
| Occlusion | 色块数量、最大尺寸、颜色、刷新间隔 |
| Brightness | 亮度偏移（-100 ~ +100） |
| Contrast | 对比度倍率（0.3 ~ 3.0） |
| Color | 色相偏移（-180 ~ +180）、饱和度（0.0 ~ 2.0） |

---

## 配置文件参考

文件路径：`config.yaml`

```yaml
server:
  rtsp_port: 8554          # MediaMTX RTSP 监听端口
  web_port: 8080           # Web 监控页面端口
  host: "0.0.0.0"          # Web 服务绑定地址（保持 0.0.0.0 以允许局域网访问）
  # IP 白名单：空列表 = 允许所有来源访问
  # 非空时只允许列表内的 IP + 127.0.0.1/::1
  allowed_ips: []
  # allowed_ips: ["192.168.1.100", "192.168.1.200"]

webcam:
  enabled: true            # true = 启用摄像头推流；false = 跳过（适用于无摄像头环境）
  device_id: 0             # 摄像头设备编号（Windows: 0, 1, 2...）
  width: 1024              # 请求摄像头分辨率宽（摄像头会使用最接近支持的分辨率）
  height: 576              # 请求摄像头分辨率高
  fps: 25                  # 目标帧率
  rtsp_path: "/webcam"     # RTSP 路径

video:
  enabled: true            # true = 启用视频文件推流
  default_file: ""         # 默认加载的视频文件名（空 = 自动加载目录第一个）
  videos_dir: "videos"     # 视频文件目录（相对于项目根目录）
  loop: true               # true = 默认单文件循环；false = 播完停止
  width: 1024              # RTSP 输出分辨率宽（源视频会被缩放至此尺寸）
  height: 576              # RTSP 输出分辨率高
  fps: 25                  # 输出帧率
  rtsp_path: "/video"      # RTSP 路径

mediamtx:
  bin_dir: "bin"           # MediaMTX 二进制文件存放目录
  version: "v1.9.1"        # 自动下载的 MediaMTX 版本
  download_timeout: 60     # 下载超时（秒）

encoding:
  encoder: "auto"          # 编码器：auto（自动） / libx264 / h264_nvenc
  bitrate: "1M"            # 视频码率（如 "1M", "2M", "500k"）
  preset: "ultrafast"      # libx264 编码预设（ultrafast/fast/medium 等）
```

### 性能调优建议

- **降低延迟/卡顿**：将 `width` / `height` 减小（如 `640×360`）可显著降低 CV 效果的计算量
- **降低网络带宽**：减小 `bitrate`（如 `"500k"`）
- **NVIDIA GPU 加速**：将 `encoder` 设为 `"h264_nvenc"`（需安装 NVIDIA 驱动）

---

## REST API

### 摄像头

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/webcam/enable` | 启用摄像头 RTSP 推流 |
| `POST` | `/api/webcam/disable` | 禁用摄像头 RTSP 推流 |
| `GET` | `/api/webcam/effects` | 获取摄像头效果配置 |
| `POST` | `/api/webcam/effects` | 更新摄像头效果配置（body: 效果字段 JSON） |

### 视频文件

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/video/play` | 播放 |
| `POST` | `/api/video/pause` | 暂停 |
| `POST` | `/api/video/seek?seconds=N` | 跳转到第 N 秒 |
| `POST` | `/api/video/forward?seconds=3` | 前进 N 秒（默认 3） |
| `POST` | `/api/video/backward?seconds=3` | 后退 N 秒（默认 3） |
| `POST` | `/api/video/load` | 切换视频，body: `{"path": "filename.mp4"}` |
| `POST` | `/api/video/speed` | 设置速度，body: `{"value": 1.5}` 或 `{"delta": 0.1}` |
| `POST` | `/api/video/loop` | 设置循环模式，body: `{"mode": "none"\|"single"\|"playlist"}` |
| `POST` | `/api/video/streaming/enable` | 开启视频 RTSP 推流 |
| `POST` | `/api/video/streaming/disable` | 关闭视频 RTSP 推流（保留 Web 预览） |
| `GET` | `/api/video/effects` | 获取视频效果配置 |
| `POST` | `/api/video/effects` | 更新视频效果配置 |

### 状态与列表

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/status` | 完整状态 JSON（摄像头、视频、效果、视频列表） |
| `GET` | `/api/videos` | 获取可用视频文件列表 |

### 效果配置字段（`/api/{webcam\|video}/effects` POST body）

```json
{
  "all_enabled": false,
  "preview_enabled": false,
  "black_screen": false,
  "noise_enabled": false,
  "noise_gaussian_sigma": 20.0,
  "noise_sp_prob": 0.03,
  "blur_enabled": false,
  "blur_type": "gaussian",
  "blur_kernel": 7,
  "blur_motion_angle": 0.0,
  "blur_motion_length": 15,
  "occlusion_enabled": false,
  "occlusion_count": 3,
  "occlusion_max_size": 80,
  "occlusion_colors": ["black"],
  "occlusion_refresh_sec": 2.0,
  "brightness_enabled": false,
  "brightness_value": 0.0,
  "contrast_enabled": false,
  "contrast_value": 1.0,
  "color_enabled": false,
  "color_hue_shift": 0.0,
  "color_saturation": 1.0
}
```

---

## 关键技术与架构

### 整体架构

```
main.py
├── MediaMTXManager     — 下载、生成配置、启动/停止 MediaMTX 进程
├── WebcamStreamer       — 摄像头帧采集 + 效果处理 + RTSP 推流 + MJPEG 预览
├── VideoStreamer        — 视频文件解码 + 播放控制 + 效果处理 + RTSP 推流 + MJPEG 预览
└── FastAPI (web_server)
    ├── GET  /                  — Web 监控页面（Jinja2 模板）
    ├── GET  /stream/webcam     — 摄像头 MJPEG 流（HTTP multipart）
    ├── GET  /stream/video      — 视频 MJPEG 流（HTTP multipart）
    └── POST /api/...           — 控制 API
```

### FFmpeg 管道推流

每路画面通过 **rawvideo pipe → FFmpeg → MediaMTX RTSP** 的管道推流：

```
OpenCV 解帧
  → NumPy 效果处理
    → 原始 BGR 帧写入 FFmpeg stdin（异步队列，非阻塞）
      → FFmpeg 编码（H.264）→ MediaMTX RTSP 服务器
```

**关键参数**：
- `-g 15`：每 15 帧一个关键帧（约 0.5s），减少 RTSP 客户端缓冲延迟
- `-preset ultrafast`：最低编码延迟
- `writeQueueSize: 2048`：MediaMTX 内部队列，缓解 CV 处理抖动

### 异步 FFmpeg 写入队列（防卡顿）

当 MediaMTX 背压时，`proc.stdin.write()` 会阻塞整个帧处理线程，导致 2-3 秒画面冻结。解决方案：

```
帧处理线程
  → queue.put_nowait(frame_bytes)   ← 满则丢帧，永不阻塞
    ↓
  写入线程（_writer_loop）
    → proc.stdin.write(data)        ← 阻塞在这里，不影响帧处理
```

### 无缝视频切换

播放列表切换或手动换片时，**保持同一 FFmpeg 进程存活**，仅向写入队列填充 3 帧黑帧过渡，RTSP 客户端（VLC、PotPlayer 等）不会断开重连。

### MJPEG 预览管道

```
帧处理线程
  → ThreadPoolExecutor (2 workers)
    → letterbox 缩放至 640×360
      → JPEG 编码（quality=60）
        → queue.put（满则丢旧帧，保持最新）
          ↑
FastAPI /stream/* 每帧从队列取最新 JPEG 推送给浏览器
```

### 效果处理器（EffectProcessor）

- **线程安全**：每帧 apply() 先复制 config 快照，再处理，锁粒度最小
- **Config Generation**：每次配置变更递增 generation 计数器；帧处理线程检测到变更时清空预览队列，避免旧效果帧残留
- **状态保存/恢复**：关闭主开关时保存当前配置，重新开启时自动恢复

### 依赖版本管理

- `pyproject.toml` 使用 `>=` 声明最低兼容版本
- `uv.lock` 记录精确版本快照并提交到 git；`uv sync` 总从 lock 文件安装，保证团队和 CI 环境一致
- `uv.lock` 支持 platform markers，同一 lock 文件在 Windows/Linux 上自动安装对应平台的 wheel

### IP 访问控制

- **Web UI**：FastAPI middleware，非白名单 IP 返回 403（`host: "0.0.0.0"` 绑定所有网卡，访问控制由中间件负责）
- **RTSP**：MediaMTX 配置的 `publishIPs`（仅 localhost）和 `readIPs`（白名单），分别限制推流端和观看端
- `allowed_ips: []` 为空时完全开放，不注册中间件

---

## 视频文件格式

将视频放入 `videos/` 目录，支持格式：

`mp4` `mkv` `avi` `mov` `webm` `ts` `flv`

Web 页面下拉列表自动扫描该目录。

## 日志

运行日志保存在 `logs/mock_rtsp_YYYYMMDD_HHMMSS.log`，同时输出到控制台。
