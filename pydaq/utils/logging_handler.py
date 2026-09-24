"""Logging setup utilities.

The orchestrator uses a single application logger named ``pydaq``.

This helper creates:
- a console handler, suitable for interactive sessions and systemd/journalctl
- a rotating file handler for on-device persistence

Handler levels and file rotation are configured via YAML. Rotated log files can
optionally be archived as ZIP files into an outbox directory for normal pydaq
transfer handling.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile


def _log_level(value: str, default: int = logging.INFO) -> int:
    """Return a logging level from a case-insensitive level name."""
    level = getattr(logging, str(value).upper(), None)
    return level if isinstance(level, int) else default


class CompactConsoleHandler(logging.StreamHandler):
    """Console handler that can suppress traceback text for selected records.

    Records logged with ``extra={"console_compact": True}`` still retain their
    ``exc_info`` for subsequent handlers (notably the rotating file handler),
    while the interactive console receives only the concise operator message.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if not getattr(record, "console_compact", False):
            super().emit(record)
            return
        exc_info = record.exc_info
        exc_text = record.exc_text
        stack_info = record.stack_info
        try:
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            super().emit(record)
        finally:
            record.exc_info = exc_info
            record.exc_text = exc_text
            record.stack_info = stack_info


class ArchivingRotatingFileHandler(RotatingFileHandler):
    """Rotate logs locally and archive completed rotations for transfer.

    The normal ``RotatingFileHandler`` backup files remain in the log directory.
    After a successful rollover, the newly completed ``.1`` file is additionally
    compressed to an immutable ZIP file in ``archive_directory``. The ZIP is
    first written below ``.staging`` and then atomically moved into the archive
    directory so the transfer scanner never sees a partially written file.
    """

    def __init__(
        self,
        filename: str | Path,
        *,
        maxBytes: int = 0,
        backupCount: int = 0,
        encoding: str | None = None,
        archive_directory: Path | None = None,
    ) -> None:
        self.archive_directory = archive_directory
        super().__init__(
            filename,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
        )

    def set_archive_directory(self, archive_directory: Path | None) -> None:
        """Update the archive destination safely while logging is active."""
        self.acquire()
        try:
            self.archive_directory = archive_directory
        finally:
            self.release()

    def doRollover(self) -> None:
        """Perform normal rollover, then archive the completed log file."""
        super().doRollover()

        if self.archive_directory is None or self.backupCount <= 0:
            return

        rotated_path = Path(f"{self.baseFilename}.1")
        if not rotated_path.is_file():
            return

        try:
            self._archive_rotated_file(rotated_path)
        except Exception as exc:  # pragma: no cover - defensive operational path
            # Do not let an archive problem break normal logging. Avoid logging
            # recursively through this handler; emit one concise diagnostic.
            try:
                sys.stderr.write(
                    "pydaq logging: could not archive rotated log "
                    f"{rotated_path}: {exc}\n"
                )
                sys.stderr.flush()
            except Exception:
                pass

    def _archive_rotated_file(self, rotated_path: Path) -> Path:
        """Create one timestamped ZIP archive for a completed rotated log."""
        assert self.archive_directory is not None

        archive_directory = self.archive_directory
        staging_directory = archive_directory / ".staging"
        archive_directory.mkdir(parents=True, exist_ok=True)
        staging_directory.mkdir(parents=True, exist_ok=True)

        source_name = Path(self.baseFilename).name
        source_path = Path(source_name)
        suffix = source_path.suffix or ".log"
        stem = source_path.stem or "pydaq"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archived_log_name = f"{stem}-{timestamp}{suffix}"
        archive_name = f"{archived_log_name}.zip"
        final_path = archive_directory / archive_name
        staging_path = staging_directory / f"{archive_name}.tmp"

        try:
            with ZipFile(
                staging_path,
                mode="w",
                compression=ZIP_DEFLATED,
            ) as archive:
                archive.write(rotated_path, arcname=archived_log_name)
            staging_path.replace(final_path)
        finally:
            staging_path.unlink(missing_ok=True)

        return final_path


def configure_log_archive_directory(
    logger: logging.Logger,
    archive_directory: Path | None,
) -> None:
    """Set the rotated-log archive directory on configured file handlers."""
    for handler in logger.handlers:
        if isinstance(handler, ArchivingRotatingFileHandler):
            handler.set_archive_directory(archive_directory)


def setup_logging(
    log_directory: Path,
    file_name: str,
    level_console: str,
    level_file: str,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
    archive_directory: Path | None = None,
) -> logging.Logger:
    """Create and return the top-level ``pydaq`` logger.

    Args:
        log_directory:
            Directory where log files are stored.
        file_name:
            Log file name, for example ``pydaq.log``.
        level_console:
            Console handler level, for example ``warning``.
        level_file:
            File handler level, for example ``warning``.
        max_bytes:
            Maximum size of the active log file before rotation.
            Set to zero to disable size-based rotation.
        backup_count:
            Number of rotated backup files to retain.
        archive_directory:
            Optional directory where completed rotated logs are staged as ZIP
            archives for transfer. ``None`` disables transfer staging.

    Returns:
        The configured ``logging.Logger`` instance.
    """
    max_bytes = int(max_bytes)
    backup_count = int(backup_count)

    if max_bytes < 0:
        raise ValueError("logging.max_bytes must be >= 0")

    if backup_count < 0:
        raise ValueError("logging.backup_count must be >= 0")

    log_directory.mkdir(parents=True, exist_ok=True)
    logfile = log_directory / file_name
    logger = logging.getLogger("pydaq")
    logger.setLevel(logging.DEBUG)  # Individual handlers apply their own levels.

    # Avoid duplicate handlers if setup_logging() is called repeatedly.
    if logger.handlers:
        configure_log_archive_directory(logger, archive_directory)
        return logger

    formatter = logging.Formatter(
        "%(asctime)s, %(levelname)s, %(name)s, %(message)s"
    )

    console_handler = CompactConsoleHandler()
    console_handler.setLevel(_log_level(level_console))
    console_handler.setFormatter(formatter)
    file_handler = ArchivingRotatingFileHandler(
        logfile,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        archive_directory=archive_directory,
    )
    file_handler.setLevel(_log_level(level_file))
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False

    return logger
