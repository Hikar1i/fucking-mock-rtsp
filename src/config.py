import sys
from pathlib import Path
from typing import Any

import yaml


_BASE_DIR = Path(__file__).parent.parent


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    if config_path is None:
        config_path = _BASE_DIR / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_base_dir"] = str(_BASE_DIR)
    cfg["encoding"]["encoder"] = _resolve_encoder(cfg["encoding"]["encoder"])
    return cfg


def _resolve_encoder(encoder: str) -> str:
    if encoder != "auto":
        return encoder
    from src.gpu_backend import probe_nvenc
    if probe_nvenc():
        return "h264_nvenc"
    return "libx264"


def get_videos_dir(cfg: dict[str, Any]) -> Path:
    vdir = Path(cfg["video"]["videos_dir"])
    if not vdir.is_absolute():
        vdir = _BASE_DIR / vdir
    return vdir


def list_videos(cfg: dict[str, Any]) -> list[str]:
    vdir = get_videos_dir(cfg)
    if not vdir.exists():
        return []
    exts = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".flv"}
    return sorted(
        f.name for f in vdir.iterdir() if f.is_file() and f.suffix.lower() in exts
    )


def get_mediamtx_bin(cfg: dict[str, Any]) -> Path:
    bin_dir = Path(cfg["mediamtx"]["bin_dir"])
    if not bin_dir.is_absolute():
        bin_dir = _BASE_DIR / bin_dir
    name = "mediamtx.exe" if sys.platform == "win32" else "mediamtx"
    return bin_dir / name
