"""Tapo C230 RTSP still-image file source for pydaq.

One scheduled call produces an image in data/tapo/YYYY/MM/ and a complete
copy in the flat outbox/tapo/ directory for the existing transfer handler.
Credentials are read from local files, as in mkndaq (never from shell exports).
Requires the ffmpeg executable on PATH, not the OpenCV Python package.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
from threading import Lock
import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from .instrument import Instrument, utc_timestamp_string


class TAPO(Instrument):
    HEADERS = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # The orchestrator already passes headers=None. Never supply it twice.
        kwargs.pop("headers", None)
        super().__init__(*args, headers=None, **kwargs)
        self._capture_lock = Lock()

    def request_reading(self) -> None:
        # pydaq owns the schedule and starts the worker; no private timer.
        self._task_queue.put(self.capture_snapshot)

    def get_record(self) -> dict[str, Any]:
        # No CSV/Parquet writer: snapshots are files, not tabular records.
        with self._state_lock:
            return dict(self.state.latest)

    @staticmethod
    def _secret_file(value: Any, label: str) -> str:
        if not value:
            raise ValueError(f"Missing io.{label} secret-file path")
        p = Path(str(value)).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Tapo {label} secret file not found: {p}")
        secret = p.read_text(encoding="utf-8").strip()
        if not secret:
            raise ValueError(f"Tapo {label} secret file is empty: {p}")
        return secret

    def capture_snapshot(self) -> Path | None:
        with self._state_lock:
            if not self.state.enabled:
                return None
        if not self._capture_lock.acquire(blocking=False):
            self.logger.warning("Tapo previous capture still running; skip this sample")
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
            # Never include an RTSP URL/ffmpeg stderr (which expose passwords).
            self.logger.error("Tapo snapshot failed: %s", str(exc))
            with self._state_lock:
                self.state.last_error = str(exc)
            return None
        finally:
            self._capture_lock.release()

    def _capture_once(self) -> Path:
        params = self.parameters or {}
        io = params.get("io") or {}
        init = params.get("init") or {}
        host = str(io.get("host") or "").strip()
        if not host or any(c in host for c in "/@ :"):
            raise ValueError("Invalid or missing Tapo io.host")
        port = int(io.get("port", 554))
        stream = int(init.get("stream", 1))
        if stream not in (1, 2):
            raise ValueError("Tapo stream must be 1 or 2")
        fmt = str(init.get("image_format", "png")).lower()
        if fmt not in ("png", "jpg", "jpeg"):
            raise ValueError("Tapo image_format must be png or jpg")
        ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
        timeout = max(5, int(io.get("timeout_seconds", 25)))
        user = self._secret_file(io.get("username"), "username")
        password = self._secret_file(io.get("password"), "password")
        executable = shutil.which(str(io.get("ffmpeg", "ffmpeg")))
        if not executable:
            raise FileNotFoundError("ffmpeg executable not found on PATH")

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
        url = (f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}"
               f"@{host}:{port}/stream{stream}")
        cmd = [
            executable, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-rw_timeout", str((timeout - 2) * 1_000_000),
            "-i", url, "-frames:v", "1",
        ]
        if ext == "jpg":
            cmd += ["-q:v", "2"]
        cmd += ["-y", str(temp_image)]
        try:
            try:
                result = subprocess.run(
                    cmd, capture_output=True, check=False, timeout=timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                # Do not log TimeoutExpired: its .cmd contains RTSP credentials.
                raise TimeoutError("Tapo RTSP capture timed out") from None
            if result.returncode != 0:
                # Do not log stderr: the RTSP URL can appear in FFmpeg errors.
                raise RuntimeError(f"ffmpeg failed (exit status {result.returncode})")
            if not temp_image.is_file() or temp_image.stat().st_size == 0:
                raise RuntimeError("ffmpeg returned no image")
            os.replace(temp_image, archive_file)
            shutil.copy2(archive_file, temp_copy)
            os.replace(temp_copy, outbox_dir / filename)
            return archive_file
        finally:
            temp_image.unlink(missing_ok=True)
            temp_copy.unlink(missing_ok=True)
