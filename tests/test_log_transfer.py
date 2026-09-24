from __future__ import annotations

import logging
from pathlib import Path
from zipfile import ZipFile

import pytest

from pydaq.utils.config_handler import ConfigError, _parse_logging_config
from pydaq.utils.logging_handler import (
    ArchivingRotatingFileHandler,
    configure_log_archive_directory,
)


def test_logging_transfer_defaults_to_disabled() -> None:
    config = _parse_logging_config({})

    assert config.transfer.enabled is False
    assert config.transfer.remote_path == "logs"


def test_logging_transfer_config_is_parsed() -> None:
    config = _parse_logging_config(
        {
            "max_bytes": 2_000_000,
            "backup_count": 7,
            "transfer": {
                "enabled": True,
                "remote_path": "/diagnostics/pydaq/",
            },
        }
    )

    assert config.transfer.enabled is True
    assert config.transfer.remote_path == "diagnostics/pydaq"


def test_logging_transfer_requires_rotation() -> None:
    with pytest.raises(ConfigError, match="requires logging.max_bytes > 0"):
        _parse_logging_config(
            {
                "max_bytes": 0,
                "backup_count": 7,
                "transfer": {"enabled": True},
            }
        )


def test_rollover_keeps_local_backup_and_stages_zip(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    archive_dir = tmp_path / "outbox" / "logs"
    log_dir.mkdir(parents=True)
    handler = ArchivingRotatingFileHandler(
        log_dir / "pydaq.log",
        maxBytes=1000,
        backupCount=2,
        encoding="utf-8",
        archive_directory=archive_dir,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger(f"test.pydaq.log-transfer.{id(handler)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    try:
        logger.info("completed diagnostic log")
        handler.flush()
        handler.doRollover()
    finally:
        handler.close()
        logger.handlers.clear()

    local_backup = log_dir / "pydaq.log.1"
    assert local_backup.is_file()
    assert "completed diagnostic log" in local_backup.read_text(encoding="utf-8")

    archives = list(archive_dir.glob("pydaq-*.log.zip"))
    assert len(archives) == 1
    assert not list(archive_dir.glob("*.tmp"))

    with ZipFile(archives[0]) as archive:
        names = archive.namelist()
        assert len(names) == 1
        assert names[0].startswith("pydaq-")
        assert names[0].endswith(".log")
        assert "completed diagnostic log" in archive.read(names[0]).decode("utf-8")


def test_archive_destination_can_be_changed_at_runtime(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    first_archive_dir = tmp_path / "outbox" / "logs-a"
    second_archive_dir = tmp_path / "outbox" / "logs-b"
    log_dir.mkdir(parents=True)

    handler = ArchivingRotatingFileHandler(
        log_dir / "pydaq.log",
        maxBytes=1000,
        backupCount=1,
        encoding="utf-8",
        archive_directory=first_archive_dir,
    )
    logger = logging.getLogger(f"test.pydaq.log-transfer-reload.{id(handler)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)

    configure_log_archive_directory(logger, second_archive_dir)

    try:
        assert handler.archive_directory == second_archive_dir
    finally:
        handler.close()
        logger.handlers.clear()


def test_global_transfer_scan_includes_log_outbox() -> None:
    try:
        from pydaq.pydaq import Orchestrator
    except ModuleNotFoundError as exc:  # pragma: no cover - artifact test env only
        pytest.skip(f"full pydaq runtime dependencies are not installed: {exc}")

    calls: list[tuple[dict[str, str], dict[str, bool]]] = []

    class TransferHandlerStub:
        def transmit_all(
            self,
            remote_paths: dict[str, str],
            remove_on_success: dict[str, bool],
        ) -> None:
            calls.append((remote_paths, remove_on_success))

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.transfer_handler = TransferHandlerStub()
    orchestrator.application_config = type(
        "ConfigStub",
        (),
        {
            "instruments": {},
            "logging": type(
                "LoggingStub",
                (),
                {
                    "transfer": type(
                        "LogTransferStub",
                        (),
                        {"enabled": True, "remote_path": "diagnostics/pydaq"},
                    )()
                },
            )(),
        },
    )()

    orchestrator._transfer_scan_all()

    assert calls == [
        (
            {"logs": "diagnostics/pydaq"},
            {"logs": True},
        )
    ]
