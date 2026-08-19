"""Logging setup utilities.

The orchestrator uses a single application logger named ``pydaq``.

This helper creates:
- a console handler, suitable for interactive sessions and systemd/journalctl
- a rotating file handler for on-device persistence

Handler levels and file rotation are configured via YAML.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


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


def setup_logging(
    log_directory: Path,
    file_name: str,
    level_console: str,
    level_file: str,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
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
        return logger

    formatter = logging.Formatter(
        "%(asctime)s, %(levelname)s, %(name)s, %(message)s"
    )

    console_handler = CompactConsoleHandler()
    console_handler.setLevel(_log_level(level_console))
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        logfile,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(_log_level(level_file))
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False

    return logger