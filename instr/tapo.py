#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tapo camera support for mkndaq.

- Provides a Tapo class that can be instantiated from mkndaq.py
- Captures single JPEG snapshots to a staging folder.
- Optional CLI for standalone testing.

Config (mkndaq.yml)
-------------------
tapo:
    type: Tapo Camera
    ip_address: 192.168.x.y
    username: ~/.tapo/tapo-account-username
    password: ~/.tapo/tapo-account-password
    stream: 1                               # 1=HQ, 2=LQ
    image_format: jpg                       # jpg (default) or png
    jpeg_quality: 90                        # 0-100, higher=better quality (jpg
    png_compression: 3                      # 0-9, higher=smaller but slower (png)
    sampling_interval_seconds: 3600          # seconds between snapshots
    reporting_interval_minutes: 360          # minutes between transfer runs
    data_path: tapo
    staging_path: tapo
    staging_zip: False
    remote_path: tapo
"""

from __future__ import annotations

import argparse
import logging
import shutil
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

import cv2  # type: ignore[import]


def next_boundary(interval_s: int) -> float:
    """Seconds to sleep until the next wall-clock boundary for *interval_s*."""
    now = datetime.now()
    seconds = now.minute * 60 + now.second + now.microsecond / 1e6
    rem = seconds % interval_s
    return 0.0 if rem == 0 else interval_s - rem

class Tapo:
    """Simple wrapper around a Tapo RTSP camera producing PNG or JPEG snapshots."""

    def __init__(self, name: str, config: dict) -> None:
        self.name = name
        self.config = config

        # configure logging
        _logger = f"{Path(config['logging']['file']).stem}"
        self.logger = logging.getLogger(f"{_logger}.{__name__}")
        self.logger.info(f"[{self.name}] Initializing Tapo camera ...")
        
        # Prefer section [name], fall back to [tapo] for convenience
        section = config.get(name) or config.get("tapo")
        if section is None:
            raise ValueError(f"No configuration section [{self.name}] (or [tapo]) found in config")

        self.cfg = section

        # Connection and authentication
        self.ip_address: str = self.cfg["ip_address"]
        raw_username: str = self.cfg["username"]
        raw_password: str = self.cfg["password"]
        self.username: str = self._resolve_secret(raw_username)
        self.password: str = self._resolve_secret(raw_password)
        self.stream: int = int(self.cfg.get("stream", 1))

        # Image format and compression settings
        self.image_format: str = self.cfg.get("image_format", "jpg").lower()
        if self.image_format not in {"jpg", "jpeg", "png"}:
            self.logger.warning(
                f"[{self.name}] Unsupported image_format {'self.image_format'}; falling back to 'jpg'"
            )
            self.image_format = "jpg"

        self.jpeg_quality: int = int(self.cfg.get("jpeg_quality", 95))
        self.png_compression: int = int(self.cfg.get("png_compression", 3))

        # Timing configuration
        self.sampling_interval_seconds: int = int(
            self.cfg.get("sampling_interval_seconds", 3600)
        )
        # mkndaq's transfer clients expect an "interval" in minutes
        self.reporting_interval: int = int(
            self.cfg.get("reporting_interval_minutes", self.cfg.get("reporting_interval", 60))
        )

        # Paths: root / data / staging like other instruments
        root = Path(self.config["root"]).expanduser()
        data_root = root / self.config["data"]
        staging_root = root / self.config["staging"]

        data_rel = Path(self.cfg.get("data_path", self.name))
        staging_rel = Path(self.cfg.get("staging_path", data_rel))

        self.data_path: Path = data_root / data_rel
        self.data_path.mkdir(parents=True, exist_ok=True)

        self.staging_path: Path = staging_root / staging_rel
        self.staging_path.mkdir(parents=True, exist_ok=True)

        # Remote path used by S3/SFTP transfer setup in mkndaq.py
        self.remote_path: str = str(self.cfg.get("remote_path", staging_rel.as_posix()))

        # Whether files in staging should be zipped before transfer (not used here,
        # but kept for consistency with other instruments)
        self.staging_zip: bool = bool(self.cfg.get("staging_zip", False))

        # Construct RTSP URL
        self.rtsp_url: str = (
            f"rtsp://{self.username}:{self.password}@{self.ip_address}:554/stream{self.stream}"
        )

        self._video_capture: Optional[cv2.VideoCapture] = None
        self._retries: int = int(self.cfg.get("retries", 3))
        self._warmup_reads: int = int(self.cfg.get("warmup_reads", 3))

        safe_rtsp = self.rtsp_url.replace(self.password, "********")
        self.logger.info(
            f"[{self.name}] Initialized camera (rtsp={safe_rtsp}, sampling_interval={self.sampling_interval_seconds} seconds)",
            extra={"to_logfile": True},
        )

    def _resolve_secret(self, value: str) -> str:
        """Return *value* directly or, if it's a path to a file, read the file."""
        path = Path(value).expanduser()
        if path.exists() and path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                self.logger.warning("Could not read secret from %s; using literal value", path)
        return value

    # ------------------------------------------------------------------
    # Low-level RTSP helpers
    # ------------------------------------------------------------------
    def _capture_video(self) -> cv2.VideoCapture:
        """Create a new VideoCapture handle for this camera."""
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        # set() calls simply return False if unsupported—safe to ignore.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _grab_frame(self) -> Tuple[Optional[cv2.VideoCapture], Any]:
        """Try to grab a frame from the RTSP stream with a few retries."""
        video_capture = self._video_capture
        frame: Any = None

        for attempt in range(self._retries):
            if video_capture is None or not video_capture.isOpened():
                video_capture = self._capture_video()
                # Small delay to let the stream initialise
                time.sleep(0.5)

            ok = False
            # Warm-up / flush a few frames (helps some RTSP sources)
            for _ in range(self._warmup_reads):
                ok, frame = video_capture.read()
                if not ok:
                    break

            if ok and frame is not None:
                self._video_capture = video_capture
                return video_capture, frame

            # Reconnect with simple backoff
            if video_capture is not None:
                video_capture.release()
            time.sleep(min(2**attempt, 5))
            video_capture = None

        # On repeated failure, ensure handle cleared
        self._video_capture = None
        return None, None

    # ------------------------------------------------------------------
    # Public API used by mkndaq.py
    # ------------------------------------------------------------------
    def get_data(self) -> Optional[Path]:
        """Capture a single JPEG snapshot and write it to self.data_path and self.staging_path."""
        try:
            _, frame = self._grab_frame()
        except Exception:
            self.logger.exception(
                f"[{self.name}] Unexpected error while grabbing frame from camera"
            )
            return None

        if frame is None:
            self.logger.warning(f"[{self.name}] Could not read frame; will try again at next interval.")
            return None

        ts = datetime.now().strftime("%Y%m%d%H%M%S")

        # Decide file extension
        if self.image_format in ("jpg", "jpeg"):
            ext = "jpg"
        else:
            ext = "png"

        outfile = self.data_path / f"{self.name}-{ts}.{ext}"

        # OpenCV imwrite parameters
        params: list[int] = []
        if ext == "jpg":
            # 0–100, higher = better quality, larger files
            quality = max(0, min(self.jpeg_quality, 100))
            params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
        elif ext == "png":
            # 0–9, higher = smaller but slower
            comp = max(0, min(self.png_compression, 9))
            params = [int(cv2.IMWRITE_PNG_COMPRESSION), comp]

        ok: bool = cv2.imwrite(str(outfile), frame, params)  # type: ignore[arg-type]
        if not ok:
            self.logger.error(f"[{self.name}] Failed to write snapshot to {outfile}.")
            return None

        self.logger.info(f"[{self.name}] Saved Tapo snapshot to {outfile}", extra={"to_logfile": True})

        # Also copy to staging path
        shutil.copy(src=outfile, dst=self.staging_path / outfile.name)

        return outfile

    def close(self) -> None:
        """Release the underlying VideoCapture handle."""
        if self._video_capture is not None:
            self._video_capture.release()
            self._video_capture = None


# ----------------------------------------------------------------------
# Standalone CLI (optional, for testing)
# ----------------------------------------------------------------------
def _cli_main() -> None:
    ap = argparse.ArgumentParser(description="Tapo C230 snapshots via RTSP (standalone)")
    ap.add_argument("--ip", required=True, help="Camera IP address")
    ap.add_argument("--user", required=True, help="Tapo camera-account username")
    ap.add_argument("--password", required=True, help="Tapo camera-account password")
    ap.add_argument("--stream", type=int, choices=(1, 2), default=1, help="1=HQ, 2=LQ")
    ap.add_argument("--out", default="captures", help="Output folder")
    ap.add_argument(
        "--interval",
        type=int,
        default=600,
        help="Seconds between shots (default: 600 = 10 min)",
    )
    ap.add_argument(
        "--no-align",
        action="store_true",
        help="Disable wall-clock alignment (just sleep interval)",
    )
    ap.add_argument("--prefix", default="tapo", help="Filename prefix")
    args = ap.parse_args()

    outdir = Path(args.out).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)

    rtsp_url = f"rtsp://{args.user}:{args.password}@{args.ip}:554/stream{args.stream}"
    safe_rtsp = rtsp_url.replace(args.password, "********")
    print(f"[INFO] RTSP: {safe_rtsp}")
    print(f"[INFO] Captures will be written to {outdir}")

    stop = False

    def _stop(*_obj: object) -> None:
        nonlocal stop
        stop = True
        print("\n[INFO] Stopping...")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    video_capture: Optional[cv2.VideoCapture] = None

    # Align to boundary (e.g., 10-minute marks) unless disabled
    if not args.no_align:
        wait = next_boundary(args.interval)
        if wait > 0:
            print(f"[INFO] Waiting {wait:.1f}s to align to next boundary...")
            time.sleep(wait)

    try:
        while not stop:
            # Create/reuse capture and save one image
            if video_capture is None or not video_capture.isOpened():
                video_capture = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                video_capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            ok, frame = video_capture.read()
            if not ok or frame is None:
                print("[WARN] Capture failed.")
            else:
                ts = datetime.now().strftime("%Y%m%d%H%M%S")
                outfile = outdir / f"{args.prefix}-{ts}.jpg"
                cv2.imwrite(str(outfile), frame)
                print(f"[INFO] Captured to {outfile}")

            # Sleep to next tick
            if args.no_align:
                time.sleep(args.interval)
            else:
                time.sleep(max(0.0, next_boundary(args.interval)))
    finally:
        if video_capture is not None:
            video_capture.release()


if __name__ == "__main__":
    _cli_main()