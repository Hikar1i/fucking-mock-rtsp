import ipaddress
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# Adapter name substrings (case-insensitive) treated as virtual / non-LAN
_VIRTUAL_PATTERNS = [
    "vmware", "vmnet", "virtual", "vethernet", "hyper-v", "hyper_v",
    "loopback", "bluetooth", "vpn", "tap", "tun", "docker",
    "wsl", "vbox", "virtualbox", "pseudo", "teredo", "isatap",
    "6to4", "tunnel", "ppoe", "pppoe",
]


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
    if sys.platform == "linux":
        try:
            result = subprocess.run(
                ["nvidia-smi"], capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return "h264_nvenc"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
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


def get_host_ips() -> list[str]:
    """Return real LAN IPv4 addresses (Ethernet/Wi-Fi), excluding virtual adapters."""
    if sys.platform == "win32":
        return _get_ips_windows()
    return _get_ips_unix()


def _is_virtual_adapter(name: str) -> bool:
    n = name.lower()
    return any(pat in n for pat in _VIRTUAL_PATTERNS)


def _is_valid_lan_ip(ip: str) -> bool:
    try:
        parsed = ipaddress.ip_address(ip)
        return (
            parsed.version == 4
            and parsed.is_private
            and not parsed.is_loopback
            and not parsed.is_link_local
        )
    except ValueError:
        return False


def _get_ips_windows() -> list[str]:
    """Parse `ipconfig` output to extract Ethernet/Wi-Fi IPv4 addresses."""
    try:
        output = subprocess.check_output(
            ["ipconfig"], encoding="utf-8", errors="ignore", timeout=5
        )
    except Exception:
        return _fallback_ips()
    ips: list[str] = []
    current_adapter = ""
    for line in output.splitlines():
        # Adapter header: no leading whitespace, ends with ":".
        # Works for both English ("Ethernet adapter ...") and
        # Simplified Chinese ("以太网适配器 ...") Windows.
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            current_adapter = line.rstrip()[:-1]
        elif "IPv4" in line and ":" in line:
            ip = line.split(":")[-1].strip()
            ip = ip.replace("(Preferred)", "").replace("(首选)", "").strip()
            if ip and _is_valid_lan_ip(ip) and not _is_virtual_adapter(current_adapter):
                if ip not in ips:
                    ips.append(ip)
    return ips or _fallback_ips()


def _get_ips_unix() -> list[str]:
    """Parse `ip addr show` output to extract Ethernet/Wi-Fi IPv4 addresses."""
    try:
        output = subprocess.check_output(
            ["ip", "addr", "show"], encoding="utf-8", errors="ignore", timeout=5
        )
        ips: list[str] = []
        current_iface = ""
        for line in output.splitlines():
            stripped = line.strip()
            if not line[0:1].isspace() and ":" in stripped:
                # Interface line: "2: eth0: <BROADCAST,...>"
                parts = stripped.split(":")
                if len(parts) >= 2:
                    current_iface = parts[1].strip()
            elif stripped.startswith("inet ") and "/" in stripped:
                ip = stripped.split()[1].split("/")[0]
                if _is_valid_lan_ip(ip) and not _is_virtual_adapter(current_iface):
                    if ip not in ips:
                        ips.append(ip)
        return ips or _fallback_ips()
    except Exception:
        return _fallback_ips()


def _fallback_ips() -> list[str]:
    """Last resort: probe a UDP route to 8.8.8.8 to find the primary outbound IP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        if _is_valid_lan_ip(ip):
            return [ip]
    except Exception:
        pass
    return []


def get_mediamtx_bin(cfg: dict[str, Any]) -> Path:
    bin_dir = Path(cfg["mediamtx"]["bin_dir"])
    if not bin_dir.is_absolute():
        bin_dir = _BASE_DIR / bin_dir
    name = "mediamtx.exe" if sys.platform == "win32" else "mediamtx"
    return bin_dir / name
