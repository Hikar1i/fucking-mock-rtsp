"""Webcam RTSP streamer: captures local camera and pushes via FFmpeg to MediaMTX."""

import logging
import subprocess
import threading
import time
from typing import Any

import cv2
import numpy as np

from src.effect_processor import EffectProcessor

logger = logging.getLogger(__name__)


def probe_webcam(device_id: int = 0) -> bool:
    """Return True if a webcam is accessible at the given device ID."""
    cap = cv2.VideoCapture(device_id, cv2.CAP_ANY)
    ok = cap.isOpened()
    cap.release()
    return ok


class WebcamStreamer:
    """Reads from a local webcam; RTSP push and MJPEG preview are independently controllable."""

    def __init__(self, cfg: dict[str, Any]):
        self._cfg = cfg
        self._device_id: int = cfg["webcam"]["device_id"]
        self._width: int = cfg["webcam"]["width"]
        self._height: int = cfg["webcam"]["height"]
        self._fps: int = cfg["webcam"]["fps"]
        self._rtsp_path: str = cfg["webcam"]["rtsp_path"]
        self._encoder: str = cfg["encoding"]["encoder"]
        self._bitrate: str = cfg["encoding"]["bitrate"]
        self._preset: str = cfg["encoding"]["preset"]
        self._rtsp_port: int = cfg["server"]["rtsp_port"]

        self.available: bool = probe_webcam(self._device_id)
        self._streaming: bool = cfg["webcam"]["enabled"] and self.available
        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._latest_frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()

        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_lock = threading.Lock()

        self.effects = EffectProcessor()

    # ------------------------------------------------------------------ public API

    @property
    def enabled(self) -> bool:
        """True when RTSP push is active."""
        return self._streaming

    def enable(self) -> bool:
        """Enable RTSP push; starts frame loop if not already running."""
        if not self.available:
            return False
        self._streaming = True
        if not self._running:
            self._start_thread()
        return True

    def disable(self) -> None:
        """Disable RTSP push only; MJPEG preview continues if thread is running."""
        self._streaming = False
        self._kill_ffmpeg()
        logger.info("Webcam RTSP streaming disabled (preview still active).")

    def start(self) -> None:
        if not self.available:
            logger.warning("Webcam device %d not available; skipping.", self._device_id)
            return
        self._start_thread()

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        with self._frame_lock:
            self._latest_frame = None
        logger.info("Webcam streamer stopped.")

    def get_latest_frame(self) -> np.ndarray | None:
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    # ------------------------------------------------------------------ internals

    def _start_thread(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._stream_loop, daemon=True, name="webcam-stream")
        self._running = True
        self._thread.start()
        logger.info("Webcam streamer started.")

    def _kill_ffmpeg(self) -> None:
        with self._ffmpeg_lock:
            proc = self._ffmpeg_proc
            self._ffmpeg_proc = None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _stream_loop(self) -> None:
        rtsp_url = f"rtsp://127.0.0.1:{self._rtsp_port}{self._rtsp_path}"
        while not self._stop_event.is_set():
            cap = cv2.VideoCapture(self._device_id, cv2.CAP_ANY)
            if not cap.isOpened():
                logger.error("Cannot open webcam device %d; retrying in 3s...", self._device_id)
                time.sleep(3)
                continue

            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or self._width)
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or self._height)
            logger.info("Webcam → preview: %dx%d @ %dfps", actual_w, actual_h, self._fps)

            try:
                while not self._stop_event.is_set():
                    # Streaming toggle: manage FFmpeg process
                    with self._ffmpeg_lock:
                        has_proc = self._ffmpeg_proc is not None
                    if self._streaming and not has_proc:
                        proc = self._start_ffmpeg(actual_w, actual_h, rtsp_url)
                        with self._ffmpeg_lock:
                            self._ffmpeg_proc = proc
                        if proc:
                            logger.info("Webcam → RTSP: %s (%dx%d @ %dfps)",
                                        rtsp_url, actual_w, actual_h, self._fps)
                    elif not self._streaming and has_proc:
                        self._kill_ffmpeg()

                    ret, frame = cap.read()
                    if not ret:
                        logger.warning("Webcam read failed; restarting...")
                        break

                    raw = frame
                    effected = self.effects.apply(frame.copy())

                    with self._frame_lock:
                        self._latest_frame = effected if self.effects.preview_active else raw

                    with self._ffmpeg_lock:
                        proc = self._ffmpeg_proc
                    if proc is not None:
                        try:
                            proc.stdin.write(effected.tobytes())
                        except (BrokenPipeError, OSError):
                            logger.warning("Webcam FFmpeg pipe broken; restarting...")
                            with self._ffmpeg_lock:
                                self._ffmpeg_proc = None
                            break
            finally:
                cap.release()
                self._kill_ffmpeg()

        with self._frame_lock:
            self._latest_frame = None

    def _start_ffmpeg(self, width: int, height: int, rtsp_url: str) -> subprocess.Popen | None:
        encoder_args = _encoder_args(self._encoder, self._preset, self._bitrate)
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(self._fps),
            "-i", "pipe:0",
            *encoder_args,
            "-pix_fmt", "yuv420p",
            "-r", str(self._fps),
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            rtsp_url,
        ]
        try:
            return subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            return None
        except Exception as exc:
            logger.error("Failed to start FFmpeg: %s", exc)
            return None


def _encoder_args(encoder: str, preset: str, bitrate: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-b:v", bitrate]
    return ["-c:v", "libx264", "-preset", preset, "-tune", "zerolatency", "-b:v", bitrate]
