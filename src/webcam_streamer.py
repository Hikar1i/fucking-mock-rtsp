"""Webcam RTSP streamer: captures local camera and pushes via FFmpeg to MediaMTX."""

import logging
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

        # Preview pipeline: downscaled JPEG queue + thread pool for encoding
        self._preview_fps: int = max(1, self._fps // 2)
        self._preview_queue: queue.Queue = queue.Queue(maxsize=5)
        self._preview_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wcam-jpeg")
        self._last_preview_jpeg: bytes | None = None
        self._preview_jpeg_lock = threading.Lock()

        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_lock = threading.Lock()
        self._wcam_prev_gen: int = 0

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
        self._preview_pool.shutdown(wait=False)
        with self._preview_jpeg_lock:
            self._last_preview_jpeg = None
        logger.info("Webcam streamer stopped.")

    @property
    def preview_fps(self) -> int:
        return self._preview_fps

    def get_preview_jpeg(self) -> bytes | None:
        """Return the most recent preview JPEG (bytes). Falls back to last known frame."""
        try:
            jpg = self._preview_queue.get_nowait()
        except queue.Empty:
            jpg = None
        if jpg is not None:
            with self._preview_jpeg_lock:
                self._last_preview_jpeg = jpg
            return jpg
        with self._preview_jpeg_lock:
            return self._last_preview_jpeg

    def _enqueue_preview(self, frame: np.ndarray) -> None:
        """Runs in thread pool: letterbox → JPEG encode → enqueue."""
        try:
            target_w, target_h = 640, 360
            h, w = frame.shape[:2]
            scale = min(target_w / w, target_h / h)
            new_w, new_h = int(w * scale), int(h * scale)
            resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
            x_off = (target_w - new_w) // 2
            y_off = (target_h - new_h) // 2
            canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
            ret, buf = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 60])
            if not ret:
                return
            jpg = buf.tobytes()
            while self._preview_queue.full():
                try:
                    self._preview_queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._preview_queue.put_nowait(jpg)
            except queue.Full:
                pass
        except Exception:
            pass

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

                    # Write to FFmpeg if streaming
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

                    # Preview: every frame at native rate (already low due to webcam fps)
                    cur_gen = self.effects.get_generation()
                    if cur_gen != self._wcam_prev_gen:
                        self._wcam_prev_gen = cur_gen
                        while not self._preview_queue.empty():
                            try:
                                self._preview_queue.get_nowait()
                            except queue.Empty:
                                break
                    preview_frame = effected if self.effects.preview_active else raw
                    self._preview_pool.submit(self._enqueue_preview, preview_frame.copy())

            finally:
                cap.release()
                self._kill_ffmpeg()

        with self._preview_jpeg_lock:
            self._last_preview_jpeg = None

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
            "-g", "15",
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
