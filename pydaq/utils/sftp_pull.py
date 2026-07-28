"""Reusable, strict SSH/SFTP support for inbound file acquisition.

This module is deliberately separate from the outbound transfer target.  Inbound
sources list and download remote files, while outbound targets upload staged
files.  Keeping the two roles separate makes logs and failures attributable to
one endpoint and prevents accidental reuse of upload-specific behaviour.
"""

from __future__ import annotations

import importlib
import posixpath
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


@dataclass(frozen=True, order=True)
class RemoteFile:
    """Metadata required to identify and archive one remote file."""

    mtime: int
    name: str
    size: int

    @property
    def identity(self) -> str:
        """Return a stable identity that detects same-name replacements."""

        return f"{self.name}\0{self.size}\0{self.mtime}"

    def as_state_entry(self) -> dict[str, int | str]:
        """Return the JSON-serializable state representation."""

        return {"name": self.name, "size": self.size, "mtime": self.mtime}


class SftpPullSession:
    """Open SFTP session scoped to one configured remote directory."""

    def __init__(self, sftp: Any, remote_dir: str) -> None:
        self._sftp = sftp
        self.remote_dir = remote_dir

    def list_files(
        self,
        pattern: str,
        *,
        min_age_seconds: float = 0.0,
        now: float | None = None,
    ) -> list[RemoteFile]:
        """List regular-looking files matching a shell-style filename pattern."""

        import fnmatch

        current_time = time.time() if now is None else float(now)
        entries: list[RemoteFile] = []
        for attr in self._sftp.listdir_attr(self.remote_dir):
            name = str(getattr(attr, "filename", ""))
            if not name or posixpath.basename(name) != name:
                continue
            if not fnmatch.fnmatch(name, pattern):
                continue

            mode = getattr(attr, "st_mode", None)
            if mode is not None and not stat.S_ISREG(int(mode)):
                continue

            mtime = int(getattr(attr, "st_mtime", 0) or 0)
            size = int(getattr(attr, "st_size", 0) or 0)
            if min_age_seconds > 0 and (current_time - mtime) < min_age_seconds:
                continue
            entries.append(RemoteFile(mtime=mtime, name=name, size=size))

        entries.sort()
        return entries

    def download(self, remote_file: RemoteFile, local_path: Path) -> None:
        """Download one file to the exact local path supplied by the caller."""

        remote_path = posixpath.join(self.remote_dir, remote_file.name)
        self._sftp.get(remote_path, str(local_path))


class SftpPullClient:
    """Open short-lived, host-key-verified SFTP sessions for file pulling."""

    def __init__(self, config: Mapping[str, Any], logger) -> None:
        self.logger = logger
        self.purpose = str(config.get("purpose", "inbound-file-pull"))
        self.host = str(config.get("host", "")).strip()
        self.port = int(config.get("port", 22))
        self.user = str(config.get("user", config.get("usr", ""))).strip()
        self.remote_dir = str(
            config.get("remote_dir", config.get("remote_path", config.get("source", "/")))
        ).strip()

        key_value = str(config.get("key", config.get("key_filename", ""))).strip()
        self.key_path = Path(key_value).expanduser() if key_value else None

        password_file_value = str(config.get("password_file", "")).strip()
        self.password_file = Path(password_file_value).expanduser() if password_file_value else None

        known_hosts_value = str(config.get("known_hosts", "~/.ssh/known_hosts")).strip()
        self.known_hosts = Path(known_hosts_value).expanduser() if known_hosts_value else None
        self.accept_unknown_host_key = bool(config.get("accept_unknown_host_key", False))

        common_timeout = float(config.get("timeout_seconds", 15.0))
        self.connect_timeout = float(config.get("connect_timeout_seconds", common_timeout))
        self.banner_timeout = float(config.get("banner_timeout_seconds", common_timeout))
        self.auth_timeout = float(config.get("auth_timeout_seconds", common_timeout))
        self.allow_agent = bool(config.get("allow_agent", False))
        self.look_for_keys = bool(config.get("look_for_keys", False))

        if not self.host:
            raise ValueError("inbound SFTP config requires io.host")
        if not self.user:
            raise ValueError("inbound SFTP config requires io.user")
        if not self.remote_dir:
            raise ValueError("inbound SFTP config requires io.remote_dir")
        if self.key_path is None and self.password_file is None and not self.allow_agent:
            raise ValueError(
                "inbound SFTP config requires io.key, io.password_file, or io.allow_agent=true"
            )

    def _read_password(self) -> str | None:
        if self.password_file is None:
            return None
        try:
            return self.password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise OSError(f"could not read SSH password file {self.password_file}: {exc}") from exc

    def _paramiko(self):
        try:
            return importlib.import_module("paramiko")
        except Exception as exc:  # pragma: no cover - depends on optional dependency
            raise RuntimeError(
                "paramiko is required for io.kind=sftp; install pydaq[sftp] or paramiko"
            ) from exc

    @contextmanager
    def open(self) -> Iterator[SftpPullSession]:
        """Open one SSH/SFTP connection and close it reliably."""

        paramiko = self._paramiko()
        client = paramiko.SSHClient()
        client.load_system_host_keys()

        if self.known_hosts and self.known_hosts.is_file():
            client.load_host_keys(str(self.known_hosts))

        if self.accept_unknown_host_key:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.logger.warning(
                "SSH host-key verification disabled purpose=%s host=%s port=%s",
                self.purpose,
                self.host,
                self.port,
            )
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        key_filename: str | None = None
        if self.key_path is not None:
            if not self.key_path.is_file():
                raise FileNotFoundError(f"SSH private key not found: {self.key_path}")
            key_filename = str(self.key_path)

        self.logger.info(
            "SSH connect purpose=%s host=%s port=%s user=%s remote_dir=%s",
            self.purpose,
            self.host,
            self.port,
            self.user,
            self.remote_dir,
        )

        sftp = None
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                key_filename=key_filename,
                password=self._read_password(),
                allow_agent=self.allow_agent,
                look_for_keys=self.look_for_keys,
                timeout=self.connect_timeout,
                banner_timeout=self.banner_timeout,
                auth_timeout=self.auth_timeout,
            )
            sftp = client.open_sftp()
            yield SftpPullSession(sftp, self.remote_dir)
        except Exception as exc:
            self.logger.error(
                "SSH/SFTP failure purpose=%s host=%s port=%s user=%s error_type=%s error=%s",
                self.purpose,
                self.host,
                self.port,
                self.user,
                type(exc).__name__,
                exc,
            )
            raise
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
            try:
                client.close()
            except Exception:
                pass
