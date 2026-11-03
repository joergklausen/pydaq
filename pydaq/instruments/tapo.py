"""Tapo C230 RTSP still-image acquisition for pydaq (OpenCV).

Each scheduled capture opens the RTSP stream over TCP, reads a frame, and closes
it. An authoritative image goes to data/tapo/YYYY/MM/; an atomic copy goes to
the flat outbox/tapo/ directory for pydaq's existing transfer handler.
Camera Account credentials are read from local secret files, as in mkndaq.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
from threading import Lock
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import cv2

from .instrument import Instrument, utc_timestamp_string


class TAPO(Instrument):
    HEADERS = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # The orchestrator already supplies headers=None.
        kwargs.pop("headers", None)
        super().__init__(*args, headers=None, **kwargs)
        self._capture_lock = Lock()

    def request_reading(self) -> None:
        """Queue a snapshot acquisition."""
        self._task_queue.put(self._capture_task)

    def _capture_task(self) -> None:
        """Worker-compatible wrapper for snapshot acquisition."""
        self.capture_snapshot()
        def get_record(self) -> dict[str, Any]:
            with self._state_lock:
                return dict(self.state.latest)

    @staticmethod
    def _secret_file(value: Any, label: str) -> str:
        if not value:
            raise ValueError(f"Missing io.{label} secret-file path")
        path = Path(str(value)).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Tapo {label} secret file not found: {path}")
        secret = path.read_text(encoding="utf-8").strip()
        if not secret:
            raise ValueError(f"Tapo {label} secret file is empty: {path}")
        return secret

    @staticmethod
    def _open_stream(url: str, timeout_seconds: float):
        """Open FFmpeg-backed OpenCV RTSP with TCP and open/read timeouts."""
        # OpenCV's Python wheel bundles its FFmpeg video backend; no `ffmpeg`
        # command-line executable is needed. Options must be set before opening.
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        timeout_ms = max(1000, int(timeout_seconds * 1000))
        params = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms,
        ]
        try:
            return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
        except (TypeError, cv2.error):
            # Older OpenCV builds do not accept constructor-only parameters.
            # The TCP transport option above still applies.
            return cv2.VideoCapture(url, cv2.CAP_FFMPEG)

    @classmethod
    def _read_frame(cls, url: str, *, timeout_seconds: float,
                    retries: int, warmup_reads: int):
        """Return the last valid frame; release the stream on every attempt."""
        for attempt in range(max(1, retries)):
            capture = None
            try:
                capture = cls._open_stream(url, timeout_seconds)
                if capture is not None and capture.isOpened():
                    frame = None
                    for _ in range(max(1, warmup_reads)):
                        ok, candidate = capture.read()
                        if not ok or candidate is None:
                            break
                        frame = candidate
                    if frame is not None:
                        return frame
            except cv2.error:
                # Avoid logging OpenCV exceptions potentially containing RTSP URLs.
                pass
            finally:
                if capture is not None:
                    capture.release()
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 5))
        raise RuntimeError("Tapo RTSP capture failed (camera/credentials/network/stream)")

    def capture_snapshot(self) -> Path | None:
        with self._state_lock:
            if not self.state.enabled:
                return None
        if not self._capture_lock.acquire(blocking=False):
            self.logger.warning("Tapo previous capture still running; skipping")
            return None
        try:
            archive = self._capture_once()
            with self._state_lock:
                self.state.last_sample_ts = time.time()
                self.state.last_error = ""
                self.state.latest = {
                    "dtm": utc_timestamp_string(),
                    "file": archive.name,
                    "bytes": archive.stat().st_size,
                }
            self.logger.info("Tapo snapshot archived and staged: %s", archive.name)
            return archive
        except Exception as exc:
            # Never log a complete RTSP URL or the camera account password.
            message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError,
                                                   RuntimeError)) else type(exc).__name__
            self.logger.error("Tapo snapshot failed: %s", message)
            with self._state_lock:
                self.state.last_error = message
            return None
        finally:
            self._capture_lock.release()

    def _capture_once(self) -> Path:
        params = self.parameters or {}
        io = params.get("io") or {}
        init = params.get("init") or {}
        host = str(io.get("host") or "").strip()
        if not host or any(char in host for char in "/@ :"):
            raise ValueError("Invalid or missing Tapo io.host")
        port = int(io.get("port", 554))
        stream = int(init.get("stream", 1))
        if stream not in (1, 2):
            raise ValueError("Tapo stream must be 1 or 2")
        fmt = str(init.get("image_format", "png")).lower()
        if fmt not in ("png", "jpg", "jpeg"):
            raise ValueError("Tapo image_format must be png or jpg")
        ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
        timeout_seconds = max(1.0, float(io.get("timeout_seconds", 25)))
        retries = max(1, int(io.get("retries", 3)))
        warmup_reads = max(1, int(init.get("warmup_reads", 3)))
        user = self._secret_file(io.get("username"), "username")
        password = self._secret_file(io.get("password"), "password")
        rtsp_url = (f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}"
                    f"@{host}:{port}/stream{stream}")

        frame = self._read_frame(
            rtsp_url, timeout_seconds=timeout_seconds,
            retries=retries, warmup_reads=warmup_reads,
        )
        stamp = datetime.now(timezone.utc)
        filename = f"{self.name}-{stamp:%Y%m%dT%H%M%SZ}.{ext}"
        archive_dir = Path(self.data_dir) / f"{stamp.year:04d}" / f"{stamp.month:02d}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / filename
        outbox_dir = Path(self.outbox_dir)
        outbox_dir.mkdir(parents=True, exist_ok=True)
        incoming = outbox_dir / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        temp_image = archive_dir / f".{filename}.{uuid4().hex}.{ext}"
        temp_copy = incoming / f".{filename}.{uuid4().hex}.partial"
        if ext == "jpg":
            quality = max(0, min(100, int(init.get("jpeg_quality", 95))))
            encode_options = [cv2.IMWRITE_JPEG_QUALITY, quality]
        else:
            compression = max(0, min(9, int(init.get("png_compression", 3))))
            encode_options = [cv2.IMWRITE_PNG_COMPRESSION, compression]
        try:
            if not cv2.imwrite(str(temp_image), frame, encode_options):
                raise RuntimeError("Could not encode Tapo image")
            if not temp_image.is_file() or temp_image.stat().st_size == 0:
                raise RuntimeError("Tapo image file is empty")
            os.replace(temp_image, archive_file)
            shutil.copy2(archive_file, temp_copy)
            os.replace(temp_copy, outbox_dir / filename)
            return archive_file
        finally:
            temp_image.unlink(missing_ok=True)
            temp_copy.unlink(missing_ok=True)
