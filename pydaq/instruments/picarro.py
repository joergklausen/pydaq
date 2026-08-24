"""Picarro G2401 file-source driver.

The G2401 writes its own authoritative ``DataLog_User_Sync`` data files.  pydaq
must therefore *copy and stage those files*, not recreate the Picarro data from
socket queries.  The TCP ``_Meas_GetConc`` command is used only to provide a
compact operator status line on stdout.

This mirrors the proven mkndaq behaviour while fitting pydaq's file-source /
outbox architecture.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import socket
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Mapping

from pydaq.instruments.instrument import Instrument, utc_timestamp_string


class G2401(Instrument):
    """Picarro G2401 file-source instrument with a lightweight status query."""

    # The driver logs its own compact CO2/CH4/CO line.  Set print_every_seconds
    # to 0 in YAML so the generic ``latest={...}``/formatter path is not used.
    EMITS_OWN_STATUS = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Picarro files are authoritative; no pydaq CSV writer is wanted.
        kwargs.pop("headers", None)
        super().__init__(*args, headers=None, **kwargs)

        io = self._mapping(self.parameters.get("io", {}), "parameters.io")

        self.status_host = str(io.get("host", io.get("ip", ""))).strip()
        self.status_port = int(io.get("port", 0) or 0)
        self.status_timeout_seconds = max(
            0.1,
            float(io.get("timeout_seconds", io.get("timeout", 5.0))),
        )
        self.status_sleep_seconds = max(
            0.0,
            float(io.get("sleep_seconds", io.get("sleep", 0.5))),
        )

        source_path = str(io.get("source_path", "")).strip()
        netshare = str(io.get("netshare", "")).strip()
        if source_path:
            self.source_path = Path(source_path).expanduser()
        elif self.status_host and netshare:
            # Legacy mkndaq-compatible fallback for a Picarro SMB share.
            self.source_path = Path(rf"\\{self.status_host}\{netshare}")
        else:
            raise ValueError(
                f"[{self.name}] Picarro configuration requires io.source_path "
                "(or io.host + io.netshare)"
            )

        self.file_pattern = str(io.get("file_pattern", "*.dat")).strip() or "*.dat"
        raw_buckets = io.get("buckets", "daily")
        self.buckets = "none" if raw_buckets is None else str(raw_buckets).strip().lower()
        if self.buckets not in {"daily", "monthly", "none"}:
            raise ValueError(
                f"[{self.name}] io.buckets must be one of daily, monthly, none"
            )

        self.days_to_sync = max(1, int(io.get("days_to_sync", 7)))
        self.min_file_age_seconds = max(
            0.0,
            float(io.get("min_file_age_seconds", io.get("min_age_seconds", 3600.0))),
        )
        self.file_scan_every_seconds = max(
            1.0,
            float(io.get("file_scan_every_seconds", 600.0)),
        )
        self.zip_files = bool(io.get("zip_files", True))
        self.max_state_entries = max(100, int(io.get("max_state_entries", 20000)))

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self.data_dir / ".picarro_file_state.json"
        self._scan_lock = Lock()
        self._next_file_scan_monotonic = 0.0
        self._last_file_error_signature = ""
        self._last_status_error_signature = ""

    @staticmethod
    def _mapping(value: Any, context: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{context} must be a mapping")
        return value

    # ------------------------------------------------------------------
    # Scheduler-facing behaviour
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """Immediately scan files and print one live status line at startup."""
        self._run_file_scan_if_due(force=True)
        self.print_status()

    def request_reading(self) -> None:
        """Queue a status poll plus a file scan when the scan interval is due."""
        self._task_queue.put(self.run_cycle)

    def append_record(self) -> None:
        """Compatibility hook: G2401 acquisition is file transfer, not CSV."""
        self.run_cycle()

    def run_cycle(self) -> None:
        """Perform periodic file housekeeping and emit the live status line."""
        with self._state_lock:
            if not self.state.enabled:
                return
        self._run_file_scan_if_due()
        self.print_status()

    def get_record(self) -> Dict[str, Any]:
        """Return file-transfer state for dashboards; no measurement is written."""
        with self._state_lock:
            return dict(self.state.latest)

    # ------------------------------------------------------------------
    # File acquisition/staging
    # ------------------------------------------------------------------
    def _run_file_scan_if_due(self, *, force: bool = False) -> list[Path]:
        now = time.monotonic()
        if not force and now < self._next_file_scan_monotonic:
            return []

        try:
            staged = self.sync_files()
        except Exception as exc:
            # Retry sooner than the normal scan interval, but do not let a file
            # access problem suppress the independent operator status query.
            self._next_file_scan_monotonic = now + min(60.0, self.file_scan_every_seconds)
            self._report_file_error(exc)
            return []

        self._next_file_scan_monotonic = now + self.file_scan_every_seconds
        self._clear_file_error()
        return staged

    def _candidate_roots(self, now: datetime) -> list[tuple[Path, Path]]:
        """Return ``(source_dir, relative_bucket)`` pairs to inspect."""
        if self.buckets == "none":
            return [(self.source_path, Path())]

        roots: list[tuple[Path, Path]] = []
        seen: set[str] = set()
        for day_offset in range(self.days_to_sync):
            dtm = now - timedelta(days=day_offset)
            if self.buckets == "daily":
                relative = Path(dtm.strftime("%Y")) / dtm.strftime("%m") / dtm.strftime("%d")
            else:  # monthly
                relative = Path(dtm.strftime("%Y")) / dtm.strftime("%m")
            key = relative.as_posix()
            if key in seen:
                continue
            seen.add(key)
            roots.append((self.source_path / relative, relative))
        return roots

    @staticmethod
    def _identity(relative_path: Path, path: Path) -> str:
        stat = path.stat()
        return f"{relative_path.as_posix()}|{stat.st_size}|{stat.st_mtime_ns}"

    def _read_state(self) -> dict[str, float]:
        if not self._state_file.exists():
            return {}
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
            raw = payload.get("files", {}) if isinstance(payload, dict) else {}
            if not isinstance(raw, dict):
                return {}
            return {str(key): float(value) for key, value in raw.items()}
        except Exception as exc:
            self.logger.warning(
                "[%s] ignoring unreadable Picarro state file %s: %s",
                self.name,
                self._state_file,
                exc,
            )
            return {}

    def _write_state(self, state: dict[str, float]) -> None:
        if len(state) > self.max_state_entries:
            newest = sorted(state.items(), key=lambda item: item[1], reverse=True)
            state = dict(newest[: self.max_state_entries])

        payload = {"version": 1, "files": state}
        temporary = self._state_file.with_name(f".{self._state_file.name}.part")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self._state_file)

    @staticmethod
    def _same_file(path: Path, source: Path) -> bool:
        if not path.exists():
            return False
        try:
            return path.stat().st_size == source.stat().st_size
        except OSError:
            return False

    def _archive_file(self, source: Path, relative_bucket: Path) -> Path:
        archive_dir = self.data_dir / relative_bucket
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / source.name

        if self._same_file(archive_path, source):
            return archive_path

        temporary = archive_dir / f".{source.name}.part"
        temporary.unlink(missing_ok=True)
        try:
            shutil.copy2(source, temporary)
            if temporary.stat().st_size != source.stat().st_size:
                raise IOError(
                    f"copied size mismatch for {source.name}: "
                    f"source={source.stat().st_size} copied={temporary.stat().st_size}"
                )
            os.replace(temporary, archive_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return archive_path

    def _stage_file(self, archive_path: Path) -> Path:
        incoming = self.outbox_dir / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)

        if self.zip_files:
            outbox_name = archive_path.with_suffix(".zip").name
            outbox_path = self.outbox_dir / outbox_name
            temporary = incoming / f".{outbox_name}.part"
            temporary.unlink(missing_ok=True)
            try:
                with zipfile.ZipFile(
                    temporary,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                ) as handle:
                    handle.write(archive_path, arcname=archive_path.name)
                os.replace(temporary, outbox_path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            return outbox_path

        outbox_path = self.outbox_dir / archive_path.name
        temporary = incoming / f".{archive_path.name}.part"
        temporary.unlink(missing_ok=True)
        try:
            shutil.copy2(archive_path, temporary)
            os.replace(temporary, outbox_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return outbox_path

    def sync_files(self, *, now: datetime | None = None) -> list[Path]:
        """Archive and stage all unseen completed Picarro data files.

        Files younger than ``min_file_age_seconds`` are intentionally skipped so
        pydaq never transfers the file the analyzer may still be writing.
        """
        with self._scan_lock:
            if not self.source_path.exists():
                raise FileNotFoundError(
                    f"Picarro data source is not accessible: {self.source_path}"
                )

            current = now or datetime.now(timezone.utc)
            now_epoch = current.timestamp()
            state = self._read_state()
            staged: list[Path] = []

            for source_dir, relative_bucket in self._candidate_roots(current):
                if not source_dir.exists():
                    self.logger.debug(
                        "[%s] Picarro source bucket not present: %s",
                        self.name,
                        source_dir,
                    )
                    continue

                candidates = sorted(
                    path
                    for path in source_dir.iterdir()
                    if path.is_file() and fnmatch.fnmatch(path.name, self.file_pattern)
                )
                for source in candidates:
                    age = now_epoch - source.stat().st_mtime
                    if age < self.min_file_age_seconds:
                        continue

                    relative_file = relative_bucket / source.name
                    identity = self._identity(relative_file, source)
                    if identity in state:
                        continue

                    archive_path = self._archive_file(source, relative_bucket)
                    outbox_path = self._stage_file(archive_path)
                    # Commit state only after both authoritative archive and
                    # transfer-ready outbox file exist.
                    state[identity] = time.time()
                    self._write_state(state)
                    staged.append(outbox_path)
                    self.logger.info(
                        "[%s] Picarro file ready file=%s outbox=%s",
                        self.name,
                        source.name,
                        outbox_path.name,
                    )

            with self._state_lock:
                self.state.latest = {
                    "dtm": utc_timestamp_string(),
                    "new_files": len(staged),
                    "files": [path.name for path in staged],
                }
                self.state.last_sample_ts = time.time()
                if not self._last_file_error_signature:
                    self.state.last_error = ""
                    self.state.last_error_reported = ""

            if staged:
                self.logger.info("[%s] Picarro scan new_files=%s", self.name, len(staged))
            else:
                self.logger.debug("[%s] Picarro scan no new files", self.name)
            return staged

    def _report_file_error(self, exc: Exception) -> None:
        signature = f"{type(exc).__name__}:{exc}"
        repeated = signature == self._last_file_error_signature
        self._last_file_error_signature = signature
        message = f"file source unavailable: {self.source_path} ({exc})"
        with self._state_lock:
            self.state.last_error = message
            self.state.last_error_reported = message
        if repeated:
            self.logger.debug("[%s] %s (repeated)", self.name, message, exc_info=True)
        else:
            self.logger.error(
                "[%s] %s",
                self.name,
                message,
                exc_info=True,
                extra={"console_compact": True},
            )

    def _clear_file_error(self) -> None:
        if self._last_file_error_signature:
            self.logger.info("[%s] Picarro file source recovered", self.name)
        self._last_file_error_signature = ""
        with self._state_lock:
            if self.state.last_error.startswith("file source unavailable:"):
                self.state.last_error = ""
                self.state.last_error_reported = ""

    # ------------------------------------------------------------------
    # Live operator status -- intentionally never persisted as data
    # ------------------------------------------------------------------
    def _query_status(self, command: str = "_Meas_GetConc") -> str:
        if not self.status_host or not self.status_port:
            raise ValueError(
                f"[{self.name}] io.host and io.port are required for Picarro status"
            )

        payload = f"{command}\r\n".encode("ascii")
        chunks: list[bytes] = []
        with socket.create_connection(
            (self.status_host, self.status_port),
            timeout=self.status_timeout_seconds,
        ) as sock:
            sock.settimeout(self.status_timeout_seconds)
            sock.sendall(payload)
            if self.status_sleep_seconds:
                time.sleep(self.status_sleep_seconds)
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break

        response = b"".join(chunks).decode("ascii", errors="ignore")
        response = response.replace("\x00", "").strip()
        if "\n" in response:
            response = response.split("\n", 1)[0].strip()
        if not response:
            raise TimeoutError("empty response to _Meas_GetConc")
        return response

    @staticmethod
    def parse_concentrations(response: str) -> tuple[float, float, float]:
        """Parse the first CO2, CH4 and CO fields returned by ``_Meas_GetConc``."""
        fields = [field.strip() for field in response.strip().split(";")]
        if len(fields) < 3 or any(field == "" for field in fields[:3]):
            raise ValueError(f"unexpected _Meas_GetConc response: {response!r}")
        try:
            return float(fields[0]), float(fields[1]), float(fields[2])
        except ValueError as exc:
            raise ValueError(f"unexpected _Meas_GetConc response: {response!r}") from exc

    @staticmethod
    def _fmt(value: float) -> str:
        return f"{value:.4g}"

    def print_status(self) -> None:
        """Query instantaneous concentrations solely for the operator display."""
        try:
            co2, ch4, co = self.parse_concentrations(self._query_status())
        except Exception as exc:
            signature = f"{type(exc).__name__}:{exc}"
            repeated = signature == self._last_status_error_signature
            self._last_status_error_signature = signature
            message = (
                f"status unavailable tcp {self.status_host}:{self.status_port}: "
                f"{type(exc).__name__}: {exc}"
            )
            if repeated:
                self.logger.debug("[%s] %s (repeated)", self.name, message)
            else:
                self.logger.warning("[%s] %s", self.name, message)
            return

        if self._last_status_error_signature:
            self.logger.info("[%s] Picarro status recovered", self.name)
        self._last_status_error_signature = ""
        self.logger.info(
            "[%s] CO2=%s ppm CH4=%s ppm CO=%s ppm",
            self.name,
            self._fmt(co2),
            self._fmt(ch4),
            self._fmt(co),
        )
