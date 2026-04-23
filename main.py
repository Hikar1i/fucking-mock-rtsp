"""Main entry point: starts MediaMTX, streamers, and web server concurrently."""

import logging
import logging.handlers
import signal
import sys
from datetime import datetime
from pathlib import Path

import uvicorn

from src.config import load_config
from src.mediamtx_manager import MediaMTXManager
from src.webcam_streamer import WebcamStreamer
from src.video_streamer import VideoStreamer
from src.rtsp_proxy_streamer import RtspProxyStreamer
from src.web_server import create_app


def _setup_logging(base_dir: Path) -> None:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"mock_rtsp_{timestamp}.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(fh)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

    logging.getLogger(__name__).info("Log file: %s", log_file)


logger = logging.getLogger(__name__)


def _validate_config(cfg: dict) -> None:
    webcam_on = cfg.get("webcam", {}).get("enabled", True)
    video_on  = cfg.get("video",  {}).get("enabled", True)
    proxy_on  = cfg.get("rtsp_proxy", {}).get("enabled", False)
    if not (webcam_on or video_on or proxy_on):
        sys.exit(
            "ERROR: All sources are disabled (webcam.enabled, video.enabled, "
            "rtsp_proxy.enabled are all false). Enable at least one source."
        )


def main() -> None:
    cfg = load_config()
    _setup_logging(Path(cfg["_base_dir"]))
    _validate_config(cfg)

    logger.info("=== Local Mock RTSP ===")
    logger.info("Encoder: %s", cfg["encoding"]["encoder"])

    mtx = MediaMTXManager(cfg)
    webcam = WebcamStreamer(cfg)
    video = VideoStreamer(cfg)

    proxy: RtspProxyStreamer | None = None
    if cfg.get("rtsp_proxy", {}).get("enabled", False):
        proxy = RtspProxyStreamer(cfg)

    def shutdown(sig=None, frame=None):
        logger.info("Shutting down...")
        if proxy:
            proxy.stop()
        video.stop()
        webcam.stop()
        mtx.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Starting MediaMTX...")
    try:
        mtx.start()
    except Exception as exc:
        logger.error("MediaMTX failed to start: %s", exc)
        logger.error("RTSP push will not work until MediaMTX is available.")

    logger.info("Starting video streamer (auto-play)...")
    video.start()
    video.play()

    logger.info("Starting webcam streamer...")
    webcam.start()

    if proxy:
        logger.info("Starting RTSP proxy streamer...")
        proxy.start()

    app = create_app(cfg, webcam, video, proxy)

    host = cfg["server"]["host"]
    port = cfg["server"]["web_port"]
    logger.info("Web monitor: http://localhost:%d", port)
    logger.info(
        "RTSP streams: rtsp://localhost:%d%s  |  rtsp://localhost:%d%s",
        cfg["server"]["rtsp_port"], cfg["webcam"]["rtsp_path"],
        cfg["server"]["rtsp_port"], cfg["video"]["rtsp_path"],
    )
    if proxy:
        logger.info(
            "RTSP proxy:   rtsp://localhost:%d%s",
            cfg["server"]["rtsp_port"], cfg["rtsp_proxy"]["rtsp_path"],
        )

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        shutdown()


if __name__ == "__main__":
    main()
