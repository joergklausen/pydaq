"""Outbox transfer manager (S3 and SFTP).

This module uploads files that appear in ``outbox/<instrument>/``.

Design choices for PYDAQ:
- The outbox is organized **only by instrument** (flat, no year/month structure).
- Remote paths are also **only by instrument** (e.g. ``<remote_base>/<instrument>/<file>``).
- After a successful upload, files are removed from outbox. The authoritative local record remains in ``data/``.

Notes on typing/Pylance:
- boto3 is dynamically typed; some environments lack type stubs. To avoid Pylance errors like
  ``"session" is not a known attribute of module "boto3"``, this implementation imports
  ``Session`` directly: ``from boto3.session import Session``.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Union


@dataclass(frozen=True)
class TransferResult:
    """Result of a single upload attempt."""

    ok: bool
    target: str
    detail: str = ""


class TransferTarget:
    """Base interface for upload targets."""

    kind: str

    def upload(self, local_path: Path, remote_relative_path: str) -> TransferResult:
        """Upload one file.

        Args:
            local_path: Local file to upload.
            remote_relative_path: Relative path under the target base/prefix.

        Returns:
            A TransferResult indicating success and details.
        """
        raise NotImplementedError

    def exists(self, remote_relative_path: str) -> TransferResult:
        """Best-effort: verify that a remote object exists."""
        return TransferResult(False, self.kind, "exists() not implemented")

    def delete(self, remote_relative_path: str) -> TransferResult:
        """Best-effort: delete remote object (may fail depending on permissions)."""
        return TransferResult(False, self.kind, "delete() not implemented")


def _sleep_backoff(attempt: int, base_seconds: float, max_seconds: float) -> None:
    """Sleep using exponential backoff with jitter."""
    delay = min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))
    delay = delay * (0.8 + 0.4 * random.random())
    time.sleep(delay)


def _expand_secret(secret_or_path: Optional[str]) -> Optional[str]:
    """Expand secret string that may be a file path.

    If ``secret_or_path`` points to an existing file, its content is returned (stripped).
    Otherwise, the input is returned unchanged.

    Args:
        secret_or_path: Secret value or file path.

    Returns:
        The resolved secret value, or None.
    """
    if not secret_or_path:
        return None
    p = Path(secret_or_path).expanduser()
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            # Fall back to raw value if reading fails.
            return secret_or_path
    return secret_or_path


def _coerce_verify(value: Any) -> Union[bool, str]:
    """Convert verify configuration to a form accepted by botocore.

    - bool: True/False
    - str: path to CA bundle

    Args:
        value: Any config value.

    Returns:
        bool or str
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip()
        low = v.lower()
        if low in ("true", "1", "yes", "y"):
            return True
        if low in ("false", "0", "no", "n"):
            return False
        return v
    return True


class SftpTarget(TransferTarget):
    """SFTP upload target (requires ``paramiko``)."""

    kind = "sftp"

    def __init__(
        self,
        host: str,
        user: str,
        remote_base: str = ".",
        key: Optional[str] = None,
        port: int = 22,
        **kwargs: Any,
    ) -> None:
        """Create an SFTP target.

        Args:
            host: SFTP hostname.
            user: SSH username.
            remote_base: Remote directory root.
            key: Path to SSH private key (string path; will be expanded).
            port: SSH port.
        """
        self.host = host
        self.user = user
        self.remote_base = remote_base
        self.key = key
        self.port = port

    def upload(self, local_path: Path, remote_relative_path: str) -> TransferResult:
        """Upload using SFTP.

        Returns:
            TransferResult with remote path in ``detail`` on success.
        """
        try:
            import importlib
            paramiko = importlib.import_module("paramiko")
        except Exception as e:
            return TransferResult(False, self.kind, f"paramiko not available: {e}")

        remote_path = (PurePosixPath(self.remote_base) / remote_relative_path).as_posix()
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                key_filename=str(Path(self.key).expanduser()) if self.key else None,
                timeout=20,
            )
            sftp = client.open_sftp()

            # Ensure remote directories exist.
            pp = PurePosixPath(remote_path)
            cur = PurePosixPath("/") if remote_path.startswith("/") else PurePosixPath(".")
            for part in pp.parts[:-1]:
                if part in ("/", "."):
                    continue
                cur = cur / part
                try:
                    sftp.stat(cur.as_posix())
                except IOError:
                    try:
                        sftp.mkdir(cur.as_posix())
                    except Exception:
                        pass

            sftp.put(local_path.as_posix(), remote_path)
            sftp.close()
            client.close()
            return TransferResult(True, self.kind, remote_path)
        except Exception as e:
            return TransferResult(False, self.kind, str(e))

    def _remote_path(self, remote_relative_path: str) -> str:
        return (PurePosixPath(self.remote_base) / remote_relative_path).as_posix()

    def _open_sftp(self):
        try:
            import importlib
            paramiko = importlib.import_module("paramiko")
        except Exception as e:
            raise RuntimeError(f"paramiko not available: {e}") from e

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=str(Path(self.key).expanduser()) if self.key else None,
            timeout=20,
        )
        sftp = client.open_sftp()
        return client, sftp

    def exists(self, remote_relative_path: str) -> TransferResult:
        remote_path = self._remote_path(remote_relative_path)
        try:
            client, sftp = self._open_sftp()
            try:
                sftp.stat(remote_path)
                return TransferResult(True, self.kind, remote_path)
            finally:
                try:
                    sftp.close()
                finally:
                    client.close()
        except Exception as e:
            return TransferResult(False, self.kind, str(e))

    def delete(self, remote_relative_path: str) -> TransferResult:
        remote_path = self._remote_path(remote_relative_path)
        try:
            client, sftp = self._open_sftp()
            try:
                sftp.remove(remote_path)
                return TransferResult(True, self.kind, remote_path)
            finally:
                try:
                    sftp.close()
                finally:
                    client.close()
        except Exception as e:
            return TransferResult(False, self.kind, str(e))


class S3Target(TransferTarget):
    """S3 upload target (requires ``boto3``).

    This target supports:
    - endpoint_url, bucket, prefix, region, access_key_id, secret_access_key, verify, addressing_style
    Secrets should be provided as file paths (contents will be read).
    """

    kind = "s3"

    def __init__(self, **params: Any) -> None:
        """Create an S3 target."""
        # Endpoint + bucket
        self.endpoint_url: str = str(params.get("endpoint_url") or "")
        self.bucket: str = str(params.get("bucket") or "")

        # Prefix / region
        self.prefix: str = str(params.get("prefix") or "").strip("/")
        self.region: str = str(params.get("region") or "")

        # Credentials (file paths)
        self.access_key_id: Optional[str] = _expand_secret(params.get("access_key_id"))
        self.secret_access_key: Optional[str] = _expand_secret(params.get("secret_access_key"))

        # TLS verify and addressing
        self.verify: Union[bool, str] = _coerce_verify(params.get("verify", True))
        self.addressing_style: str = str(params.get("addressing_style") or "path")

        # Validate
        if not self.bucket:
            raise ValueError("S3Target requires 'bucket' parameter.")

    def upload(self, local_path: Path, remote_relative_path: str) -> TransferResult:
        """Upload using S3.

        Returns:
            TransferResult with ``s3://...`` URI in ``detail`` on success.
        """
        try:
            import importlib

            boto3_mod = importlib.import_module("boto3")
            botocore_config_mod = importlib.import_module("botocore.config")
            BotoSession = boto3_mod.session.Session
            BotocoreConfig = botocore_config_mod.Config
        except Exception as e:
            return TransferResult(False, self.kind, f"boto3 not available: {e}")

        key = str(PurePosixPath(self.prefix) / remote_relative_path).lstrip("/")
        try:
            session = BotoSession(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region or None,
            )

            cfg = BotocoreConfig(
                s3={"addressing_style": self.addressing_style},
                proxies=None,
            )

            s3 = session.client(
                "s3",
                endpoint_url=self.endpoint_url or None,
                verify=self.verify,
                config=cfg,
            )
            s3.upload_file(local_path.as_posix(), self.bucket, key)
            return TransferResult(True, self.kind, f"s3://{self.bucket}/{key}")
        except Exception as e:
            return TransferResult(False, self.kind, str(e))

    def _make_key(self, remote_relative_path: str) -> str:
        return str(PurePosixPath(self.prefix) / remote_relative_path).lstrip("/")

    def _make_client(self):
        try:
            import importlib

            boto3_mod = importlib.import_module("boto3")
            botocore_config_mod = importlib.import_module("botocore.config")
            BotoSession = boto3_mod.session.Session
            BotocoreConfig = botocore_config_mod.Config
        except Exception as e:
            raise RuntimeError(f"boto3 not available: {e}") from e

        session = BotoSession(
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region or None,
        )
        cfg = BotocoreConfig(
            s3={"addressing_style": self.addressing_style},
            proxies=None,
        )
        return session.client(
            "s3",
            endpoint_url=self.endpoint_url or None,
            verify=self.verify,
            config=cfg,
        )


    def exists(self, remote_relative_path: str) -> TransferResult:
        key = self._make_key(remote_relative_path)
        try:
            s3 = self._make_client()
            s3.head_object(Bucket=self.bucket, Key=key)
            return TransferResult(True, self.kind, f"s3://{self.bucket}/{key}")
        except Exception as e:
            return TransferResult(False, self.kind, str(e))

    def delete(self, remote_relative_path: str) -> TransferResult:
        key = self._make_key(remote_relative_path)
        try:
            s3 = self._make_client()
            s3.delete_object(Bucket=self.bucket, Key=key)
            return TransferResult(True, self.kind, f"s3://{self.bucket}/{key}")
        except Exception as e:
            return TransferResult(False, self.kind, str(e))


class TransferHandler:
    """Scan outbox folders and upload files to targets."""

    def __init__(
        self,
        outbox_root: Path,
        targets: List[TransferTarget],
        *,
        require_all_targets: bool = False,
        retries: int = 3,
        backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 30.0,
        logger=None,
    ) -> None:
        """Create a transfer manager.

        Args:
            outbox_root: Root directory containing per-instrument outboxes (flat per instrument).
            targets: List of upload targets.
            require_all_targets: If True, treat upload as success only if *all* targets succeed.
            retries: Number of retry attempts per target.
            backoff_seconds: Base backoff in seconds.
            max_backoff_seconds: Maximum backoff in seconds.
            logger: Optional logger for messages.
        """
        self.outbox_root = outbox_root
        self.targets = targets
        self.require_all_targets = require_all_targets
        self.retries = max(1, retries)
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.logger = logger

    def transmit_instrument(self, instrument_name: str, remote_path: str, remove_on_success: bool = True) -> None:
        """Transmit all files currently in an instrument outbox.

        Args:
            instrument_name: Instrument key (directory under outbox root).
            remote_path: Remote relative directory/prefix (typically instrument_name).
            remove_on_success: If True, delete outbox file after successful upload.
        """
        base = self.outbox_root / instrument_name
        if not base.exists():
            return

        # Outbox is intended to be flat. Ignore subdirectories.
        files = sorted([p for p in base.glob("*") if p.is_file()])
        for file_path in files:
            self._transmit_one(file_path, remote_path, remove_on_success)

    def transmit_all(self, instrument_remote_path_map: Dict[str, str], remove_on_success_map: Dict[str, bool]) -> None:
        """Transmit all files for all instruments."""
        for instrument_name, remote_path in instrument_remote_path_map.items():
            self.transmit_instrument(
                instrument_name,
                remote_path,
                remove_on_success=remove_on_success_map.get(instrument_name, True),
            )

    def _transmit_one(self, local_path: Path, remote_path: str, remove_on_success: bool) -> List[TransferResult]:
        # Remote path is flat: <remote_path>/<filename>
        remote_relative_path = (PurePosixPath(remote_path) / local_path.name).as_posix().lstrip("/")
        results: List[TransferResult] = []

        for target in self.targets:
            ok = False
            detail = ""
            for attempt in range(1, self.retries + 1):
                result = target.upload(local_path, remote_relative_path)
                detail = result.detail
                if result.ok:
                    ok = True
                    break
                _sleep_backoff(attempt, self.backoff_seconds, self.max_backoff_seconds)
            results.append(TransferResult(ok, target.kind, detail))

        overall_success = all(r.ok for r in results) if self.require_all_targets else any(r.ok for r in results)

        if overall_success:
            if remove_on_success:
                local_path.unlink(missing_ok=True)
            if self.logger:
                self.logger.info(
                    "[transfer] %s -> %s (%s)",
                    local_path.name,
                    remote_relative_path,
                    ", ".join(f"{r.target}:{r.ok}" for r in results),
                )
        else:
            if self.logger:
                self.logger.error(
                    "[transfer] failed %s -> %s (%s)",
                    local_path.name,
                    remote_relative_path,
                    ", ".join(f"{r.target}:{r.detail}" for r in results),
                )

        return results

    @staticmethod
    def _build_remote_relative_path(remote_path: str, filename: str) -> str:
        return (PurePosixPath(remote_path) / filename).as_posix().lstrip("/")

    def startup_selftest(
        self,
        station_id: str,
        *,
        instrument_name: str = "__selftest__",
        remote_root: str = "_pydaq_selftest",
        cleanup_local: bool = True,
        verify_retries: int = 3,
    ) -> bool:
        """
        Create a tiny test file, upload it to all configured targets, verify it exists,
        then attempt deletion (best-effort).

        Returns:
            True if upload+verify succeeded for all targets that are configured.
        """
        if not self.targets:
            if self.logger:
                self.logger.info("[selftest] skipped (no targets)")
            return True

        out_dir = self.outbox_root / instrument_name
        out_dir.mkdir(parents=True, exist_ok=True)

        filename = f"pydaq_selftest_{station_id}.txt"
        local_path = out_dir / filename

        local_path.write_text(
            "pydaq transfer self-test\n"
            f"station={station_id}\n"
            f"dtm={time.strftime('%Y-%m-%dT%H:%M:%S')}\n",
            encoding="utf-8",
        )

        remote_path = f"{remote_root}/{station_id}"
        remote_rel = self._build_remote_relative_path(remote_path, filename)

        if self.logger:
            self.logger.info("[selftest] upload start file=%s remote=%s", local_path.name, remote_rel)

        # Reuse existing upload logic, but keep local file for verify/delete phase.
        upload_results = self._transmit_one(local_path, remote_path, remove_on_success=False)

        verify_results: list[TransferResult] = []
        delete_results: list[TransferResult] = []

        # verify + delete aligned with self.targets order (zip is safe)
        for target, up in zip(self.targets, upload_results):
            if not up.ok:
                verify_results.append(TransferResult(False, target.kind, "skip (upload failed)"))
                delete_results.append(TransferResult(False, target.kind, "skip (upload failed)"))
                continue

            # verify with small retry (some backends can be briefly eventual-consistent)
            v: TransferResult = TransferResult(False, target.kind, "not verified")
            for attempt in range(1, max(1, verify_retries) + 1):
                v = target.exists(remote_rel)
                if v.ok:
                    break
                _sleep_backoff(attempt, base_seconds=1.0, max_seconds=5.0)
            verify_results.append(v)

            # delete best-effort (may fail depending on rights)
            if v.ok:
                delete_results.append(target.delete(remote_rel))
            else:
                delete_results.append(TransferResult(False, target.kind, "skip (not verified)"))

        ok_upload = all(r.ok for r in upload_results) if self.require_all_targets else any(r.ok for r in upload_results)
        ok_verify = all(r.ok for r in verify_results) if self.require_all_targets else any(r.ok for r in verify_results)

        # Per-target log line (same logger path as normal transfers)
        if self.logger:
            for t, up, vr, dr in zip(self.targets, upload_results, verify_results, delete_results):
                self.logger.info(
                    "[selftest] target=%s upload=%s verify=%s delete=%s detail=%s",
                    getattr(t, "kind", "target"),
                    up.ok,
                    vr.ok,
                    dr.ok,
                    (vr.detail or up.detail or dr.detail),
                )
            self.logger.info("[selftest] result upload_ok=%s verify_ok=%s", ok_upload, ok_verify)

        if cleanup_local:
            try:
                local_path.unlink(missing_ok=True)
            except Exception:
                pass

        # For a "confirmation that transfers work", treat verify as the key signal.
        return bool(ok_upload and ok_verify)
