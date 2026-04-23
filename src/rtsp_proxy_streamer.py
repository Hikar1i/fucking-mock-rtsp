"""Real RTSP source proxy: pulls an external RTSP stream, applies effects, re-publishes via MediaMTX."""

import logging
import queue
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cv2
import numpy as np

from src.effect_processor import EffectProcessor
from src.webcam_streamer import _encoder_args

logger = logging.getLogger(__name__)

STATUS_CONNECTING   = "connecting"
STATUS_CONNECTED    = "connected"
STATUS_DISCONNECTED = "disconnected"


def mask_url(url: str) -> str:
    """Mask credentials in an RTSP URL for safe logging / display."""
    return re.sub(r'(rtsp://)[^:@/]+:[^:@/]+@', r'\1***:***@', url, flags=re.IGNORECASE)


class RtspProxyStreamer:
    """Pulls a real RTSP source, applies effects, and re-publishes via MediaMTX."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        pcfg = cfg["rtsp_proxy"]
        self._source_url: str       = pcfg["source_url"]
        self._out_width: int        = pcfg["width"]
        self._out_height: int       = pcfg["height"]
        self._out_fps: int          = pcfg["fps"]
        self._rtsp_path: str        = pcfg["rtsp_path"]
        self._reconnect_delay: float = float(pcfg.get("reconnect_delay", 3))
        self._open_timeout: float   = float(pcfg.get("open_timeout", 10))

        self._encoder: str   = cfg["encoding"]["encoder"]
        self._bitrate: str   = cfg["encoding"]["bitrate"]
        self._preset: str    = cfg["encoding"]["preset"]
        self._rtsp_port: int = cfg["server"]["rtsp_port"]

        self._status: str      = STATUS_CONNECTING
        self._status_lock      = threading.Lock()
        self._streaming: bool  = True
        self._running: bool    = False
        self._stop_event       = threading.Event()
        self._reconnect_event  = threading.Event()
        self._thread: threading.Thread | None = None

        self._web_preview: bool = bool(cfg.get("rtsp_proxy", {}).get("web_preview", True))

        self._preview_fps: int = max(1, self._out_fps // 2)
        self._preview_queue: queue.Queue = queue.Queue(maxsize=5)
        self._preview_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pxy-jpeg")
        self._last_preview_jpeg: bytes | None = None
        self._preview_jpeg_lock = threading.Lock()
        self._prev_gen: int = 0

        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_lock = threading.Lock()
        self._ffmpeg_write_queue: queue.Queue = queue.Queue(maxsize=4)
        self._ffmpeg_writer_thread: threading.Thread | None = None

        self.effects = EffectProcessor()

    # ── public API ─────────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        with self._status_lock:
            return self._status

    @property
    def streaming(self) -> bool:
        return self._streaming

    def enable_streaming(self) -> None:
        if not self._streaming:
            self._streaming = True
            logger.info("Proxy RTSP streaming enabled.")

    def disable_streaming(self) -> None:
        if self._streaming:
            self._streaming = False
            self._kill_ffmpeg()
            logger.info("Proxy RTSP streaming disabled.")

    def reconnect(self) -> None:
        """Trigger an immediate reconnect attempt from the UI."""
        logger.info("Proxy reconnect requested.")
        self._reconnect_event.set()

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._stream_loop, daemon=True, name="proxy-stream"
        )
        self._running = True
        self._thread.start()
        logger.info("RTSP proxy streamer started → %s", mask_url(self._source_url))

    def stop(self) -> None:
        self._stop_event.set()
        self._reconnect_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=8)
        self._thread = None
        self._preview_pool.shutdown(wait=False)
        with self._preview_jpeg_lock:
            self._last_preview_jpeg = None
        logger.info("RTSP proxy streamer stopped.")

    @property
    def preview_fps(self) -> int:
        return self._preview_fps

    def get_preview_jpeg(self) -> bytes | None:
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

    # ── internals ──────────────────────────────────────────────────────────────

    def _set_status(self, status: str) -> None:
        with self._status_lock:
            self._status = status

    def _open_capture_with_timeout(self) -> cv2.VideoCapture | None:
        """Open RTSP VideoCapture in a daemon thread to avoid blocking forever."""
        result: list[cv2.VideoCapture | None] = [None]
        exc: list[Exception | None] = [None]

        def _try_open() -> None:
            try:
                cap = cv2.VideoCapture()
                # Set open/read timeouts where available (OpenCV 4.1+)
                if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                    cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(self._open_timeout * 1000))
                if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
                    cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
                ok = cap.open(self._source_url, cv2.CAP_FFMPEG)
                if ok and cap.isOpened():
                    result[0] = cap
                else:
                    cap.release()
            except Exception as e:
                exc[0] = e

        t = threading.Thread(target=_try_open, daemon=True, name="pxy-cap-open")
        t.start()
        t.join(timeout=self._open_timeout + 2.0)

        if t.is_alive():
            logger.warning(
                "RTSP open timed out after %.0fs: %s",
                self._open_timeout, mask_url(self._source_url),
            )
            return None
        if exc[0] is not None:
            logger.error("RTSP open exception: %s", exc[0])
            return None
        return result[0]

    def _enqueue_preview(self, frame: np.ndarray) -> None:
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

    def _writer_loop(self, proc: subprocess.Popen) -> None:
        while True:
            try:
                data = self._ffmpeg_write_queue.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    logger.warning(
                        "Proxy FFmpeg exited (code %s) — will restart on next frame",
                        proc.returncode,
                    )
                    with self._ffmpeg_lock:
                        if self._ffmpeg_proc is proc:
                            self._ffmpeg_proc = None
                    break
                continue
            if data is None:
                break
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError):
                with self._ffmpeg_lock:
                    if self._ffmpeg_proc is proc:
                        self._ffmpeg_proc = None
                break

    def _kill_ffmpeg(self) -> None:
        try:
            self._ffmpeg_write_queue.put_nowait(None)
        except queue.Full:
            while not self._ffmpeg_write_queue.empty():
                try:
                    self._ffmpeg_write_queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._ffmpeg_write_queue.put_nowait(None)
            except queue.Full:
                pass
        with self._ffmpeg_lock:
            proc = self._ffmpeg_proc
            self._ffmpeg_proc = None
        try:
            if proc:
                proc.stdin.close()
        except Exception:
            pass
        if self._ffmpeg_writer_thread:
            self._ffmpeg_writer_thread.join(timeout=2)
            self._ffmpeg_writer_thread = None
        if proc:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _start_ffmpeg(self, width: int, height: int, rtsp_url: str) -> subprocess.Popen | None:
        encoder_args = _encoder_args(self._encoder, self._preset, self._bitrate)
        cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(self._out_fps),
            "-i", "pipe:0",
            *encoder_args,
            "-pix_fmt", "yuv420p",
            "-g", "15",
            "-r", str(self._out_fps),
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            rtsp_url,
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            def _log_stderr() -> None:
                for raw in proc.stderr:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        logger.warning("[FFmpeg proxy] %s", line)
            threading.Thread(target=_log_stderr, daemon=True, name="pxy-ffmpeg-err").start()
            self._ffmpeg_write_queue = queue.Queue(maxsize=4)
            t = threading.Thread(
                target=self._writer_loop, args=(proc,),
                daemon=True, name="pxy-ffmpeg-wr",
            )
            t.start()
            self._ffmpeg_writer_thread = t
            return proc
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            return None
        except Exception as exc:
            logger.error("Failed to start FFmpeg for proxy: %s", exc)
            return None

    def _stream_loop(self) -> None:
        out_rtsp_url = f"rtsp://127.0.0.1:{self._rtsp_port}{self._rtsp_path}"

        while not self._stop_event.is_set():
            self._reconnect_event.clear()
            self._set_status(STATUS_CONNECTING)
            logger.info("Proxy: connecting to %s ...", mask_url(self._source_url))

            cap = self._open_capture_with_timeout()

            if self._stop_event.is_set():
                if cap:
                    cap.release()
                break

            if cap is None:
                self._set_status(STATUS_DISCONNECTED)
                logger.warning(
                    "Proxy: failed to connect. Retrying in %.0fs...", self._reconnect_delay
                )
                self._reconnect_event.wait(timeout=self._reconnect_delay)
                continue

            src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or self._out_width)
            src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or self._out_height)
            self._set_status(STATUS_CONNECTED)
            logger.info(
                "Proxy: connected %dx%d → %s", src_w, src_h, mask_url(self._source_url)
            )

            with self._ffmpeg_lock:
                proc_alive = self._ffmpeg_proc is not None and self._ffmpeg_proc.poll() is None
            if self._streaming and not proc_alive:
                proc = self._start_ffmpeg(self._out_width, self._out_height, out_rtsp_url)
                with self._ffmpeg_lock:
                    self._ffmpeg_proc = proc
                if proc:
                    logger.info("Proxy → RTSP: %s", out_rtsp_url)

            try:
                while not self._stop_event.is_set() and not self._reconnect_event.is_set():
                    # Streaming toggle — check alive, not just not-None
                    with self._ffmpeg_lock:
                        has_proc = (
                            self._ffmpeg_proc is not None
                            and self._ffmpeg_proc.poll() is None
                        )
                    if self._streaming and not has_proc:
                        proc = self._start_ffmpeg(self._out_width, self._out_height, out_rtsp_url)
                        with self._ffmpeg_lock:
                            self._ffmpeg_proc = proc
                        if proc:
                            logger.info("Proxy → RTSP resumed: %s", out_rtsp_url)
                    elif not self._streaming and has_proc:
                        self._kill_ffmpeg()

                    ret, frame = cap.read()
                    if not ret:
                        logger.warning("Proxy: lost connection to source.")
                        self._set_status(STATUS_DISCONNECTED)
                        break

                    self._set_status(STATUS_CONNECTED)

                    if src_w != self._out_width or src_h != self._out_height:
                        frame = cv2.resize(frame, (self._out_width, self._out_height))

                    raw = frame
                    effected = self.effects.apply(frame.copy())

                    with self._ffmpeg_lock:
                        proc = self._ffmpeg_proc
                    if proc is not None:
                        try:
                            self._ffmpeg_write_queue.put_nowait(effected.tobytes())
                        except queue.Full:
                            pass

                    cur_gen = self.effects.get_generation()
                    if cur_gen != self._prev_gen:
                        self._prev_gen = cur_gen
                        while not self._preview_queue.empty():
                            try:
                                self._preview_queue.get_nowait()
                            except queue.Empty:
                                break
                    preview_frame = effected if self.effects.preview_active else raw
                    if self._web_preview:
                        self._preview_pool.submit(self._enqueue_preview, preview_frame.copy())

            finally:
                cap.release()
                self._kill_ffmpeg()

            if not self._stop_event.is_set() and not self._reconnect_event.is_set():
                self._set_status(STATUS_DISCONNECTED)
                logger.info(
                    "Proxy: waiting %.0fs before reconnect...", self._reconnect_delay
                )
                self._reconnect_event.wait(timeout=self._reconnect_delay)

        with self._preview_jpeg_lock:
            self._last_preview_jpeg = None
