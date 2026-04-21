# Local Mock RTSP

本地 RTSP 流模拟器，用于摄像头异常检测项目的开发调试。

## 功能

- 将本地摄像头推送为 RTSP 流（可开关）
- 将视频文件推送为 RTSP 流（支持播放/暂停/快进/快退/跳转）
- Web 监控页面实时预览双路画面
- 无摄像头环境（Ubuntu 服务器）优雅降级，不阻塞启动
- Windows/Ubuntu 平台自动选择编码器（libx264 / h264_nvenc）

## RTSP 流地址

| 用途 | 地址 |
|------|------|
| 本地摄像头 | `rtsp://localhost:8554/webcam` |
| 视频文件 | `rtsp://localhost:8554/video` |
| Web 监控页面 | `http://localhost:8080` |

## 前置依赖

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- FFmpeg（需在 PATH 中可用）

MediaMTX 会在首次启动时自动下载，无需手动安装。

## 快速启动

```bash
# 安装依赖
uv sync

# 启动（自动下载 MediaMTX、启动推流、打开 Web 页面）
uv run python main.py
```

## 配置

编辑 `config.yaml`：

```yaml
webcam:
  enabled: true      # 是否启用摄像头推流
  device_id: 0       # 摄像头设备编号

video:
  videos_dir: videos  # 视频文件目录
  loop: true          # 播放完毕后循环

server:
  rtsp_port: 8554
  web_port: 8080
```

## 视频文件

将视频文件放入 `videos/` 目录，支持格式：mp4、mkv、avi、mov、webm、ts、flv。  
Web 页面下拉框会自动列出该目录下所有视频文件。

## REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/webcam/enable` | 启用摄像头推流 |
| POST | `/api/webcam/disable` | 禁用摄像头推流 |
| POST | `/api/video/play` | 播放 |
| POST | `/api/video/pause` | 暂停 |
| POST | `/api/video/seek?seconds=N` | 跳转到 N 秒 |
| POST | `/api/video/forward?seconds=10` | 前进 |
| POST | `/api/video/backward?seconds=10` | 后退 |
| POST | `/api/video/load` | 切换视频（body: `{"path": "filename.mp4"}`）|
| GET  | `/api/status` | 当前状态 JSON |
