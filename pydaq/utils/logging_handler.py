"""Logging setup utilities.

The orchestrator uses a single application logger named ``pydaq``.

This helper creates:
- a compact console handler for interactive sessions and systemd/journalctl
- a rotating file handler retaining the original detailed log messages

Both handler levels are configured via YAML.
"""

from __future__ import annotations

import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping

from pydaq.utils.status_formatter import format_latest_record


class _CompactConsoleFilter(logging.Filter):
    """Compact selected INFO records and suppress unchanged console samples.

    The filter is attached only to the console handler. The rotating file
    handler is registered first and therefore retains the original detailed
    message, including the complete ``latest`` dictionary.
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_sample_fingerprint: dict[str, str] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        """Transform supported records before console output.

        Args:
            record: Log record being handled.

        Returns:
            ``True`` when the record should be printed on the console.
        """
        if self._is_fidas_aggregate(record):
            # The normal orchestrator status line already reports PM values.
            return False

        latest = self._extract_latest(record)
        if latest is None:
            return True

        instrument_name, sample = latest
        fingerprint = self._fingerprint(sample)

        if self._last_sample_fingerprint.get(instrument_name) == fingerprint:
            return False

        self._last_sample_fingerprint[instrument_name] = fingerprint
        record.msg = f"[{instrument_name}] {format_latest_record(sample)}"
        record.args = ()
        return True

    @staticmethod
    def _extract_latest(
        record: logging.LogRecord,
    ) -> tuple[str, Mapping[str, Any]] | None:
        """Extract arguments from the orchestrator's current latest-data call."""
        if record.msg != "[%s] latest=%s":
            return None
        if not isinstance(record.args, tuple) or len(record.args) != 2:
            return None

        instrument_name, sample = record.args
        if not isinstance(sample, Mapping):
            return None

        return str(instrument_name), sample

    @staticmethod
    def _is_fidas_aggregate(record: logging.LogRecord) -> bool:
        """Identify the redundant FIDAS aggregate summary."""
        return (
            record.name.endswith(".fidas")
            and record.msg == "aggregate %s"
        )

    @staticmethod
    def _fingerprint(sample: Mapping[str, Any]) -> str:
        """Create a stable identity for duplicate suppression."""
        try:
            payload = json.dumps(
                sample,
                sort_keys=True,
                default=str,
                allow_nan=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            payload = repr(dict(sample))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def setup_logging(
    log_directory: Path,
    file_name: str,
    level_console: str,
    level_file: str,
) -> logging.Logger:
    """Create and return the top-level ``pydaq`` logger.

    The logger is created once; repeated calls return the same logger without
    adding duplicate handlers.

    Args:
        log_directory: Directory where log files are stored.
        file_name: Log file name, for example ``pydaq.log``.
        level_console: Console handler level, for example ``info``.
        level_file: File handler level, for example ``info``.

    Returns:
        Configured ``logging.Logger`` instance.
    """
    log_directory.mkdir(parents=True, exist_ok=True)
    logfile = log_directory / file_name

    logger = logging.getLogger("pydaq")
    logger.setLevel(logging.DEBUG)  # handlers filter
    if logger.handlers:
        return logger

    console_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    file_formatter = logging.Formatter(
        "%(asctime)s, %(levelname)s, %(name)s, %(message)s"
    )

    file_handler = RotatingFileHandler(
        logfile,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(
        getattr(logging, level_file.upper(), logging.INFO)
    )
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(
        getattr(logging, level_console.upper(), logging.INFO)
    )
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(_CompactConsoleFilter())

    # Keep this order: the file receives the untouched detailed LogRecord
    # before the console filter compacts the record for display.
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False
    return logger
