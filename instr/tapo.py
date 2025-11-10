#!/usr/bin/env python3
# Save a snapshot from a Tapo C230 via RTSP every N seconds (default 600 = 10 min)

import argparse
import signal
import time
from datetime import datetime
from pathlib import Path

import cv2  # pip install opencv-python


def next_boundary(interval_s: int) -> float:
    """Seconds to sleep until the next wall-clock boundary for interval_s."""
    now = datetime.now()
    seconds = now.minute * 60 + now.second + now.microsecond / 1e6
    rem = seconds % interval_s
    return 0.0 if rem == 0 else interval_s - rem


def make_capture(rtsp_url: str):
    # Use FFMPEG backend if available; small buffer to keep things snappy.
    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    # These set() calls simply return False if unsupported—safe to ignore.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def grab_frame(cap, rtsp_url: str, retries: int = 3, warmup_reads: int = 3):
    """Try to read a frame; reconnect a few times on failure."""
    for attempt in range(retries):
        if cap is None or not cap.isOpened():
            cap = make_capture(rtsp_url)
            time.sleep(0.5)

        ok, frame = False, None
        # Warmup/flush a few frames (helps some RTSP sources)
        for _ in range(warmup_reads):
            ok, frame = cap.read()
            if not ok:
                break

        if ok and frame is not None:
            return cap, frame

        # Reconnect backoff
        if cap:
            cap.release()
        time.sleep(min(2 ** attempt, 5))
        cap = None

    return None, None


def main():
    ap = argparse.ArgumentParser(description="Tapo C230 snapshots via RTSP")
    # You wrote "192.168.087." which likely means 192.168.0.87; set that as default.
    ap.add_argument("--ip", default="192.168.0.87", help="Camera IP address")
    ap.add_argument("--user", required=False, default="gawkenya", help="Tapo camera-account username")
    ap.add_argument("--password", required=False, default="jambo-mkn-tapo-cam-1", help="Tapo camera-account password")
    ap.add_argument("--stream", type=int, choices=(1, 2), default=1, help="1=HQ, 2=LQ")
    ap.add_argument("--out", default="captures", help="Output folder")
    ap.add_argument("--interval", type=int, default=10, help="Seconds between shots")
    ap.add_argument("--no-align", action="store_true",
                    help="Disable wall-clock alignment (just sleep interval)")
    ap.add_argument("--prefix", default="tapo", help="Filename prefix")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    rtsp = f"rtsp://{args.user}:{args.password}@{args.ip}:554/stream{args.stream}"
    safe_rtsp = rtsp.replace(args.password, "********")
    print(f"[INFO] RTSP: {safe_rtsp}")

    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True
        print("\n[INFO] Stopping...")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    cap = None

    # Align to boundary (e.g., 10-minute marks) unless disabled
    if not args.no_align:
        wait = next_boundary(args.interval)
        if wait > 0:
            print(f"[INFO] Waiting {wait:.1f}s to align to next boundary...")
            time.sleep(wait)

    while not stop:
        cap, frame = grab_frame(cap, rtsp)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if frame is None:
            print("[WARN] Could not read frame; will try again at next interval.")
        else:
            path = outdir / f"{args.prefix}_{ts}.jpg"
            ok = cv2.imwrite(str(path), frame)
            print(f"[{'OK' if ok else 'ERR'}] Saved {path if ok else 'write failed'}")

        # Sleep to next tick
        if args.no_align:
            time.sleep(args.interval)
        else:
            time.sleep(max(0.0, next_boundary(args.interval)))

    if cap:
        cap.release()


if __name__ == "__main__":
    main()
