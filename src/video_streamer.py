"""Video file RTSP streamer with playback control (play/pause/seek/speed/loop)."""

import logging
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.webcam_streamer import _encoder_args
from src.effect_processor import EffectProcessor

logger = logging.getLogger(__name__)

SPEED_MIN = 0.1
SPEED_MAX = 4.0
SPEED_STEP = 0.1


class VideoState:
    """Thread-safe playback state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.playing: bool = False
        self.seek_request: float | None = None
        self.file_path: str = ""
        self.total_seconds: float = 0.0
        self.current_seconds: float = 0.0
        self.fps: float = 25.0
        self.width: int = 1280
        self.height: int = 720
        self.speed: float = 1.0
        self.loop_mode: str = "none"   # "none" | "single" | "playlist"
        self.streaming: bool = True     # RTSP push on/off

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "playing": self.playing,
                "file_path": self.file_path,
                "total_seconds": round(self.total_seconds, 1),
                "current_seconds": round(self.current_seconds, 1),
                "fps": self.fps,
                "width": self.width,
                "height": self.height,
                "speed": round(self.speed, 1),
                "loop_mode": self.loop_mode,
                "streaming": self.streaming,
            }

    def request_seek(self, seconds: float) -> None:
        with self._lock:
            self.seek_request = float(seconds)

    def pop_seek_request(self) -> float | None:
        with self._lock:
            val = self.seek_request
            self.seek_request = None
            return val


class VideoStreamer:
    """Reads a video file with playback control and pushes RTSP via FFmpeg."""

    def __init__(self, cfg: dict[str, Any]):
        self._cfg = cfg
        self._out_width: int = cfg["video"]["width"]
        self._out_height: int = cfg["video"]["height"]
        self._out_fps: int = cfg["video"]["fps"]
        self._rtsp_path: str = cfg["video"]["rtsp_path"]
        self._rtsp_port: int = cfg["server"]["rtsp_port"]
        self._encoder: str = cfg["encoding"]["encoder"]
        self._bitrate: str = cfg["encoding"]["bitrate"]
        self._preset: str = cfg["encoding"]["preset"]
        self._videos_dir: Path = Path(cfg["_base_dir"]) / cfg["video"]["videos_dir"]

        self.state = VideoState()
        self.state.loop_mode = "single" if cfg["video"].get("loop", False) else "none"
        self.effects = EffectProcessor()

        self._play_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running: bool = False

        self._web_preview: bool = bool(cfg["video"].get("web_preview", True))

        # Preview pipeline: downscaled JPEG queue + thread pool for encoding
        self._preview_fps: int = max(1, self._out_fps // 2)
        self._preview_queue: queue.Queue = queue.Queue(maxsize=5)
        self._preview_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vid-jpeg")
        self._last_preview_jpeg: bytes | None = None
        self._preview_jpeg_lock = threading.Lock()

        # FFmpeg process — managed inside stream loop but tracked for enable/disable
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_lock = threading.Lock()
        self._ffmpeg_write_queue: queue.Queue = queue.Queue(maxsize=4)
        self._ffmpeg_writer_thread: threading.Thread | None = None

        default_file = cfg["video"].get("default_file", "")
        if default_file:
            self.state.file_path = str(self._resolve_video_path(default_file))
        else:
            self._pick_first_video()

    # ------------------------------------------------------------------ helpers

    def _pick_first_video(self) -> None:
        if not self._videos_dir.exists():
            return
        exts = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".flv"}
        files = sorted(f for f in self._videos_dir.iterdir() if f.is_file() and f.suffix.lower() in exts)
        if files:
            self.state.file_path = str(files[0])
            logger.info("Default video: %s", files[0].name)

    def _resolve_video_path(self, name_or_path: str) -> Path:
        p = Path(name_or_path)
        if p.is_absolute() and p.exists():
            return p
        candidate = self._videos_dir / name_or_path
        if candidate.exists():
            return candidate
        return p

    def _list_videos(self) -> list[str]:
        if not self._videos_dir.exists():
            return []
        exts = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".flv"}
        return sorted(f.name for f in self._videos_dir.iterdir() if f.is_file() and f.suffix.lower() in exts)

    def _next_video(self) -> str | None:
        videos = self._list_videos()
        if not videos or not self.state.file_path:
            return None
        current = Path(self.state.file_path).name
        try:
            idx = videos.index(current)
        except ValueError:
            return None
        next_idx = (idx + 1) % len(videos)
        return str(self._videos_dir / videos[next_idx]) if next_idx != idx else None

    # ------------------------------------------------------------------ playback API

    def load_video(self, name_or_path: str) -> bool:
        resolved = self._resolve_video_path(name_or_path)
        if not resolved.exists():
            logger.error("Video not found: %s", name_or_path)
            return False
        was_playing = self.state.playing
        self.state.playing = False
        self._play_event.clear()
        self.state.file_path = str(resolved)
        self.state.seek_request = 0.0
        if was_playing:
            self.state.playing = True
            self._play_event.set()
        logger.info("Loaded video: %s", resolved.name)
        return True

    def play(self) -> None:
        self.state.playing = True
        self._play_event.set()

    def pause(self) -> None:
        self.state.playing = False
        self._play_event.clear()

    def seek(self, seconds: float) -> None:
        self.state.request_seek(max(0.0, seconds))

    def forward(self, seconds: float = 3.0) -> None:
        self.seek(self.state.current_seconds + seconds)

    def backward(self, seconds: float = 3.0) -> None:
        self.seek(max(0.0, self.state.current_seconds - seconds))

    def set_speed(self, speed: float) -> float:
        speed = round(max(SPEED_MIN, min(SPEED_MAX, speed)), 1)
        self.state.speed = speed
        return speed

    def adjust_speed(self, delta: float) -> float:
        return self.set_speed(round(self.state.speed + delta, 1))

    def set_loop_mode(self, mode: str) -> None:
        if mode in ("none", "single", "playlist"):
            self.state.loop_mode = mode

    def enable_streaming(self) -> None:
        """Start RTSP push (MJPEG preview is unaffected)."""
        if not self.state.streaming:
            self.state.streaming = True
            logger.info("Video RTSP streaming enabled.")

    def disable_streaming(self) -> None:
        """Stop RTSP push while keeping frame loop alive for MJPEG preview."""
        if self.state.streaming:
            self.state.streaming = False
            self._kill_ffmpeg()
            logger.info("Video RTSP streaming disabled.")

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._stream_loop, daemon=True, name="video-stream")
        self._running = True
        self._thread.start()
        logger.info("Video streamer started.")

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        self._play_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=8)
        self._thread = None
        self._preview_pool.shutdown(wait=False)
        with self._preview_jpeg_lock:
            self._last_preview_jpeg = None
        logger.info("Video streamer stopped.")

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
            # Drop oldest frames if queue is full (prefer freshest)
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

    # ------------------------------------------------------------------ internal

    def _writer_loop(self, proc: subprocess.Popen) -> None:
        """Dedicated thread: drains _ffmpeg_write_queue → FFmpeg stdin."""
        while True:
            try:
                data = self._ffmpeg_write_queue.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
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

    def _stream_loop(self) -> None:
        rtsp_url = f"rtsp://127.0.0.1:{self._rtsp_port}{self._rtsp_path}"
        while not self._stop_event.is_set():
            file_path = self.state.file_path
            if not file_path or not Path(file_path).exists():
                logger.warning("No valid video file set; waiting...")
                time.sleep(2)
                continue

            cap = cv2.VideoCapture(file_path)
            if not cap.isOpened():
                logger.error("Cannot open video: %s; retrying in 3s...", file_path)
                time.sleep(3)
                continue

            src_fps = cap.get(cv2.CAP_PROP_FPS) or self._out_fps
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if total_frames <= 0:
                # Unknown frame count (VFR / bad container metadata); rely on
                # cap.read() failure for EoF detection instead.
                total_frames = float("inf")
                total_sec = 0.0
            else:
                total_sec = total_frames / src_fps if src_fps > 0 else 0.0
            src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

            self.state.fps = src_fps
            self.state.total_seconds = total_sec
            self.state.width = src_w
            self.state.height = src_h

            # fractional frame position tracker (for speed <> 1.0)
            frame_pos: float = cap.get(cv2.CAP_PROP_POS_FRAMES)

            frame_interval = 1.0 / self._out_fps
            next_frame_time = time.monotonic() + frame_interval

            # Preview sub-sampling counters
            _prev_counter: int = 0
            _prev_skip: int = max(1, self._out_fps // self._preview_fps)
            _prev_gen: int = self.effects.get_generation()

            # Start FFmpeg only if streaming is enabled AND not already running
            # (kept alive from previous video for seamless transitions)
            with self._ffmpeg_lock:
                _proc_alive = self._ffmpeg_proc is not None and self._ffmpeg_proc.poll() is None
            if self.state.streaming and not _proc_alive:
                proc = self._start_ffmpeg(self._out_width, self._out_height, rtsp_url)
                with self._ffmpeg_lock:
                    self._ffmpeg_proc = proc
                if proc:
                    logger.info("Video → RTSP: %s → %s", Path(file_path).name, rtsp_url)

            _keep_ffmpeg = False  # set True on seamless transitions
            try:
                while not self._stop_event.is_set():
                    # File changed → keep FFmpeg alive, reload outer loop
                    if file_path != self.state.file_path:
                        logger.info("Video file changed; reloading.")
                        _keep_ffmpeg = True
                        break

                    # Streaming toggle: start/stop FFmpeg as needed
                    with self._ffmpeg_lock:
                        has_proc = self._ffmpeg_proc is not None
                    if self.state.streaming and not has_proc:
                        proc = self._start_ffmpeg(self._out_width, self._out_height, rtsp_url)
                        with self._ffmpeg_lock:
                            self._ffmpeg_proc = proc
                        if proc:
                            logger.info("Video → RTSP resumed: %s → %s", Path(file_path).name, rtsp_url)
                    elif not self.state.streaming and has_proc:
                        self._kill_ffmpeg()

                    # Seek request
                    seek_to = self.state.pop_seek_request()
                    if seek_to is not None:
                        cap.set(cv2.CAP_PROP_POS_MSEC, seek_to * 1000)
                        frame_pos = cap.get(cv2.CAP_PROP_POS_FRAMES)

                    # Paused → wait; reset drift timer on resume so no burst
                    if not self.state.playing:
                        self._play_event.wait(timeout=0.5)
                        next_frame_time = time.monotonic() + frame_interval
                        continue

                    # Speed: advance frame position by speed factor
                    speed = max(SPEED_MIN, self.state.speed)
                    frame_pos += speed
                    target_frame = int(frame_pos)

                    # EoF: counter-based check OR actual read failure
                    # (CAP_PROP_FRAME_COUNT is unreliable for some containers/VFR)
                    _at_eof = target_frame >= total_frames
                    frame = None
                    if not _at_eof:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                        ret, frame = cap.read()
                        if not ret:
                            _at_eof = True

                    if _at_eof:
                        mode = self.state.loop_mode
                        if mode == "single":
                            logger.info("Video loop (single): rewinding.")
                            frame_pos = 0.0
                            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                            continue
                        elif mode == "playlist":
                            nxt = self._next_video()
                            if nxt:
                                logger.info("Playlist: next video → %s", Path(nxt).name)
                                self.state.file_path = nxt
                                _keep_ffmpeg = True  # seamless: keep FFmpeg alive
                                break
                            else:
                                # Single video in list → rewind seamlessly
                                _keep_ffmpeg = True
                                frame_pos = 0.0
                                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                continue
                        else:
                            logger.info("Video ended.")
                            self.state.playing = False
                            self._play_event.clear()
                            break


                    self.state.current_seconds = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

                    if src_w != self._out_width or src_h != self._out_height:
                        frame = cv2.resize(frame, (self._out_width, self._out_height))

                    # Apply effects: full-quality for RTSP
                    raw = frame
                    effected = self.effects.apply(frame.copy())

                    # Write to FFmpeg via async queue (non-blocking: drop if full)
                    with self._ffmpeg_lock:
                        proc = self._ffmpeg_proc
                    if proc is not None:
                        try:
                            self._ffmpeg_write_queue.put_nowait(effected.tobytes())
                        except queue.Full:
                            pass  # Drop frame: encoder/network backpressured

                    # Preview: subsampled + downscaled JPEG via thread pool
                    _prev_counter += 1
                    if _prev_counter % _prev_skip == 0:
                        cur_gen = self.effects.get_generation()
                        if cur_gen != _prev_gen:
                            _prev_gen = cur_gen
                            while not self._preview_queue.empty():
                                try:
                                    self._preview_queue.get_nowait()
                                except queue.Empty:
                                    break
                        preview_frame = effected if self.effects.preview_active else raw
                        if self._web_preview:
                            self._preview_pool.submit(self._enqueue_preview, preview_frame.copy())

                    # Drift-free timing: schedule next frame relative to fixed baseline
                    sleep_time = next_frame_time - time.monotonic()
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    elif sleep_time < -frame_interval * 2:
                        # Fallen >2 frames behind (e.g. heavy CV); reset rather than burst
                        next_frame_time = time.monotonic()
                    next_frame_time += frame_interval

            finally:
                cap.release()
                if _keep_ffmpeg and self.state.streaming and not self._stop_event.is_set():
                    # Bridge transition with a few black frames (no stream disconnect)
                    _black = np.zeros((self._out_height, self._out_width, 3), dtype=np.uint8).tobytes()
                    for _ in range(3):
                        try:
                            self._ffmpeg_write_queue.put_nowait(_black)
                        except queue.Full:
                            break
                else:
                    self._kill_ffmpeg()

    def _start_ffmpeg(self, width: int, height: int, rtsp_url: str) -> subprocess.Popen | None:
        encoder_args = _encoder_args(self._encoder, self._preset, self._bitrate)
        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(self._out_fps),
            "-i", "pipe:0",
            *encoder_args,
            "-pix_fmt", "yuv420p",
            "-r", str(self._out_fps),
            "-g", "15",
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            rtsp_url,
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Fresh write queue + dedicated non-blocking writer thread
            self._ffmpeg_write_queue = queue.Queue(maxsize=4)
            t = threading.Thread(target=self._writer_loop, args=(proc,),
                                 daemon=True, name="vid-ffmpeg-wr")
            t.start()
            self._ffmpeg_writer_thread = t
            return proc
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            return None
        except Exception as exc:
            logger.error("Failed to start FFmpeg: %s", exc)
            return None
