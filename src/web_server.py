"""FastAPI web server: MJPEG preview streams, REST control API, monitoring page."""

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncGenerator

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import list_videos, get_videos_dir, get_host_ips
from src.webcam_streamer import WebcamStreamer
from src.video_streamer import VideoStreamer
from src.rtsp_proxy_streamer import RtspProxyStreamer, mask_url

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _placeholder_frame(width: int = 640, height: int = 360, text: str = "N/A") -> bytes:
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = (40, 40, 40)
    font = cv2.FONT_HERSHEY_SIMPLEX
    text_size = cv2.getTextSize(text, font, 1.0, 2)[0]
    tx = (width - text_size[0]) // 2
    ty = (height + text_size[1]) // 2
    cv2.putText(img, text, (tx, ty), font, 1.0, (120, 120, 120), 2)
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes()


async def _mjpeg_generator_bytes(
    get_jpeg_fn,
    placeholder_bytes: bytes,
    fps: int = 12,
) -> AsyncGenerator[bytes, None]:
    """MJPEG generator that consumes pre-encoded JPEG bytes (no re-encoding overhead)."""
    interval = 1.0 / fps
    while True:
        start = asyncio.get_event_loop().time()
        jpg = await asyncio.get_event_loop().run_in_executor(None, get_jpeg_fn)
        if jpg is None:
            jpg = placeholder_bytes
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
        )
        elapsed = asyncio.get_event_loop().time() - start
        sleep = max(0.0, interval - elapsed)
        await asyncio.sleep(sleep)


def create_app(
    cfg: dict[str, Any],
    webcam: WebcamStreamer,
    video: VideoStreamer,
    proxy: RtspProxyStreamer | None = None,
) -> FastAPI:
    app = FastAPI(title="Local Mock RTSP", version="0.1.0")

    # ── IP whitelist middleware ──────────────────────────────────────────────
    _raw_ips: list[str] = cfg["server"].get("allowed_ips", []) or []
    if _raw_ips:
        _allowed_ips: frozenset[str] = frozenset(_raw_ips) | {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

        @app.middleware("http")
        async def _ip_whitelist(request: Request, call_next):
            host = request.client.host if request.client else ""
            if host not in _allowed_ips:
                return JSONResponse({"error": "Forbidden"}, status_code=403)
            return await call_next(request)
    _jinja = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        cache_size=0,
    )

    rtsp_port = cfg["server"]["rtsp_port"]
    _host_ips = get_host_ips() or ["localhost"]
    webcam_rtsp_urls = [f"rtsp://{ip}:{rtsp_port}{cfg['webcam']['rtsp_path']}" for ip in _host_ips]
    video_rtsp_urls = [f"rtsp://{ip}:{rtsp_port}{cfg['video']['rtsp_path']}" for ip in _host_ips]
    webcam_rtsp = webcam_rtsp_urls[0]  # primary for status API
    video_rtsp = video_rtsp_urls[0]    # primary for status API
    proxy_rtsp_urls: list[str] = []
    proxy_rtsp: str = ""
    proxy_source_masked: str = ""
    if proxy is not None:
        proxy_rtsp_urls = [f"rtsp://{ip}:{rtsp_port}{cfg['rtsp_proxy']['rtsp_path']}" for ip in _host_ips]
        proxy_rtsp = proxy_rtsp_urls[0]
        proxy_source_masked = mask_url(cfg["rtsp_proxy"]["source_url"])

    # ── web_preview flags (per-stream) ──────────────────────────────────────
    webcam_web_preview: bool = bool(cfg["webcam"].get("web_preview", True))
    video_web_preview: bool  = bool(cfg["video"].get("web_preview", True))
    proxy_web_preview: bool  = bool(cfg.get("rtsp_proxy", {}).get("web_preview", True))

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        videos = list_videos(cfg)
        current = Path(video.state.file_path).name if video.state.file_path else ""
        tmpl = _jinja.get_template("index.html")
        rendered = tmpl.render(
            webcam_available=webcam.available,
            webcam_enabled=webcam.enabled,
            webcam_web_preview=webcam_web_preview,
            webcam_rtsp_urls=webcam_rtsp_urls,
            video_enabled=cfg["video"].get("enabled", True),
            video_web_preview=video_web_preview,
            video_rtsp_urls=video_rtsp_urls,
            videos=videos,
            current_video=current,
            proxy_enabled=proxy is not None,
            proxy_web_preview=proxy_web_preview,
            proxy_rtsp_urls=proxy_rtsp_urls,
            proxy_source_masked=proxy_source_masked,
        )
        return HTMLResponse(rendered)

    # --------------------------------------------------------------- MJPEG streams
    # ── proxy MJPEG + API routes (only registered when proxy is enabled) ──────
    if proxy is not None:
        @app.get("/stream/proxy")
        async def stream_proxy():
            placeholder = _placeholder_frame(640, 360, "Proxy Disconnected")
            return StreamingResponse(
                _mjpeg_generator_bytes(proxy.get_preview_jpeg, placeholder, fps=proxy.preview_fps),
                media_type="multipart/x-mixed-replace; boundary=frame",
            )

        @app.post("/api/proxy/streaming/enable")
        async def proxy_streaming_enable():
            proxy.enable_streaming()
            return {"ok": True, "streaming": proxy.streaming}

        @app.post("/api/proxy/streaming/disable")
        async def proxy_streaming_disable():
            proxy.disable_streaming()
            return {"ok": True, "streaming": proxy.streaming}

        @app.post("/api/proxy/reconnect")
        async def proxy_reconnect():
            proxy.reconnect()
            return {"ok": True}

        @app.get("/api/proxy/effects")
        async def proxy_effects_get():
            return proxy.effects.get_config()

        @app.post("/api/proxy/effects")
        async def proxy_effects_post(request: Request):
            data = await request.json()
            if not proxy_web_preview:
                data["preview_enabled"] = False
            proxy.effects.update_config(data)
            return proxy.effects.get_config()

    @app.get("/stream/webcam")
    async def stream_webcam():
        if not webcam_web_preview:
            return JSONResponse({"error": "Web preview disabled for webcam"}, status_code=503)
        placeholder = _placeholder_frame(640, 360, "Webcam Unavailable")
        return StreamingResponse(
            _mjpeg_generator_bytes(webcam.get_preview_jpeg, placeholder, fps=webcam.preview_fps),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/stream/video")
    async def stream_video():
        if not video_web_preview:
            return JSONResponse({"error": "Web preview disabled for video"}, status_code=503)
        placeholder = _placeholder_frame(640, 360, "No Video Frame")
        return StreamingResponse(
            _mjpeg_generator_bytes(video.get_preview_jpeg, placeholder, fps=video.preview_fps),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    # --------------------------------------------------------------- webcam API
    @app.post("/api/webcam/enable")
    async def webcam_enable():
        if not webcam.available:
            raise HTTPException(status_code=503, detail="Webcam not available on this host")
        ok = webcam.enable()
        return {"ok": ok, "enabled": webcam.enabled}

    @app.post("/api/webcam/disable")
    async def webcam_disable():
        webcam.disable()
        return {"ok": True, "enabled": webcam.enabled}

    # --------------------------------------------------------------- video API
    @app.post("/api/video/play")
    async def video_play():
        video.play()
        return {"ok": True, "playing": video.state.playing}

    @app.post("/api/video/pause")
    async def video_pause():
        video.pause()
        return {"ok": True, "playing": video.state.playing}

    @app.post("/api/video/seek")
    async def video_seek(seconds: float):
        video.seek(seconds)
        return {"ok": True, "seek_to": seconds}

    @app.post("/api/video/forward")
    async def video_forward(seconds: float = 3.0):
        video.forward(seconds)
        return {"ok": True}

    @app.post("/api/video/backward")
    async def video_backward(seconds: float = 3.0):
        video.backward(seconds)
        return {"ok": True}

    @app.post("/api/video/load")
    async def video_load(request: Request):
        body = await request.json()
        name_or_path = body.get("path", "")
        if not name_or_path:
            raise HTTPException(status_code=400, detail="Missing 'path' field")
        ok = video.load_video(name_or_path)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Video not found: {name_or_path}")
        return {"ok": True, "loaded": name_or_path}

    @app.post("/api/video/speed")
    async def video_speed(request: Request):
        body = await request.json()
        if "delta" in body:
            speed = video.adjust_speed(float(body["delta"]))
        elif "value" in body:
            speed = video.set_speed(float(body["value"]))
        else:
            raise HTTPException(status_code=400, detail="Provide 'delta' or 'value'")
        return {"ok": True, "speed": speed}

    @app.post("/api/video/loop")
    async def video_loop(request: Request):
        body = await request.json()
        mode = body.get("mode", "none")
        video.set_loop_mode(mode)
        return {"ok": True, "loop_mode": video.state.loop_mode}

    @app.post("/api/video/streaming/enable")
    async def video_streaming_enable():
        video.enable_streaming()
        return {"ok": True, "streaming": video.state.streaming}

    @app.post("/api/video/streaming/disable")
    async def video_streaming_disable():
        video.disable_streaming()
        return {"ok": True, "streaming": video.state.streaming}

    # --------------------------------------------------------------- effects API
    @app.get("/api/webcam/effects")
    async def webcam_effects_get():
        return webcam.effects.get_config()

    @app.post("/api/webcam/effects")
    async def webcam_effects_post(request: Request):
        data = await request.json()
        if not webcam_web_preview:
            data["preview_enabled"] = False
        webcam.effects.update_config(data)
        return webcam.effects.get_config()

    @app.get("/api/video/effects")
    async def video_effects_get():
        return video.effects.get_config()

    @app.post("/api/video/effects")
    async def video_effects_post(request: Request):
        data = await request.json()
        if not video_web_preview:
            data["preview_enabled"] = False
        video.effects.update_config(data)
        return video.effects.get_config()

    # --------------------------------------------------------------- status API
    @app.get("/api/status")
    async def status():
        videos = list_videos(cfg)
        current_name = Path(video.state.file_path).name if video.state.file_path else ""
        result: dict = {
            "webcam": {
                "available": webcam.available,
                "enabled": webcam.enabled,
                "rtsp_url": webcam_rtsp,
                "effects": webcam.effects.get_config(),
            },
            "video": {
                **video.state.snapshot(),
                "file_name": current_name,
                "rtsp_url": video_rtsp,
                "effects": video.effects.get_config(),
            },
            "videos_list": videos,
        }
        if proxy is not None:
            result["proxy"] = {
                "status": proxy.status,
                "streaming": proxy.streaming,
                "rtsp_url": proxy_rtsp,
                "effects": proxy.effects.get_config(),
            }
        return JSONResponse(result)

    @app.get("/api/videos")
    async def list_videos_api():
        return {"videos": list_videos(cfg)}

    return app
