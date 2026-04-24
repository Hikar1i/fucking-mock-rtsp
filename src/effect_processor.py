"""逐帧图像干扰效果处理器（噪点、模糊、遮挡块、亮度/对比度、颜色、马赛克、纯色蒙版）。"""

import copy
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from src import gpu_backend as _gpu

logger = logging.getLogger(__name__)


@dataclass
class EffectConfig:
    all_enabled: bool = False
    black_screen: bool = False

    noise_enabled: bool = False
    noise_gaussian_sigma: float = 20.0
    noise_sp_prob: float = 0.03

    blur_enabled: bool = False
    blur_type: str = "gaussian"      # "gaussian"=高斯 | "motion"=运动
    blur_kernel: int = 7             # 1-31，必须为奇数
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
    color_hue_shift: float = 0.0        # 色调偏移 -180 ~ +180
    color_saturation: float = 1.0       # 饱和度倍率 0.0 ~ 4.0
    color_temperature: float = 0.0      # 色温 -100（冷）~ +100（暖）
    color_invert: bool = False          # 反相
    color_brightness_bias: float = 0.5  # 明度偏置 0.0=全黑, 0.5=不变, 1.0=全白

    mosaic_enabled: bool = False
    mosaic_block_size: int = 16         # 马赛克块大小 2 ~ 64 px
    mosaic_blur_radius: int = 0         # 马赛克后叠加高斯模糊半径 0=不模糊

    overlay_enabled: bool = False
    overlay_r: int = 0                  # 蒙版颜色 R 通道 0~255
    overlay_g: int = 0
    overlay_b: int = 0
    overlay_opacity: float = 0.5        # 蒙版透明度 0.0~1.0

    flip_h: bool = False                # 水平翻转（摄像头镜像纠正用）

    preview_enabled: bool = False       # 是否在网页预览中叠加干扰效果


class EffectProcessor:
    """线程安全的逐帧效果处理器，每路流独立实例化。"""

    def __init__(self) -> None:
        self._cfg = EffectConfig()
        self._lock = threading.Lock()
        self._occlusion_blocks: list[tuple] = []
        self._last_occlusion_refresh: float = 0.0
        self._saved_cfg: EffectConfig | None = None   # 关闭总开关前的配置快照
        self._generation: int = 0

    # ------------------------------------------------------------------ 配置读写

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
                "color_temperature": c.color_temperature,
                "color_invert": c.color_invert,
                "color_brightness_bias": c.color_brightness_bias,
                "mosaic_enabled": c.mosaic_enabled,
                "mosaic_block_size": c.mosaic_block_size,
                "mosaic_blur_radius": c.mosaic_blur_radius,
                "overlay_enabled": c.overlay_enabled,
                "overlay_r": c.overlay_r,
                "overlay_g": c.overlay_g,
                "overlay_b": c.overlay_b,
                "overlay_opacity": c.overlay_opacity,
                "flip_h": c.flip_h,
                "preview_enabled": c.preview_enabled,
            }

    def update_config(self, data: dict[str, Any]) -> None:
        with self._lock:
            c = self._cfg
            # 关闭总开关时保存当前配置快照
            if "all_enabled" in data and not data["all_enabled"] and c.all_enabled:
                self._saved_cfg = copy.copy(c)
            # 打开总开关时恢复快照（如有）
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
        """返回配置变更计数器，用于检测队列中的过期帧。"""
        with self._lock:
            return self._generation

    @property
    def preview_active(self) -> bool:
        """网页预览中是否叠加干扰效果。"""
        with self._lock:
            return self._cfg.all_enabled and self._cfg.preview_enabled

    @property
    def flip_h(self) -> bool:
        """返回当前水平翻转状态（摄像头镜像纠正用）。"""
        with self._lock:
            return self._cfg.flip_h

    def apply_flip_only(self, frame: np.ndarray) -> np.ndarray:
        """仅应用水平翻转，忽略所有其他效果和总开关状态。"""
        with self._lock:
            flip = self._cfg.flip_h
        return cv2.flip(frame, 1) if flip else frame

    # ------------------------------------------------------------------ 帧处理入口

    def apply(self, frame: np.ndarray) -> np.ndarray:
        with self._lock:
            cfg = copy.copy(self._cfg)
            cfg.occlusion_colors = list(self._cfg.occlusion_colors)

        # 水平翻转独立于总开关，始终执行（摄像头镜像纠正）
        if cfg.flip_h:
            frame = cv2.flip(frame, 1)

        if not cfg.all_enabled:
            return frame

        if cfg.black_screen:
            return np.zeros_like(frame)

        # ── Unified CuPy pipeline: upload once → chain all ops on GPU → download once ──
        # Reduces PCIe transfers from (N_effects × 2) to 2 regardless of how many
        # effects are active. At 1024×576 this saves ~0.25 ms per extra effect;
        # benefit scales linearly with frame resolution.
        if _gpu.CUPY_AVAILABLE:
            cp = _gpu.cp
            gf = cp.asarray(frame)
            if cfg.noise_enabled:
                gf = self._gpu_noise(gf, cfg, cp)
            if cfg.blur_enabled:
                gf = self._gpu_blur(gf, cfg, cp)
            if cfg.brightness_enabled or cfg.contrast_enabled:
                gf = self._gpu_bc(gf, cfg, cp)
            if cfg.color_enabled:
                gf = self._gpu_color(gf, cfg, cp)
            if cfg.mosaic_enabled:
                gf = self._gpu_mosaic(gf, cfg, cp)
            frame = cp.asnumpy(gf)
            if cfg.occlusion_enabled:
                frame = self._apply_occlusion(frame, cfg)
            if cfg.overlay_enabled:
                frame = self._apply_overlay(frame, cfg)
            return frame

        # ── Per-effect path: PyTorch CUDA or CPU (each method handles GPU internally) ──
        if cfg.noise_enabled:
            frame = self._apply_noise(frame, cfg)
        if cfg.blur_enabled:
            frame = self._apply_blur(frame, cfg)
        if cfg.mosaic_enabled:
            frame = self._apply_mosaic(frame, cfg)
        if cfg.brightness_enabled or cfg.contrast_enabled:
            frame = self._apply_brightness_contrast(frame, cfg)
        if cfg.color_enabled:
            frame = self._apply_color(frame, cfg)
        if cfg.occlusion_enabled:
            frame = self._apply_occlusion(frame, cfg)
        if cfg.overlay_enabled:
            frame = self._apply_overlay(frame, cfg)

        return frame

    # ------------------------------------------------------------------ 各效果实现

    @staticmethod
    def _apply_noise(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        h, w = frame.shape[:2]
        if _gpu.CUPY_AVAILABLE:
            cp = _gpu.cp
            gpu = cp.asarray(frame)
            if cfg.noise_gaussian_sigma > 0:
                noise = cp.random.normal(
                    0, cfg.noise_gaussian_sigma, (h, w, 3), dtype=cp.float32
                )
                gpu = cp.clip(gpu.astype(cp.float32) + noise, 0, 255).astype(cp.uint8)
            if cfg.noise_sp_prob > 0:
                prob = cfg.noise_sp_prob
                rng = cp.random.random((h, w))
                gpu = gpu.copy()
                gpu[rng < prob / 2] = 0
                gpu[rng > 1 - prob / 2] = 255
            return cp.asnumpy(gpu)
        if _gpu.TORCH_CUDA_AVAILABLE:
            th = _gpu.torch
            gpu = th.as_tensor(frame).cuda()
            if cfg.noise_gaussian_sigma > 0:
                noise = th.empty(h, w, 3, dtype=th.float32, device="cuda")
                noise.normal_(0, cfg.noise_gaussian_sigma)
                gpu = th.clamp(gpu.float() + noise, 0, 255).to(th.uint8)
            if cfg.noise_sp_prob > 0:
                prob = cfg.noise_sp_prob
                rng = th.rand(h, w, device="cuda")
                gpu = gpu.clone()
                gpu[rng < prob / 2] = 0
                gpu[rng > 1 - prob / 2] = 255
            return gpu.cpu().numpy()
        # CPU fallback
        if cfg.noise_gaussian_sigma > 0:
            noise = np.random.normal(0, cfg.noise_gaussian_sigma, (h, w, 3)).astype(np.int16)
            frame = np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        # 椒盐噪点
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
        if k % 2 == 0:  # 确保卷积核为奇数
            k += 1
        if cfg.blur_type == "motion":
            # 运动模糊：生成方向性卷积核
            length = max(1, cfg.blur_motion_length)
            angle = cfg.blur_motion_angle
            kernel = np.zeros((length, length))
            center = length // 2
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
        if _gpu.OPENCV_CUDA_AVAILABLE:
            try:
                gpu_mat = cv2.cuda_GpuMat()
                gpu_mat.upload(frame)
                gauss = cv2.cuda.createGaussianFilter(
                    cv2.CV_8UC3, cv2.CV_8UC3, (k, k), 0
                )
                return gauss.apply(gpu_mat).download()
            except Exception:
                pass
        return cv2.GaussianBlur(frame, (k, k), 0)

    @staticmethod
    def _apply_brightness_contrast(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        alpha = cfg.contrast_value if cfg.contrast_enabled else 1.0
        beta = cfg.brightness_value if cfg.brightness_enabled else 0.0
        if _gpu.CUPY_AVAILABLE:
            cp = _gpu.cp
            gpu = cp.asarray(frame, dtype=cp.float32)
            return cp.asnumpy(cp.clip(cp.abs(gpu * alpha + beta), 0, 255).astype(cp.uint8))
        return cv2.convertScaleAbs(frame, alpha=alpha, beta=beta)

    @staticmethod
    def _apply_mosaic(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        h, w = frame.shape[:2]
        block = max(2, cfg.mosaic_block_size)
        small_w, small_h = max(1, w // block), max(1, h // block)
        if _gpu.OPENCV_CUDA_AVAILABLE:
            try:
                gm = cv2.cuda_GpuMat()
                gm.upload(frame)
                gm = cv2.cuda.resize(gm, (small_w, small_h), interpolation=cv2.INTER_NEAREST)
                gm = cv2.cuda.resize(gm, (w, h), interpolation=cv2.INTER_NEAREST)
                if cfg.mosaic_blur_radius > 0:
                    k = cfg.mosaic_blur_radius * 2 + 1
                    gauss = cv2.cuda.createGaussianFilter(cv2.CV_8UC3, cv2.CV_8UC3, (k, k), 0)
                    gm = gauss.apply(gm)
                return gm.download()
            except Exception:
                pass
        small = cv2.resize(frame, (small_w, small_h), interpolation=cv2.INTER_NEAREST)
        frame = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
        # 模糊：像素化后叠加高斯模糊，使块边缘柔和
        if cfg.mosaic_blur_radius > 0:
            k = cfg.mosaic_blur_radius * 2 + 1
            frame = cv2.GaussianBlur(frame, (k, k), 0)
        return frame

    @staticmethod
    def _apply_overlay(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        opacity = float(np.clip(cfg.overlay_opacity, 0.0, 1.0))
        if opacity <= 0.0:
            return frame
        color_layer = np.full_like(frame, (cfg.overlay_b, cfg.overlay_g, cfg.overlay_r), dtype=np.uint8)
        if opacity >= 1.0:
            return color_layer
        return cv2.addWeighted(frame, 1.0 - opacity, color_layer, opacity, 0)

    @staticmethod
    def _apply_color(frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        # ── 色相/饱和度：优先使用可用的最高级 GPU 路径 ──
        _hue_sat_done = False
        if _gpu.OPENCV_CUDA_AVAILABLE:
            try:
                gpu_bgr = cv2.cuda_GpuMat()
                gpu_bgr.upload(frame)
                gpu_hsv_mat = cv2.cuda.cvtColor(gpu_bgr, cv2.COLOR_BGR2HSV)
                hsv = gpu_hsv_mat.download().astype(np.float32)
                if _gpu.CUPY_AVAILABLE:
                    cp = _gpu.cp
                    h_gpu = cp.asarray(hsv)
                    h_gpu[:, :, 0] = (h_gpu[:, :, 0] + cfg.color_hue_shift / 2.0) % 180.0
                    h_gpu[:, :, 1] = cp.clip(h_gpu[:, :, 1] * cfg.color_saturation, 0, 255)
                    hsv = cp.asnumpy(h_gpu).astype(np.uint8)
                elif _gpu.TORCH_CUDA_AVAILABLE:
                    th = _gpu.torch
                    h_gpu = th.as_tensor(hsv).cuda()
                    h_gpu[:, :, 0] = (h_gpu[:, :, 0] + cfg.color_hue_shift / 2.0) % 180.0
                    h_gpu[:, :, 1] = th.clamp(
                        h_gpu[:, :, 1] * cfg.color_saturation, 0, 255
                    )
                    hsv = h_gpu.cpu().numpy().astype(np.uint8)
                else:
                    hsv[:, :, 0] = (hsv[:, :, 0] + cfg.color_hue_shift / 2.0) % 180.0
                    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * cfg.color_saturation, 0, 255)
                    hsv = hsv.astype(np.uint8)
                gpu_hsv2 = cv2.cuda_GpuMat()
                gpu_hsv2.upload(hsv)
                frame = cv2.cuda.cvtColor(gpu_hsv2, cv2.COLOR_HSV2BGR).download()
                _hue_sat_done = True
            except Exception:
                pass
        if not _hue_sat_done:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
            if _gpu.CUPY_AVAILABLE:
                cp = _gpu.cp
                h_gpu = cp.asarray(hsv)
                h_gpu[:, :, 0] = (h_gpu[:, :, 0] + cfg.color_hue_shift / 2.0) % 180.0
                h_gpu[:, :, 1] = cp.clip(h_gpu[:, :, 1] * cfg.color_saturation, 0, 255)
                frame = cv2.cvtColor(cp.asnumpy(h_gpu).astype(np.uint8), cv2.COLOR_HSV2BGR)
            elif _gpu.TORCH_CUDA_AVAILABLE:
                th = _gpu.torch
                h_gpu = th.as_tensor(hsv).cuda()
                h_gpu[:, :, 0] = (h_gpu[:, :, 0] + cfg.color_hue_shift / 2.0) % 180.0
                h_gpu[:, :, 1] = th.clamp(h_gpu[:, :, 1] * cfg.color_saturation, 0, 255)
                frame = cv2.cvtColor(h_gpu.cpu().numpy().astype(np.uint8), cv2.COLOR_HSV2BGR)
            else:
                # CPU fallback
                hsv[:, :, 0] = (hsv[:, :, 0] + cfg.color_hue_shift / 2.0) % 180.0
                # 饱和度缩放
                hsv[:, :, 1] = np.clip(hsv[:, :, 1] * cfg.color_saturation, 0, 255)
                frame = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        # ── 色温/明度偏置/反相：所有路径均执行 ──
        # 色温：正値偏暖（提亮加R降低B），负値偏冷（提亮加B降低R）
        if cfg.color_temperature != 0:
            t = float(cfg.color_temperature) * 1.5
            f = frame.astype(np.float32)
            f[:, :, 2] = np.clip(f[:, :, 2] + t, 0, 255)   # R 通道
            f[:, :, 0] = np.clip(f[:, :, 0] - t, 0, 255)   # B 通道
            frame = f.astype(np.uint8)
        # 明度偏置：<0.5 向黑色过渡，>0.5 向白色过渡
        b = float(cfg.color_brightness_bias)
        if b != 0.5:
            f = frame.astype(np.float32)
            if b < 0.5:
                f = f * (b * 2.0)
            else:
                t = (b - 0.5) * 2.0
                f = f + (255.0 - f) * t
            frame = np.clip(f, 0, 255).astype(np.uint8)
        # 反相
        if cfg.color_invert:
            frame = cv2.bitwise_not(frame)
        return frame

    # ── GPU-native (CuPy) effect methods ───────────────────────────────────────────
    # All accept and return a cp.ndarray; called only from the unified pipeline.

    @staticmethod
    def _gpu_noise(gf: np.ndarray, cfg: EffectConfig, cp) -> np.ndarray:
        h, w = gf.shape[:2]
        if cfg.noise_gaussian_sigma > 0:
            noise = cp.random.normal(0, cfg.noise_gaussian_sigma, (h, w, 3), dtype=cp.float32)
            gf = cp.clip(gf.astype(cp.float32) + noise, 0, 255).astype(cp.uint8)
        if cfg.noise_sp_prob > 0:
            rng = cp.random.random((h, w))
            gf = gf.copy()
            gf[rng < cfg.noise_sp_prob / 2] = 0
            gf[rng > 1 - cfg.noise_sp_prob / 2] = 255
        return gf

    @staticmethod
    def _gpu_blur(gf: np.ndarray, cfg: EffectConfig, cp) -> np.ndarray:
        k = max(1, cfg.blur_kernel)
        if k % 2 == 0:
            k += 1
        if cfg.blur_type == "motion":
            length = max(1, cfg.blur_motion_length)
            kernel_cpu = np.zeros((length, length), dtype=np.float32)
            center = length // 2
            rad = math.radians(cfg.blur_motion_angle)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            for i in range(length):
                x = int(round(center + (i - center) * cos_a))
                y = int(round(center + (i - center) * sin_a))
                if 0 <= x < length and 0 <= y < length:
                    kernel_cpu[y, x] = 1.0
            s = kernel_cpu.sum()
            if s > 0:
                kernel_cpu /= s
            frame_cpu = cp.asnumpy(gf)
            return cp.asarray(cv2.filter2D(frame_cpu, -1, kernel_cpu))
        # Gaussian blur via cupyx.scipy.ndimage — stays entirely on GPU
        try:
            from cupyx.scipy import ndimage as cpnd
            sigma = max(0.5, (k - 1) / 6.0)
            gf_f = gf.astype(cp.float32)
            blurred = cpnd.gaussian_filter(gf_f, sigma=[sigma, sigma, 0])
            return cp.clip(blurred, 0, 255).astype(cp.uint8)
        except Exception:
            frame_cpu = cp.asnumpy(gf)
            return cp.asarray(cv2.GaussianBlur(frame_cpu, (k, k), 0))

    @staticmethod
    def _gpu_bc(gf: np.ndarray, cfg: EffectConfig, cp) -> np.ndarray:
        alpha = cfg.contrast_value if cfg.contrast_enabled else 1.0
        beta  = cfg.brightness_value if cfg.brightness_enabled else 0.0
        return cp.clip(cp.abs(gf.astype(cp.float32) * alpha + beta), 0, 255).astype(cp.uint8)

    @staticmethod
    def _gpu_mosaic(gf, cfg, cp):
        """马赛克像素化 + 可选高斯模糊，完全在 CuPy GPU 上执行，无需下载回 CPU。"""
        h, w = gf.shape[:2]
        block = max(2, cfg.mosaic_block_size)
        # 广播索引实现最近邻像素化（等价于 INTER_NEAREST 缩小再放大）
        row_src = cp.minimum((cp.arange(h) // block) * block, h - 1)
        col_src = cp.minimum((cp.arange(w) // block) * block, w - 1)
        gf = gf[row_src[:, None], col_src[None, :], :]
        # 可选：像素化后叠加高斯模糊使块边缘柔和
        if cfg.mosaic_blur_radius > 0:
            try:
                from cupyx.scipy import ndimage as cpnd
                sigma = float(cfg.mosaic_blur_radius)
                blurred = cpnd.gaussian_filter(gf.astype(cp.float32), sigma=[sigma, sigma, 0])
                gf = cp.clip(blurred, 0, 255).astype(cp.uint8)
            except Exception:
                frame_cpu = cp.asnumpy(gf)
                k = cfg.mosaic_blur_radius * 2 + 1
                gf = cp.asarray(cv2.GaussianBlur(frame_cpu, (k, k), 0))
        return gf

    @staticmethod
    def _gpu_color(gf: np.ndarray, cfg: EffectConfig, cp) -> np.ndarray:
        """BGR → HSV → apply hue/saturation → HSV → BGR, entirely in CuPy."""
        f = gf.astype(cp.float32) / 255.0
        b, g, r = f[:, :, 0], f[:, :, 1], f[:, :, 2]

        v     = cp.maximum(cp.maximum(r, g), b)
        min_c = cp.minimum(cp.minimum(r, g), b)
        delta = v - min_c

        s = cp.where(v > 1e-7, delta / v, cp.zeros_like(v))

        h = cp.zeros_like(v)
        eps = 1e-7
        mask_r = (v == r) & (delta > eps)
        mask_g = (v == g) & ~mask_r & (delta > eps)
        mask_b = ~mask_r & ~mask_g & (delta > eps)
        safe_d = delta + 1e-10
        h = cp.where(mask_r, 60.0 * ((g - b) / safe_d % 6.0), h)
        h = cp.where(mask_g, 60.0 * ((b - r) / safe_d + 2.0), h)
        h = cp.where(mask_b, 60.0 * ((r - g) / safe_d + 4.0), h)
        h = cp.where(h < 0, h + 360.0, h)

        h = (h + cfg.color_hue_shift) % 360.0
        s = cp.clip(s * cfg.color_saturation, 0.0, 1.0)

        h6     = h / 60.0
        hi     = cp.floor(h6).astype(cp.int32) % 6
        f_frac = h6 - cp.floor(h6)
        p = v * (1.0 - s)
        q = v * (1.0 - s * f_frac)
        t = v * (1.0 - s * (1.0 - f_frac))

        conds = [hi == i for i in range(6)]
        out_r = cp.select(conds, [v, q, p, p, t, v])
        out_g = cp.select(conds, [t, v, v, q, p, p])
        out_b = cp.select(conds, [p, p, t, v, v, q])

        result = cp.stack([out_b, out_g, out_r], axis=2)
        result = cp.clip(result * 255.0, 0.0, 255.0).astype(cp.uint8)
        # 色温：正値偏暖（提亮加R降低B），负値偏冷（提亮加B降低R）
        if cfg.color_temperature != 0:
            t = float(cfg.color_temperature) * 1.5
            rf = result.astype(cp.float32)
            rf[:, :, 2] = cp.clip(rf[:, :, 2] + t, 0, 255)
            rf[:, :, 0] = cp.clip(rf[:, :, 0] - t, 0, 255)
            result = rf.astype(cp.uint8)
        # 明度偏置：<0.5 向黑色过渡，>0.5 向白色过渡
        b = float(cfg.color_brightness_bias)
        if b != 0.5:
            rf = result.astype(cp.float32)
            if b < 0.5:
                rf = rf * (b * 2.0)
            else:
                t2 = (b - 0.5) * 2.0
                rf = rf + (255.0 - rf) * t2
            result = cp.clip(rf, 0, 255).astype(cp.uint8)
        # 反相
        if cfg.color_invert:
            result = 255 - result
        return result

    def _apply_occlusion(self, frame: np.ndarray, cfg: EffectConfig) -> np.ndarray:
        now = time.monotonic()
        h, w = frame.shape[:2]
        # 到达刷新间隔或块数变化时重新随机生成遮挡块位置
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
