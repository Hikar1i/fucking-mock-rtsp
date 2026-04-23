#!/usr/bin/env python3
"""
Test script: verify RTSP source connectivity and stream validity.

Usage:
    python test_rtsp_probe.py [rtsp_url]

Default URL: rtsp://admin:kd123456@192.168.100.14:554
"""

import re
import socket
import sys
import threading
import time
from urllib.parse import urlparse


def mask_url(url: str) -> str:
    return re.sub(r"(rtsp://)[^:@/]+:[^:@/]+@", r"\1***:***@", url, flags=re.IGNORECASE)


def _hr(char: str = "─", width: int = 60) -> str:
    return char * width


def step(n: int, total: int, label: str) -> None:
    print(f"\n[{n}/{total}] {label}")


def ok(msg: str = "") -> None:
    print(f"      ✓  {msg}" if msg else "      ✓  OK")


def fail(msg: str = "") -> None:
    print(f"      ✗  {msg}" if msg else "      ✗  FAILED")


# ── Step 1: TCP connectivity ──────────────────────────────────────────────────

def test_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    step(1, 3, f"TCP connectivity  →  {host}:{port}  (timeout {timeout:.0f}s)")
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        ok(f"Port {port} is reachable on {host}")
        return True
    except socket.timeout:
        fail(f"Connection timed out after {timeout:.0f}s")
    except ConnectionRefusedError:
        fail("Connection refused — RTSP server may not be running")
    except OSError as e:
        fail(f"Network error: {e}")
    return False


# ── Step 2: Open RTSP stream ──────────────────────────────────────────────────

def test_open(url: str, timeout: float = 10.0):
    """Returns cv2.VideoCapture on success, None on failure."""
    try:
        import cv2
    except ImportError:
        fail("opencv-python not installed — run: pip install opencv-python")
        return None

    step(2, 3, f"Opening RTSP stream  (timeout {timeout:.0f}s)")
    print(f"       URL: {mask_url(url)}")

    result: list = [None]
    error: list = [None]

    def _try_open() -> None:
        try:
            cap = cv2.VideoCapture()
            if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(timeout * 1000))
            if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            opened = cap.open(url, cv2.CAP_FFMPEG)
            if opened and cap.isOpened():
                result[0] = cap
            else:
                cap.release()
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_try_open, daemon=True)
    t.start()
    t.join(timeout=timeout + 3.0)

    if t.is_alive():
        fail(f"Timed out after {timeout:.0f}s — stream did not respond")
        return None
    if error[0] is not None:
        fail(f"Exception: {error[0]}")
        return None
    if result[0] is None:
        fail("cv2.VideoCapture.open() returned False — check URL / credentials")
        return None

    import cv2
    cap = result[0]
    w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    ok(f"Stream opened — reported resolution: {w}x{h} @ {fps:.2f} fps")
    return cap


# ── Step 3: Read frames ───────────────────────────────────────────────────────

def test_frames(cap, target: int = 5, deadline: float = 15.0) -> bool:
    step(3, 3, f"Reading {target} frames  (deadline {deadline:.0f}s)")

    got = 0
    failed_reads = 0
    t0 = time.monotonic()

    while got < target and (time.monotonic() - t0) < deadline:
        ret, frame = cap.read()
        if ret and frame is not None and frame.size > 0:
            got += 1
            elapsed = time.monotonic() - t0
            print(f"       frame {got}/{target}: shape={frame.shape}  t={elapsed:.2f}s")
        else:
            failed_reads += 1
            if failed_reads > 30:
                fail(f"Too many consecutive read failures ({failed_reads})")
                return False
            time.sleep(0.05)

    elapsed = time.monotonic() - t0
    actual_fps = got / elapsed if elapsed > 0 else 0

    if got >= target:
        ok(f"{got} frames in {elapsed:.2f}s  (~{actual_fps:.1f} fps actual)")
        return True
    else:
        fail(f"Only received {got}/{target} frames in {elapsed:.2f}s")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "rtsp://admin:kd123456@192.168.100.14:554"

    print(_hr("═"))
    print("  RTSP Probe Test")
    print(f"  Target : {mask_url(url)}")
    print(_hr("═"))

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 554

    if not host:
        print("\n✗  Could not parse host from URL. Aborting.")
        sys.exit(1)

    # Step 1
    if not test_tcp(host, port):
        print(f"\n{_hr()}")
        print("✗  TCP check failed. Likely causes:")
        print("   • Camera is powered off or unreachable on the network")
        print("   • Firewall blocking port 554")
        print("   • Wrong IP address")
        sys.exit(1)

    # Step 2
    cap = test_open(url)
    if cap is None:
        print(f"\n{_hr()}")
        print("✗  RTSP stream open failed. Likely causes:")
        print("   • Wrong credentials (username / password)")
        print("   • Wrong RTSP path (try appending /stream1 or /h264)")
        print("   • Camera RTSP server not enabled")
        sys.exit(1)

    # Step 3
    ok3 = test_frames(cap)
    cap.release()

    print(f"\n{_hr()}")
    if ok3:
        print("✓  All tests passed — RTSP stream is accessible and delivering frames.")
        print("   You can now enable rtsp_proxy in config.yaml.")
    else:
        print("✗  Stream opened but frame delivery failed.")
        print("   The camera may be pushing an unsupported codec.")
        sys.exit(1)


if __name__ == "__main__":
    main()
