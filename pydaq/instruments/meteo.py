"""Meteo bulletin file source for pydaq.

The remote linuxbox produces WMO bulletin files (normally ``VRXA00.*``).  This
file-source driver pulls completed files by SSH/SFTP, stores authoritative raw
copies below ``data/meteo/YYYY/MM/DD`` using the remote modification time in
UTC, and stages flat copies in ``outbox/meteo`` for pydaq's existing outbound
transfer handler.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, ContextManager, Dict, Mapping, Protocol

from pydaq.instruments.instrument import Instrument, utc_timestamp_string
from pydaq.utils.sftp_pull import RemoteFile, SftpPullClient


class PullSession(Protocol):
    """Structural interface used by the bulletin driver and test doubles."""

    def list_files(
        self,
        pattern: str,
        *,
        min_age_seconds: float = 0.0,
    ) -> list[RemoteFile]: ...

    def download(self, remote_file: RemoteFile, local_path: Path) -> None: ...


class PullClient(Protocol):
    """Structural interface for opening an inbound file-pull session."""

    def open(self) -> ContextManager[PullSession]: ...


class METEO(Instrument):
    """Inbound SSH/SFTP file source for meteorological bulletin files."""

    def __init__(
        self,
        *args: Any,
        pull_client: PullClient | None = None,
        **kwargs: Any,
    ) -> None:
        # The orchestrator supplies headers=None.  Remove it before forcing the
        # writer off, otherwise Python receives the keyword twice.
        kwargs.pop("headers", None)
        super().__init__(*args, headers=None, **kwargs)

        io_config = self._mapping(self.parameters.get("io", {}), "parameters.io")
        kind = str(io_config.get("kind", "sftp")).strip().lower()
        if kind not in {"sftp", "ssh", "ssh_sftp"}:
            raise ValueError(
                f"[{self.name}] unsupported io.kind={kind!r}; expected 'sftp'"
            )

        self.pattern = str(
            io_config.get("pattern", io_config.get("file_pattern", "VRXA00.*"))
        ).strip()
        if not self.pattern:
            raise ValueError(f"[{self.name}] io.pattern must not be empty")

        self.min_remote_age_seconds = max(
            0.0, float(io_config.get("min_remote_age_seconds", 60.0))
        )
        self.retries = max(0, int(io_config.get("retries", 2)))
        self.backoff_seconds = max(0.0, float(io_config.get("backoff_seconds", 2.0)))
        self.max_state_entries = max(100, int(io_config.get("max_state_entries", 20000)))

        self._state_file = self.data_dir / ".meteo_fetch_state.json"
        self._pull_client = pull_client or SftpPullClient(io_config, self.logger)
        self._fetch_lock = Lock()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _mapping(value: Any, context: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{context} must be a mapping")
        return value

    # ------------------------------------------------------------------
    # Scheduler-facing behaviour
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """Fetch immediately at startup instead of waiting one full interval."""

        self.fetch_new_bulletins()

    def request_reading(self) -> None:
        """Queue a file-pull operation rather than a CSV record acquisition."""

        self._task_queue.put(self.fetch_new_bulletins)

    def append_record(self) -> None:
        """Compatibility hook: file sources fetch files rather than append CSV."""

        self.fetch_new_bulletins()

    def get_record(self) -> Dict[str, Any]:
        """Return the latest file-pull status; normal scheduling does not call this."""

        with self._state_lock:
            return dict(self.state.latest)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _read_state(self) -> tuple[dict[str, RemoteFile], set[str]]:
        if not self._state_file.exists():
            return {}, set()
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning(
                "could not read meteo state file=%s error=%s",
                self._state_file,
                exc,
            )
            return {}, set()

        identities: dict[str, RemoteFile] = {}
        legacy_names: set[str] = set()
        if isinstance(payload, dict):
            raw_legacy_names = payload.get("legacy_names", [])
            if isinstance(raw_legacy_names, list):
                legacy_names.update(str(name) for name in raw_legacy_names)
            raw_seen = payload.get("seen", [])
        else:
            raw_seen = []
        if not isinstance(raw_seen, list):
            return identities, legacy_names

        for entry in raw_seen:
            if isinstance(entry, str):
                # Compatibility with mkndaq's historical filename-only state.
                legacy_names.add(entry)
                continue
            if not isinstance(entry, dict):
                continue
            try:
                remote = RemoteFile(
                    mtime=int(entry["mtime"]),
                    name=str(entry["name"]),
                    size=int(entry["size"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            identities[remote.identity] = remote
        return identities, legacy_names

    def _write_state(
        self,
        identities: Mapping[str, RemoteFile],
        legacy_names: set[str],
    ) -> None:
        ordered = sorted(identities.values())[-self.max_state_entries :]
        payload: dict[str, Any] = {
            "version": 2,
            "seen": [entry.as_state_entry() for entry in ordered],
        }
        if legacy_names:
            payload["legacy_names"] = sorted(legacy_names)

        temporary = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._state_file)

    # ------------------------------------------------------------------
    # Filesystem operations
    # ------------------------------------------------------------------
    def _archive_path(self, remote_file: RemoteFile) -> Path:
        remote_datetime = datetime.fromtimestamp(remote_file.mtime, tz=timezone.utc)
        return (
            self.data_dir
            / remote_datetime.strftime("%Y")
            / remote_datetime.strftime("%m")
            / remote_datetime.strftime("%d")
            / remote_file.name
        )

    @staticmethod
    def _file_matches(path: Path, remote_file: RemoteFile) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        return stat.st_size == remote_file.size and int(stat.st_mtime) == remote_file.mtime

    def _download_to_archive(
        self,
        session: PullSession,
        remote_file: RemoteFile,
    ) -> Path:
        archive_path = self._archive_path(remote_file)
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        if self._file_matches(archive_path, remote_file):
            self.logger.info("archive already complete file=%s", archive_path)
            return archive_path

        temporary = archive_path.with_name(f".{archive_path.name}.part")
        temporary.unlink(missing_ok=True)
        try:
            session.download(remote_file, temporary)
            actual_size = temporary.stat().st_size
            if actual_size != remote_file.size:
                raise IOError(
                    f"downloaded size mismatch for {remote_file.name}: "
                    f"expected {remote_file.size}, got {actual_size}"
                )
            os.utime(temporary, (remote_file.mtime, remote_file.mtime))
            temporary.replace(archive_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return archive_path

    def _stage_archive(self, archive_path: Path) -> Path:
        outbox_path = self.outbox_dir / archive_path.name
        # TransferHandler scans only files directly under outbox/<instrument>.
        # Keep partial copies in a subdirectory so they can never be uploaded.
        incoming_dir = self.outbox_dir / ".incoming"
        incoming_dir.mkdir(parents=True, exist_ok=True)
        temporary = incoming_dir / f"{outbox_path.name}.part"
        temporary.unlink(missing_ok=True)
        try:
            shutil.copy2(archive_path, temporary)
            temporary.replace(outbox_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return outbox_path

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------
    def fetch_new_bulletins(self) -> list[Path]:
        """Pull, archive and stage all unseen completed bulletin files."""

        with self._fetch_lock:
            with self._state_lock:
                if not self.state.enabled:
                    return []

            identities, legacy_names = self._read_state()
            last_error: Exception | None = None

            for attempt in range(self.retries + 1):
                try:
                    staged = self._fetch_once(identities, legacy_names)
                    self._set_success_state(staged)
                    return staged
                except Exception as exc:
                    last_error = exc
                    if attempt >= self.retries:
                        break
                    delay = self.backoff_seconds * (2**attempt)
                    self.logger.warning(
                        "meteo pull failed attempt=%s/%s retry_in_seconds=%.1f "
                        "error_type=%s error=%s",
                        attempt + 1,
                        self.retries + 1,
                        delay,
                        type(exc).__name__,
                        exc,
                    )
                    if delay > 0:
                        time.sleep(delay)

            assert last_error is not None
            raise last_error

    def _fetch_once(
        self,
        identities: dict[str, RemoteFile],
        legacy_names: set[str],
    ) -> list[Path]:
        staged: list[Path] = []
        with self._pull_client.open() as session:
            candidates = session.list_files(
                self.pattern,
                min_age_seconds=self.min_remote_age_seconds,
            )
            self.logger.info(
                "meteo remote scan pattern=%s eligible_files=%s",
                self.pattern,
                len(candidates),
            )

            for remote_file in candidates:
                if remote_file.identity in identities:
                    continue

                if remote_file.name in legacy_names:
                    # Upgrade filename-only mkndaq state only when the matching
                    # authoritative archive is actually present.  A copied state
                    # file alone must not suppress the pydaq archive migration.
                    existing_archive = self._archive_path(remote_file)
                    legacy_names.remove(remote_file.name)
                    if self._file_matches(existing_archive, remote_file):
                        identities[remote_file.identity] = remote_file
                        self._write_state(identities, legacy_names)
                        continue

                archive_path = self._download_to_archive(session, remote_file)
                staged_path = self._stage_archive(archive_path)

                # State is committed only after both archive and outbox are complete.
                identities[remote_file.identity] = remote_file
                self._write_state(identities, legacy_names)
                staged.append(staged_path)
                self.logger.info(
                    "meteo bulletin ready remote=%s archive=%s outbox=%s size=%s mtime=%s",
                    remote_file.name,
                    archive_path,
                    staged_path,
                    remote_file.size,
                    remote_file.mtime,
                )
        return staged

    def _set_success_state(self, staged: list[Path]) -> None:
        timestamp = utc_timestamp_string()
        with self._state_lock:
            self.state.latest = {
                "dtm": timestamp,
                "new_files": len(staged),
                "files": [path.name for path in staged],
            }
            self.state.last_sample_ts = time.time()
            self.state.last_error = ""
        if staged:
            self.logger.info("meteo pull completed new_files=%s", len(staged))
        else:
            self.logger.info("meteo pull completed no new files")
