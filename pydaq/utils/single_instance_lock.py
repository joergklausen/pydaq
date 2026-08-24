"""Cross-platform single-instance protection for pydaq.

The lock is keyed by the canonical station configuration path.  The operating
system owns the actual lock, so a crash, forced termination, or reboot releases
it automatically; a stale metadata file is never treated as authoritative.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


class AlreadyRunningError(RuntimeError):
    """Raised when another pydaq process already owns the same config lock."""

    def __init__(self, config_path: Path, owner: dict[str, Any] | None = None) -> None:
        self.config_path = config_path
        self.owner = owner or {}
        super().__init__(self._message())

    def _message(self) -> str:
        details: list[str] = []
        pid = self.owner.get("pid")
        host = self.owner.get("host")
        started = self.owner.get("started")
        if pid is not None:
            details.append(f"pid={pid}")
        if host:
            details.append(f"host={host}")
        if started:
            details.append(f"started={started}")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"pydaq is already running for config {self.config_path}{suffix}"


def canonical_config_path(config_path: str | os.PathLike[str]) -> Path:
    """Return the stable path used to identify one pydaq station/config instance."""

    resolved = Path(config_path).expanduser().resolve(strict=False)
    # Windows paths are case-insensitive.  normcase prevents different spelling
    # of the same path from producing different mutex names.
    if os.name == "nt":
        return Path(os.path.normcase(str(resolved)))
    return resolved


def _lock_token(config_path: Path) -> str:
    identity = f"pydaq\0{config_path}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


class SingleInstanceLock:
    """Hold an OS-level lock for one canonical pydaq configuration path.

    Windows uses a named mutex. POSIX systems use ``fcntl.flock`` on a file
    descriptor that stays open for the lifetime of the lock holder.
    """

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, config_path: str | os.PathLike[str]) -> None:
        self.config_path = canonical_config_path(config_path)
        self.token = _lock_token(self.config_path)
        self._lock_dir = Path(tempfile.gettempdir()) / "pydaq"
        self._metadata_path = self._lock_dir / f"{self.token}.json"
        self._mutex_handle: object | None = None
        self._lock_file: IO[str] | None = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def metadata_path(self) -> Path:
        """Diagnostic metadata location; never the source of lock truth."""

        return self._metadata_path

    def acquire(self) -> None:
        """Acquire the lock without waiting, or raise :class:`AlreadyRunningError`."""

        if self._acquired:
            return

        self._lock_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            self._acquire_windows()
        else:
            self._acquire_posix()
        self._acquired = True

    def release(self) -> None:
        """Release the lock. Safe to call more than once."""

        if not self._acquired:
            return

        if os.name == "nt":
            # Remove metadata while we still own the mutex.  Once CloseHandle is
            # called another process may immediately acquire and write its own.
            try:
                self._metadata_path.unlink(missing_ok=True)
            except OSError:
                pass
            if self._mutex_handle is not None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel32.CloseHandle.restype = ctypes.c_int
                kernel32.CloseHandle(self._mutex_handle)
                self._mutex_handle = None
        else:
            if self._lock_file is not None:
                import fcntl

                try:
                    # Clear diagnostics before unlocking.  The file itself is
                    # intentionally retained; unlinking lock files can create
                    # inode races between old and new owners.
                    self._lock_file.seek(0)
                    self._lock_file.truncate()
                    self._lock_file.flush()
                except OSError:
                    pass
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
                self._lock_file = None

        self._acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def _owner_metadata(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config": str(self.config_path),
        }

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_metadata_file(self) -> None:
        temp_path = self._metadata_path.with_suffix(f".{os.getpid()}.tmp")
        temp_path.write_text(
            json.dumps(self._owner_metadata(), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, self._metadata_path)

    def _acquire_windows(self) -> None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # Global namespace prevents a second copy in another interactive session
        # from controlling the same station/configuration.
        mutex_name = f"Global\\pydaq-{self.token}"
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        error = ctypes.get_last_error()
        if error == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            owner = self._read_metadata(self._metadata_path)
            raise AlreadyRunningError(self.config_path, owner)

        self._mutex_handle = handle
        try:
            self._write_metadata_file()
        except Exception:
            kernel32.CloseHandle(handle)
            self._mutex_handle = None
            raise

    def _acquire_posix(self) -> None:
        import fcntl

        # On POSIX the lock file is also the metadata file.  The kernel lock on
        # its open descriptor, not the file's existence/content, is authoritative.
        lock_path = self._metadata_path
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.seek(0)
            try:
                owner = json.load(lock_file)
                if not isinstance(owner, dict):
                    owner = {}
            except (ValueError, OSError):
                owner = {}
            lock_file.close()
            raise AlreadyRunningError(self.config_path, owner) from None
        except Exception:
            lock_file.close()
            raise

        self._lock_file = lock_file
        try:
            lock_file.seek(0)
            lock_file.truncate()
            json.dump(self._owner_metadata(), lock_file, sort_keys=True)
            lock_file.flush()
            try:
                os.fsync(lock_file.fileno())
            except OSError:
                pass
        except Exception:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            self._lock_file = None
            raise
