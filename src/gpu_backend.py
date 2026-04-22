"""GPU/CUDA capability detection — imported once at startup.

Priority order for array computation
-------------------------------------
1. CuPy  (if installed via ``uv sync --extra gpu`` or conda)
2. PyTorch CUDA  (available in the ``cu12-dev`` conda environment)
3. NumPy / CPU OpenCV  (always available — guaranteed fallback)

Exported flags
--------------
CUPY_AVAILABLE         : cupy is importable and a CUDA device responded
TORCH_CUDA_AVAILABLE   : torch.cuda.is_available() is True
OPENCV_CUDA_AVAILABLE  : OpenCV was compiled with CUDA and ≥1 device found

Exported modules
----------------
cp    : cupy module, or None when unavailable
torch : torch module, or None when unavailable
"""

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# ── module-level placeholders (replaced below if the backend is present) ──────
cp: Any = None
torch: Any = None

CUPY_AVAILABLE: bool = False
TORCH_CUDA_AVAILABLE: bool = False
OPENCV_CUDA_AVAILABLE: bool = False

# ── 1. CuPy ───────────────────────────────────────────────────────────────────
try:
    import cupy as _cupy
    _cupy.array([1], dtype=_cupy.uint8)  # force device init / validate GPU
    cp = _cupy
    CUPY_AVAILABLE = True
    logger.info("GPU backend: CuPy %s — noise/array ops will use GPU.", _cupy.__version__)
except Exception as _e:
    logger.debug("CuPy unavailable (%s); will probe PyTorch CUDA next.", _e)

# ── 2. PyTorch CUDA ───────────────────────────────────────────────────────────
try:
    import torch as _torch
    if _torch.cuda.is_available():
        _torch.zeros(1, device="cuda")  # force device init
        torch = _torch
        TORCH_CUDA_AVAILABLE = True
        _dev = _torch.cuda.get_device_name(0)
        logger.info(
            "GPU backend: PyTorch %s / CUDA %s — device: %s",
            _torch.__version__, _torch.version.cuda, _dev,
        )
    else:
        logger.debug("PyTorch present but CUDA unavailable; CPU path will be used.")
except Exception as _e:
    logger.debug("PyTorch CUDA probe failed (%s); CPU path will be used.", _e)

# ── 3. OpenCV CUDA ────────────────────────────────────────────────────────────
try:
    import cv2 as _cv2
    _n = _cv2.cuda.getCudaEnabledDeviceCount()
    OPENCV_CUDA_AVAILABLE = _n > 0
    if OPENCV_CUDA_AVAILABLE:
        logger.info("GPU backend: OpenCV CUDA — %d device(s) detected.", _n)
    else:
        logger.debug(
            "OpenCV built without CUDA or no device found; "
            "GPU blur/resize will be skipped."
        )
except Exception as _e:
    logger.debug("OpenCV CUDA probe failed (%s).", _e)


def probe_nvenc() -> bool:
    """Return True if NVIDIA GPU with working drivers is present (cross-platform)."""
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def gpu_summary() -> str:
    """Return a one-line human-readable summary of detected GPU backends."""
    parts = []
    if CUPY_AVAILABLE:
        parts.append("CuPy")
    if TORCH_CUDA_AVAILABLE and torch is not None:
        parts.append(f"PyTorch-CUDA({torch.cuda.get_device_name(0)})")
    if OPENCV_CUDA_AVAILABLE:
        parts.append("OpenCV-CUDA")
    return ", ".join(parts) if parts else "CPU-only"
