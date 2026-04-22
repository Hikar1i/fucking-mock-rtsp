"""Per-frame image effect processor (noise, blur, occlusion, brightness/contrast, color)."""

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class EffectConfig:
    all_enabled: bool = False
    black_screen: bool = False

    noise_enabled: bool = False
    noise_gaussian_sigma: float = 20.0
    noise_sp_prob: float = 0.03

    blur_enabled: bool = False
    blur_type: str = "gaussian"      # "gaussian" | "motion"
    blur_kernel: int = 7             # 1-31, odd
    blur_motion_angle: float = 0.0
    blur_motion_length: int = 15

    occlusion_enabled: bool = False
    occlusion_count: int = 3
    occlusion_max_size: int = 80
    occlusion_colors: list = field(default_factory=lambda: ["black"])
    occlusion_refresh_sec: float = 2.0

    brightness_enabled: bool = False
    brightness_value: float = 0.0    # -100 ~ +100

    contrast_enabled: bool = False
    contrast_value: float = 1.0      # 0.3 ~ 3.0

    color_enabled: bool = False
    color_hue_shift: float = 0.0     # -180 ~ +180
    color_saturation: float = 1.0    # 0.0 ~ 2.0

    preview_enabled: bool = False    # show effects in MJPEG preview


class EffectProcessor:
    """Thread-safe per-frame effect processor. Instantiate one per stream."""

    def __init__(self) -> None:
        self._cfg = EffectConfig()
        self._lock = threading.Lock()
        self._occlusion_blocks: list[tuple] = []
        self._last_occlusion_refresh: float = 0.0
        self._saved_cfg: EffectConfig | None = None   # snapshot before all_enabled=False
        self._generation: int = 0

    # ---------------------------------------------------------------------- config

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            c = self._cfg
            return {
                "all_enabled": c.all_enabled,
                "black_screen": c.black_screen,
                "noise_enabled": c.noise_enabled,
                "noise_gaussian_sigma": c.noise_gaussian_sigma,
                "noise_sp_prob": c.noise_sp_prob,
                "blur_enabled": c.blur_enabled,
                "blur_type": c.blur_type,
                "blur_kernel": c.blur_kernel,
                "blur_motion_angle": c.blur_motion_angle,
                "blur_motion_length": c.blur_motion_length,
                "occlusion_enabled": c.occlusion_enabled,
                "occlusion_count": c.occlusion_count,
                "occlusion_max_size": c.occlusion_max_size,
                "occlusion_colors": list(c.occlusion_colors),
                "occlusion_refresh_sec": c.occlusion_refresh_sec,
                "brightness_enabled": c.brightness_enabled,
                "brightness_value": c.brightness_value,
                "contrast_enabled": c.contrast_enabled,
                "contrast_value": c.contrast_value,
                "color_enabled": c.color_enabled,
                "color_hue_shift": c.color_hue_shift,
                "color_saturation": c.color_saturation,
                "preview_enabled": c.preview_enabled,
            }

    def update_config(self, data: dict[str, Any]) -> None:
        with self._lock:
            c = self._cfg
            # When toggling all_enabled OFF, save current config
            if "all_enabled" in data and not data["all_enabled"] and c.all_enabled:
                self._saved_cfg = copy.copy(c)
            # When toggling all_enabled ON, restore if we have a save
            if "all_enabled" in data and data["all_enabled"] and not c.all_enabled:
                if self._saved_cfg is not None:
                    current_preview = c.preview_enabled
                    restored = copy.copy(self._saved_cfg)
                    restored.all_enabled = True
                    restored.preview_enabled = current_preview
                    self._cfg = restored
                    self._generation += 1
                    return

            for k, v in data.items():
                if hasattr(c, k):
                    setattr(c, k, v)
            self._generation += 1

    def get_generation(self) -> int:
        """Return a counter incremented on every config change; used for stale-queue detection."""
        with self._lock:
            return self._generation

    @property
    def preview_active(self) -> bool:
        """True when effects should be visible in the MJPEG preview."""
        with self._lock:
            return self._cfg.all_enabled and self._cfg.preview_enabled

    # ---------------------------------------------------------------------- apply

    def apply(self, frame: np.ndarray) -> np.ndarray:
        with self._lock:
            cfg = copy.copy(self._cfg)
            cfg.occlusion_colors = list(self._cfg.occlusion_colors)

        if not cfg.all_enabled:
            return frame

        if cfg.black_screen:
            return np.zeros_like(frame)

        if cfg.noise_enabled:
            frame = self._apply_noise(frame, cfg)
        if cfg.blur_enabled:
            frame = self._apply_blur(frame, cfg)
        if cfg.brightness_enabled or cfg.contrast_enabled:
            frame = self._apply_brightness_contrast(frame, cfg)
        if cfg.color_enabled:
            frame = self._apply_color(frame, cfg)
        if cfg.occlusion_enabled:
            frame = self._apply_occlusion(frame, cfg)

        return frame

    # ---------------------------------------------------------------------- internals

    @staticmethod
    def _apply_noise(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        h, w = frame.shape[:2]
        # Gaussian noise
        if cfg.noise_gaussian_sigma > 0:
            noise = np.random.normal(0, cfg.noise_gaussian_sigma, (h, w, 3)).astype(np.int16)
            frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        # Salt & pepper
        if cfg.noise_sp_prob > 0:
            prob = cfg.noise_sp_prob
            rng = np.random.random((h, w))
            frame = frame.copy()
            frame[rng < prob / 2] = 0
            frame[rng > 1 - prob / 2] = 255
        return frame

    @staticmethod
    def _apply_blur(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        k = max(1, cfg.blur_kernel)
        if k % 2 == 0:
            k += 1
        if cfg.blur_type == "motion":
            length = max(1, cfg.blur_motion_length)
            angle = cfg.blur_motion_angle
            kernel = np.zeros((length, length))
            center = length // 2
            import math
            rad = math.radians(angle)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            for i in range(length):
                x = int(round(center + (i - center) * cos_a))
                y = int(round(center + (i - center) * sin_a))
                if 0 <= x < length and 0 <= y < length:
                    kernel[y, x] = 1.0
            s = kernel.sum()
            if s > 0:
                kernel /= s
            return cv2.filter2D(frame, -1, kernel)
        return cv2.GaussianBlur(frame, (k, k), 0)

    @staticmethod
    def _apply_brightness_contrast(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        alpha = cfg.contrast_value if cfg.contrast_enabled else 1.0
        beta = cfg.brightness_value if cfg.brightness_enabled else 0.0
        return cv2.convertScaleAbs(frame, alpha=alpha, beta=beta)

    @staticmethod
    def _apply_color(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
        # Hue shift
        hsv[:, :, 0] = (hsv[:, :, 0] + cfg.color_hue_shift / 2.0) % 180.0
        # Saturation scale
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * cfg.color_saturation, 0, 255)
        hsv = hsv.astype(np.uint8)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    def _apply_occlusion(self, frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        now = time.monotonic()
        h, w = frame.shape[:2]
        if now - self._last_occlusion_refresh >= cfg.occlusion_refresh_sec or \
                len(self._occlusion_blocks) != cfg.occlusion_count:
            self._occlusion_blocks = []
            color_map = {
                "black": (0, 0, 0),
                "white": (255, 255, 255),
                "gray": (128, 128, 128),
            }
            colors = [color_map[c] for c in cfg.occlusion_colors if c in color_map] or [(0, 0, 0)]
            for _ in range(cfg.occlusion_count):
                bw = np.random.randint(10, max(11, cfg.occlusion_max_size))
                bh = np.random.randint(10, max(11, cfg.occlusion_max_size))
                x = np.random.randint(0, max(1, w - bw))
                y = np.random.randint(0, max(1, h - bh))
                col = colors[np.random.randint(0, len(colors))]
                self._occlusion_blocks.append((x, y, bw, bh, col))
            self._last_occlusion_refresh = now

        frame = frame.copy()
        for (x, y, bw, bh, col) in self._occlusion_blocks:
            frame[y:y + bh, x:x + bw] = col
        return frame
