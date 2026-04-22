"""MediaMTX RTSP server manager: auto-download and lifecycle management."""

import logging
import platform
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)

_GITHUB_RELEASE_BASE = (
    "https://github.com/bluenviron/mediamtx/releases/download"
)

_PLATFORM_MAP = {
    ("linux", "x86_64"): "linux_amd64",
    ("linux", "aarch64"): "linux_arm64v8",
    ("linux", "armv7l"): "linux_armv7",
    ("darwin", "x86_64"): "darwin_amd64",
    ("darwin", "arm64"): "darwin_arm64",
    ("windows", "AMD64"): "windows_amd64",
    ("windows", "x86"): "windows_386",
}


def _get_platform_tag() -> str:
    os_name = sys.platform
    if os_name.startswith("linux"):
        os_name = "linux"
    elif os_name == "darwin":
        os_name = "darwin"
    elif os_name == "win32":
        os_name = "windows"
    machine = platform.machine()
    key = (os_name, machine)
    tag = _PLATFORM_MAP.get(key)
    if tag is None:
        raise RuntimeError(f"Unsupported platform: {key}")
    return tag


def _get_archive_name(version: str, tag: str) -> str:
    ext = "zip" if sys.platform == "win32" else "tar.gz"
    return f"mediamtx_{version}_{tag}.{ext}"


def _fetch_latest_version() -> str:
    api_url = "https://api.github.com/repos/bluenviron/mediamtx/releases/latest"
    try:
        resp = requests.get(api_url, timeout=10)
        resp.raise_for_status()
        return resp.json()["tag_name"]
    except Exception as exc:
        raise RuntimeError(f"Cannot fetch latest MediaMTX version: {exc}") from exc


def download_mediamtx(cfg: dict[str, Any]) -> Path:
    """Download MediaMTX binary if not already present. Returns the binary path."""
    from src.config import get_mediamtx_bin

    bin_path = get_mediamtx_bin(cfg)
    if bin_path.exists():
        logger.info("MediaMTX binary found: %s", bin_path)
        return bin_path

    version = cfg["mediamtx"]["version"]
    timeout = cfg["mediamtx"]["download_timeout"]
    tag = _get_platform_tag()
    bin_path.parent.mkdir(parents=True, exist_ok=True)

    def _try_download(ver: str) -> bool:
        archive_name = _get_archive_name(ver, tag)
        url = f"{_GITHUB_RELEASE_BASE}/{ver}/{archive_name}"
        archive_path = bin_path.parent / archive_name
        logger.info("Downloading MediaMTX %s from %s ...", ver, url)
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            with open(archive_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            logger.info("Extracting %s ...", archive_path)
            _extract_archive(archive_path, bin_path.parent)
            archive_path.unlink(missing_ok=True)
            return True
        except Exception as exc:
            logger.warning("Download attempt for %s failed: %s", ver, exc)
            archive_path.unlink(missing_ok=True)
            return False

    if not _try_download(version):
        logger.warning("Configured version %s not found; fetching latest...", version)
        latest = _fetch_latest_version()
        logger.info("Latest MediaMTX version: %s", latest)
        if not _try_download(latest):
            raise RuntimeError(f"Failed to download MediaMTX (tried {version} and {latest})")

    if not bin_path.exists():
        raise RuntimeError(f"MediaMTX binary not found after extraction: {bin_path}")

    if sys.platform != "win32":
        bin_path.chmod(bin_path.stat().st_mode | 0o111)

    logger.info("MediaMTX ready: %s", bin_path)
    return bin_path


def _extract_archive(archive_path: Path, dest: Path) -> None:
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest)
    else:
        import tarfile
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(dest)


def _build_mediamtx_config(cfg: dict[str, Any]) -> str:
    rtsp_port = cfg["server"]["rtsp_port"]
    webcam_path = cfg["webcam"]["rtsp_path"].lstrip("/")
    video_path = cfg["video"]["rtsp_path"].lstrip("/")
    allowed_ips: list[str] = cfg["server"].get("allowed_ips", []) or []
    # publishIPs always localhost-only; readIPs extended when whitelist is active
    _pub_ips = '["127.0.0.1", "::1"]'
    if allowed_ips:
        _read_set = list(dict.fromkeys(allowed_ips + ["127.0.0.1", "::1"]))
        _read_ips = "[" + ", ".join(f'"{ip}"' for ip in _read_set) + "]"
        _path_cfg = (
            f"    publishIPs: {_pub_ips}\n"
            f"    readIPs: {_read_ips}"
        )
    else:
        _path_cfg = f"    publishIPs: {_pub_ips}"

    lines = [
        f"rtspAddress: :{rtsp_port}",
        "logLevel: warn",
        "logDestinations: [stdout]",
        "readTimeout: 10s",
        "writeTimeout: 10s",
        "writeQueueSize: 2048",
        # Disable unused protocols to avoid port conflicts (8888/8889/1935/8890)
        "hls: no",
        "webrtc: no",
        "rtmp: no",
        "srt: no",
        "paths:",
        f"  {webcam_path}:",
        _path_cfg,
        f"  {video_path}:",
        _path_cfg,
    ]
    return "\n".join(lines) + "\n"


class MediaMTXManager:
    def __init__(self, cfg: dict[str, Any]):
        self._cfg = cfg
        self._proc: subprocess.Popen | None = None
        self._config_file: Path | None = None

    def start(self) -> None:
        bin_path = download_mediamtx(self._cfg)
        base_dir = Path(self._cfg["_base_dir"])
        self._config_file = base_dir / "bin" / "mediamtx_runtime.yml"
        self._config_file.parent.mkdir(parents=True, exist_ok=True)
        self._config_file.write_text(
            _build_mediamtx_config(self._cfg), encoding="utf-8"
        )

        cmd = [str(bin_path), str(self._config_file)]
        logger.info("Starting MediaMTX: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        if self._proc.poll() is not None:
            raise RuntimeError("MediaMTX exited immediately after start")
        logger.info(
            "MediaMTX running (PID %d), RTSP on :%d",
            self._proc.pid,
            self._cfg["server"]["rtsp_port"],
        )

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            logger.info("Stopping MediaMTX (PID %d)...", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        if self._config_file and self._config_file.exists():
            self._config_file.unlink(missing_ok=True)

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
