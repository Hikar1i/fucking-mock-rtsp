#!/usr/bin/env bash
# GPU-aware install helper for uv environments.
# Detects NVIDIA GPU via nvidia-smi and installs cupy-cuda12x when found.
set -euo pipefail

if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null 2>&1; then
    echo "[install] NVIDIA GPU detected — installing with GPU extras (cupy-cuda12x)..."
    uv sync --extra gpu
    echo "[install] Done. GPU acceleration (CuPy + PyTorch CUDA) is now available."
else
    echo "[install] No NVIDIA GPU detected — installing CPU-only dependencies."
    uv sync
    echo "[install] Done. PyTorch CUDA (if already in your environment) will still be used automatically."
fi
