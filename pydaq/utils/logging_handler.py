"""Logging setup utilities.

The orchestrator uses a single application logger named ``pydaq``.

This helper creates:
- a console handler (good for interactive sessions and systemd/journalctl)
- a rotating file handler (on-device persistence)

Both handler levels are configured via YAML.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(
    log_directory: Path,
    file_name: str,
    level_console: str,
    level_file: str,
) -> logging.Logger:
    """Create and return the top-level ``pydaq`` logger.

    The logger is created once; repeated calls return the same logger without adding duplicate handlers.

    Args:
        log_directory: Directory where log files are stored.
        file_name: Log file name (e.g. ``pydaq.log``).
        level_console: Console handler level (e.g. ``info``).
        level_file: File handler level (e.g. ``error``).

    Returns:
        The configured ``logging.Logger`` instance.
    """
    log_directory.mkdir(parents=True, exist_ok=True)
    logfile = log_directory / file_name

    logger = logging.getLogger("pydaq")
    logger.setLevel(logging.DEBUG)  # handlers filter

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s, %(levelname)s, %(name)s, %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, level_console.upper(), logging.INFO))
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        logfile,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, level_file.upper(), logging.INFO))
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
