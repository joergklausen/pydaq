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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any, Dict, Iterator, List, Optional, Union


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

    def _remote_path(self, remote_relative_path: str) -> str:
        """Return the complete POSIX path on the SFTP server."""
        return (
            PurePosixPath(self.remote_base) / remote_relative_path
        ).as_posix()

    @contextmanager
    def _sftp_session(self) -> Iterator[Any]:
        """Open an SSH/SFTP session and always close allocated resources.

        Cleanup also occurs if connecting, opening SFTP, creating directories,
        uploading, verifying, or deleting raises an exception.
        """
        try:
            import importlib

            paramiko = importlib.import_module("paramiko")
        except Exception as exc:
            raise RuntimeError(f"paramiko not available: {exc}") from exc

        client = None
        sftp = None

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                key_filename=(
                    str(Path(self.key).expanduser())
                    if self.key
                    else None
                ),
                timeout=20,
            )
            sftp = client.open_sftp()
            yield sftp
        finally:
            # SFTP uses the SSH transport, so close it first.
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass

            # ``client`` may exist even when connect() or open_sftp() failed.
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @staticmethod
    def _ensure_remote_parent(sftp: Any, remote_path: str) -> None:
        """Create missing remote parent directories."""
        path = PurePosixPath(remote_path)
        current = (
            PurePosixPath("/")
            if path.is_absolute()
            else PurePosixPath(".")
        )

        for part in path.parts[:-1]:
            if part in {"/", "."}:
                continue

            current = current / part
            directory = current.as_posix()

            try:
                sftp.stat(directory)
                continue
            except OSError:
                pass

            try:
                sftp.mkdir(directory)
            except OSError as mkdir_error:
                # A concurrent process may have created it between stat/mkdir.
                try:
                    sftp.stat(directory)
                except OSError:
                    raise RuntimeError(
                        f"could not create remote directory {directory!r}"
                    ) from mkdir_error

    def upload(
        self,
        local_path: Path,
        remote_relative_path: str,
    ) -> TransferResult:
        """Upload one file using an automatically closed SFTP session."""
        remote_path = self._remote_path(remote_relative_path)

        try:
            with self._sftp_session() as sftp:
                self._ensure_remote_parent(sftp, remote_path)
                sftp.put(local_path.as_posix(), remote_path)
            return TransferResult(True, self.kind, remote_path)
        except Exception as exc:
            return TransferResult(False, self.kind, str(exc))

    def exists(self, remote_relative_path: str) -> TransferResult:
        """Check whether a remote file exists."""
        remote_path = self._remote_path(remote_relative_path)

        try:
            with self._sftp_session() as sftp:
                sftp.stat(remote_path)
            return TransferResult(True, self.kind, remote_path)
        except Exception as exc:
            return TransferResult(False, self.kind, str(exc))

    def delete(self, remote_relative_path: str) -> TransferResult:
        """Delete a remote file."""
        remote_path = self._remote_path(remote_relative_path)

        try:
            with self._sftp_session() as sftp:
                sftp.remove(remote_path)
            return TransferResult(True, self.kind, remote_path)
        except Exception as exc:
            return TransferResult(False, self.kind, str(exc))


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
        self.access_key_id: Optional[str] = _expand_secret(
            params.get("access_key_id") or params.get("access_key")
        )
        self.secret_access_key: Optional[str] = _expand_secret(
            params.get("secret_access_key") or params.get("secret_key")
        )

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


# One lock is shared by all handler instances in the process. This also
# protects against overlap if a YAML reload replaces ``TransferHandler`` while
# an older scan is still completing.
_TRANSFER_SCAN_LOCK = Lock()


class TransferHandler:
    """Scan outbox folders and upload files to targets.

    All public scan entry points share a non-blocking process-wide lock. Thus a
    global scan, per-instrument scan, or startup self-test can never use the
    same outbox/transfer targets concurrently.
    """

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
            outbox_root: Root containing flat per-instrument outboxes.
            targets: Upload targets.
            require_all_targets: Require every target to succeed.
            retries: Attempts per target.
            backoff_seconds: Base retry delay.
            max_backoff_seconds: Maximum retry delay.
            logger: Optional logger.
        """
        self.outbox_root = outbox_root
        self.targets = targets
        self.require_all_targets = require_all_targets
        self.retries = max(1, retries)
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.logger = logger

    def _try_start_scan(
        self,
        scope: str,
        *,
        blocking: bool = False,
    ) -> bool:
        """Acquire the process-wide transfer lock.

        Per-instrument jobs use non-blocking acquisition and are skipped when a
        broader scan is active. The global scan uses blocking acquisition so it
        always runs after any active per-instrument scan and cannot starve.
        """
        acquired = _TRANSFER_SCAN_LOCK.acquire(blocking=blocking)
        if not acquired and self.logger:
            self.logger.debug(
                "[transfer] scan skipped scope=%s "
                "reason=another transfer scan is active",
                scope,
            )
        return acquired

    @staticmethod
    def _finish_scan() -> None:
        """Release the process-wide transfer lock."""
        _TRANSFER_SCAN_LOCK.release()

    def transmit_instrument(
        self,
        instrument_name: str,
        remote_path: str,
        remove_on_success: bool = True,
    ) -> None:
        """Transmit one instrument outbox unless another scan is active."""
        if not self._try_start_scan(f"instrument:{instrument_name}"):
            return

        try:
            scanned, uploaded, failed = self._transmit_instrument_unlocked(
                instrument_name,
                remote_path,
                remove_on_success,
            )
            if self.logger and scanned:
                self.logger.info(
                    "[transfer] scan complete "
                    "instrument=%s scanned=%d uploaded=%d failed=%d",
                    instrument_name,
                    scanned,
                    uploaded,
                    failed,
                )
        finally:
            self._finish_scan()

    def transmit_all(
        self,
        instrument_remote_path_map: Dict[str, str],
        remove_on_success_map: Dict[str, bool],
    ) -> None:
        """Transmit all instrument outboxes as one serialized scan."""
        if not self._try_start_scan("all", blocking=True):
            return

        try:
            scanned_total = 0
            uploaded_total = 0
            failed_total = 0

            for instrument_name, remote_path in (
                instrument_remote_path_map.items()
            ):
                scanned, uploaded, failed = (
                    self._transmit_instrument_unlocked(
                        instrument_name,
                        remote_path,
                        remove_on_success_map.get(instrument_name, True),
                    )
                )
                scanned_total += scanned
                uploaded_total += uploaded
                failed_total += failed

            if self.logger and scanned_total:
                self.logger.info(
                    "[transfer] scan complete "
                    "scope=all scanned=%d uploaded=%d failed=%d",
                    scanned_total,
                    uploaded_total,
                    failed_total,
                )
        finally:
            self._finish_scan()

    def _transmit_instrument_unlocked(
        self,
        instrument_name: str,
        remote_path: str,
        remove_on_success: bool,
    ) -> tuple[int, int, int]:
        """Transmit one outbox while the caller owns the transfer lock."""
        base = self.outbox_root / instrument_name
        if not base.exists():
            return 0, 0, 0

        files = sorted(path for path in base.glob("*") if path.is_file())
        uploaded = 0
        failed = 0

        for file_path in files:
            results = self._transmit_one(
                file_path,
                remote_path,
                remove_on_success,
            )
            if self._overall_success(results):
                uploaded += 1
            else:
                failed += 1

        return len(files), uploaded, failed

    def _overall_success(self, results: List[TransferResult]) -> bool:
        """Evaluate a list of per-target results."""
        if not results:
            return False
        if self.require_all_targets:
            return all(result.ok for result in results)
        return any(result.ok for result in results)

    def _transmit_one(
        self,
        local_path: Path,
        remote_path: str,
        remove_on_success: bool,
    ) -> List[TransferResult]:
        """Transmit one file to all configured targets."""
        remote_relative_path = (
            PurePosixPath(remote_path) / local_path.name
        ).as_posix().lstrip("/")
        results: List[TransferResult] = []

        for target in self.targets:
            result = TransferResult(False, target.kind, "not attempted")

            for attempt in range(1, self.retries + 1):
                try:
                    result = target.upload(
                        local_path,
                        remote_relative_path,
                    )
                except Exception as exc:
                    # A target should return a failed result, but an unexpected
                    # exception must not abort the rest of the outbox scan.
                    result = TransferResult(False, target.kind, str(exc))

                if result.ok:
                    break

                if attempt < self.retries:
                    _sleep_backoff(
                        attempt,
                        self.backoff_seconds,
                        self.max_backoff_seconds,
                    )

            results.append(result)

        overall_success = self._overall_success(results)

        if overall_success:
            if remove_on_success:
                try:
                    local_path.unlink(missing_ok=True)
                except OSError as exc:
                    if self.logger:
                        self.logger.warning(
                            "[transfer] uploaded but could not remove "
                            "local file=%s error=%s",
                            local_path,
                            exc,
                        )

            if self.logger:
                self.logger.debug(
                    "[transfer] %s -> %s (%s)",
                    local_path.name,
                    remote_relative_path,
                    ", ".join(
                        f"{result.target}:{result.ok}"
                        for result in results
                    ),
                )
        elif self.logger:
            self.logger.error(
                "[transfer] failed %s -> %s (%s)",
                local_path.name,
                remote_relative_path,
                ", ".join(
                    f"{result.target}:{result.detail}"
                    for result in results
                ),
            )

        return results

    @staticmethod
    def _build_remote_relative_path(
        remote_path: str,
        filename: str,
    ) -> str:
        """Build a flat relative remote path."""
        return (
            PurePosixPath(remote_path) / filename
        ).as_posix().lstrip("/")

    def startup_selftest(
        self,
        station_id: str,
        *,
        instrument_name: str = "__selftest__",
        remote_root: str = "_pydaq_selftest",
        cleanup_local: bool = True,
        verify_retries: int = 3,
    ) -> bool:
        """Upload, verify, and delete a small transfer test file."""
        if not self._try_start_scan("selftest", blocking=True):
            if self.logger:
                self.logger.warning(
                    "[selftest] skipped because another transfer scan "
                    "is active"
                )
            return False

        try:
            return self._startup_selftest_unlocked(
                station_id,
                instrument_name=instrument_name,
                remote_root=remote_root,
                cleanup_local=cleanup_local,
                verify_retries=verify_retries,
            )
        finally:
            self._finish_scan()

    def _startup_selftest_unlocked(
        self,
        station_id: str,
        *,
        instrument_name: str,
        remote_root: str,
        cleanup_local: bool,
        verify_retries: int,
    ) -> bool:
        """Run the startup self-test while holding the transfer lock."""
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
        remote_rel = self._build_remote_relative_path(
            remote_path,
            filename,
        )

        if self.logger:
            self.logger.info(
                "[selftest] upload start file=%s remote=%s",
                local_path.name,
                remote_rel,
            )

        upload_results = self._transmit_one(
            local_path,
            remote_path,
            remove_on_success=False,
        )

        verify_results: List[TransferResult] = []
        delete_results: List[TransferResult] = []

        for target, upload_result in zip(self.targets, upload_results):
            if not upload_result.ok:
                verify_results.append(
                    TransferResult(
                        False,
                        target.kind,
                        "skip (upload failed)",
                    )
                )
                delete_results.append(
                    TransferResult(
                        False,
                        target.kind,
                        "skip (upload failed)",
                    )
                )
                continue

            verify_result = TransferResult(
                False,
                target.kind,
                "not verified",
            )
            attempts = max(1, verify_retries)
            for attempt in range(1, attempts + 1):
                verify_result = target.exists(remote_rel)
                if verify_result.ok:
                    break
                if attempt < attempts:
                    _sleep_backoff(
                        attempt,
                        base_seconds=1.0,
                        max_seconds=5.0,
                    )

            verify_results.append(verify_result)
            if verify_result.ok:
                delete_results.append(target.delete(remote_rel))
            else:
                delete_results.append(
                    TransferResult(
                        False,
                        target.kind,
                        "skip (not verified)",
                    )
                )

        upload_ok = self._overall_success(upload_results)
        verify_ok = self._overall_success(verify_results)

        if self.logger:
            for target, upload_result, verify_result, delete_result in zip(
                self.targets,
                upload_results,
                verify_results,
                delete_results,
            ):
                self.logger.info(
                    "[selftest] target=%s upload=%s verify=%s "
                    "delete=%s detail=%s",
                    getattr(target, "kind", "target"),
                    upload_result.ok,
                    verify_result.ok,
                    delete_result.ok,
                    (
                        verify_result.detail
                        or upload_result.detail
                        or delete_result.detail
                    ),
                )
            self.logger.info(
                "[selftest] result upload_ok=%s verify_ok=%s",
                upload_ok,
                verify_ok,
            )

        if cleanup_local:
            try:
                local_path.unlink(missing_ok=True)
            except OSError:
                pass

        return bool(upload_ok and verify_ok)

