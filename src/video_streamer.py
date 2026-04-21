"""Video file RTSP streamer with playback control (play/pause/seek/speed/loop)."""

import logging
import subprocess
import threading
import time
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

        self._latest_frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()

        # FFmpeg process — managed inside stream loop but tracked for enable/disable
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_lock = threading.Lock()

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
        logger.info("Video streamer stopped.")

    def get_latest_frame(self) -> np.ndarray | None:
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    # ------------------------------------------------------------------ internal

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
            last_frame_time = time.monotonic()

            # Start FFmpeg only if streaming is enabled
            if self.state.streaming:
                proc = self._start_ffmpeg(self._out_width, self._out_height, rtsp_url)
                with self._ffmpeg_lock:
                    self._ffmpeg_proc = proc
                if proc:
                    logger.info("Video → RTSP: %s → %s", Path(file_path).name, rtsp_url)

            try:
                while not self._stop_event.is_set():
                    # File changed → restart outer loop
                    if file_path != self.state.file_path:
                        logger.info("Video file changed; reloading.")
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

                    # Paused → wait
                    if not self.state.playing:
                        self._play_event.wait(timeout=0.5)
                        last_frame_time = time.monotonic()
                        continue

                    # Speed: advance frame position by speed factor
                    speed = max(SPEED_MIN, self.state.speed)
                    frame_pos += speed
                    target_frame = int(frame_pos)

                    if target_frame >= total_frames:
                        # End of video
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
                                break
                            else:
                                # Single video in list → rewind
                                frame_pos = 0.0
                                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                                continue
                        else:
                            logger.info("Video ended.")
                            self.state.playing = False
                            self._play_event.clear()
                            break

                    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                    ret, frame = cap.read()
                    if not ret:
                        frame_pos = max(0.0, frame_pos - speed)
                        continue

                    self.state.current_seconds = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

                    if src_w != self._out_width or src_h != self._out_height:
                        frame = cv2.resize(frame, (self._out_width, self._out_height))

                    # Apply effects (always for FFmpeg; conditionally for MJPEG)
                    raw = frame
                    effected = self.effects.apply(frame.copy())

                    with self._frame_lock:
                        self._latest_frame = effected if self.effects.preview_active else raw

                    # Write to FFmpeg if streaming
                    with self._ffmpeg_lock:
                        proc = self._ffmpeg_proc
                    if proc is not None:
                        try:
                            proc.stdin.write(effected.tobytes())
                        except (BrokenPipeError, OSError):
                            logger.warning("FFmpeg pipe broken; restarting...")
                            with self._ffmpeg_lock:
                                self._ffmpeg_proc = None
                            break

                    # Timing: always deliver at _out_fps regardless of speed
                    now = time.monotonic()
                    elapsed = now - last_frame_time
                    sleep_time = frame_interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    last_frame_time = time.monotonic()

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
            "-r", str(self._out_fps),
            "-i", "pipe:0",
            *encoder_args,
            "-pix_fmt", "yuv420p",
            "-r", str(self._out_fps),
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
            return proc
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            return None
        except Exception as exc:
            logger.error("Failed to start FFmpeg: %s", exc)
            return None
