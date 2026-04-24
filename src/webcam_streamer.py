"""本地摄像头 RTSP 推流器：捕获摄像头画面并通过 FFmpeg 推送至 MediaMTX。"""

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
    """检测指定设备ID的摄像头是否可用，可用返回 True。"""
    cap = cv2.VideoCapture(device_id, cv2.CAP_ANY)
    ok = cap.isOpened()
    cap.release()
    return ok


class WebcamStreamer:
    """本地摄像头采集器，RTSP 推流与网页 MJPEG 预览可独立开关。"""

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

        self._web_preview: bool = bool(cfg["webcam"].get("web_preview", True))

        # 预览管线：降分辨率 JPEG 队列 + 线程池编码
        self._preview_fps: int = max(1, self._fps // 2)
        self._preview_queue: queue.Queue = queue.Queue(maxsize=5)
        self._preview_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wcam-jpeg")
        self._last_preview_jpeg: bytes | None = None
        self._preview_jpeg_lock = threading.Lock()

        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_lock = threading.Lock()
        self._ffmpeg_write_queue: queue.Queue = queue.Queue(maxsize=4)
        self._ffmpeg_writer_thread: threading.Thread | None = None
        self._wcam_prev_gen: int = 0

        self.effects = EffectProcessor()
        self.effects._cfg.flip_h = True  # 摄像头默认纠正镜像方向（水平翻转）

    # ------------------------------------------------------------------ 公开接口

    @property
    def enabled(self) -> bool:
        """RTSP 推流是否激活。"""
        return self._streaming

    def enable(self) -> bool:
        """开启 RTSP 推流；若循环未启动则自动启动。"""
        if not self.available:
            return False
        self._streaming = True
        if not self._running:
            self._start_thread()
        return True

    def disable(self) -> None:
        """仅关闭 RTSP 推流，网页预览线程继续运行。"""
        self._streaming = False
        self._kill_ffmpeg()
        logger.info("摄像头 RTSP 推流已停止（预览保持运行）。")

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
        """返回最新预览帧的 JPEG 字节，若队列为空则返回上一帧缓存。"""
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
        """在线程池中执行：等比缩放加黑边 → JPEG 编码 → 入队。"""
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

    # ------------------------------------------------------------------ 内部实现

    def _start_thread(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._stream_loop, daemon=True, name="webcam-stream")
        self._running = True
        self._thread.start()
        logger.info("Webcam streamer started.")

    def _writer_loop(self, proc: subprocess.Popen) -> None:
        """专用写线程：从 _ffmpeg_write_queue 取帧数据并写入 FFmpeg stdin。"""
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
        # 通知写线程退出
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
            cap = cv2.VideoCapture(self._device_id, cv2.CAP_ANY)
            if not cap.isOpened():
                logger.error("Cannot open webcam device %d; retrying in 3s...", self._device_id)
                time.sleep(3)
                continue

            # 请求配置的分辨率，摄像头将使用最接近的支持模式
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or self._width)
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or self._height)
            logger.info("Webcam → RTSP: %dx%d @ %dfps (requested %dx%d)",
                        actual_w, actual_h, self._fps, self._width, self._height)

            try:
                while not self._stop_event.is_set():
                    # 推流开关：按需启动/终止 FFmpeg 进程
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

                    # 推流中则将效果帧写入 FFmpeg
                    with self._ffmpeg_lock:
                        proc = self._ffmpeg_proc
                    if proc is not None:
                        try:
                            self._ffmpeg_write_queue.put_nowait(effected.tobytes())
                        except queue.Full:
                            pass  # 队列满则丢帧，绝不阻塞主采集循环

                    # 网页预览：效果配置变更时清空旧帧队列，再提交新帧编码
                    cur_gen = self.effects.get_generation()
                    if cur_gen != self._wcam_prev_gen:
                        self._wcam_prev_gen = cur_gen
                        while not self._preview_queue.empty():
                            try:
                                self._preview_queue.get_nowait()
                            except queue.Empty:
                                break
                    # flip_h 始终体现在预览中；仅 preview_active 时叠加其他干扰效果
                    preview_frame = effected if self.effects.preview_active else self.effects.apply_flip_only(raw)
                    if self._web_preview:
                        self._preview_pool.submit(self._enqueue_preview, preview_frame.copy())

            finally:
                cap.release()
                self._kill_ffmpeg()

        with self._preview_jpeg_lock:
            self._last_preview_jpeg = None

    def _start_ffmpeg(self, width: int, height: int, rtsp_url: str) -> subprocess.Popen | None:  # noqa: E501
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
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # 重置写队列并启动专用写线程
            self._ffmpeg_write_queue = queue.Queue(maxsize=4)
            t = threading.Thread(target=self._writer_loop, args=(proc,),
                                 daemon=True, name="wcam-ffmpeg-wr")
            t.start()
            self._ffmpeg_writer_thread = t
            return proc
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH")
            return None
        except Exception as exc:
            logger.error("Failed to start FFmpeg: %s", exc)
            return None


def _encoder_args(encoder: str, preset: str, bitrate: str) -> list[str]:
    """根据编码器类型返回对应的 FFmpeg 编码参数。"""
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-b:v", bitrate]
    return ["-c:v", "libx264", "-preset", preset, "-tune", "zerolatency", "-b:v", bitrate]
