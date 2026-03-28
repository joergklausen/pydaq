from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pydaq.instruments.instrument as instrument_mod
from pydaq.instruments.fidas import FIDAS


class DummyWriter:
    """Minimal in-memory writer used to isolate the FIDAS unit tests."""

    def __init__(self, *args, **kwargs) -> None:
        self.appended: list[dict] = []
        self.finalize_calls = 0
        self.stage_calls = 0

    def append(self, record: dict) -> None:
        self.appended.append(dict(record))

    def finalize_if_needed(self) -> None:
        self.finalize_calls += 1

    def stage_current(self) -> None:
        self.stage_calls += 1


@pytest.fixture
def fidas_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FIDAS:
    """Create a FIDAS instance with its writer replaced by an in-memory stub."""
    monkeypatch.setattr(instrument_mod, "HourlyCsvWriter", DummyWriter)

    logger = logging.getLogger("pytest.fidas")
    logger.setLevel(logging.DEBUG)

    driver = FIDAS(
        name="fidas",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logger,
        parameters={
            "io": {
                "host": "127.0.0.1",
                "port": 56790,
                "buffer_size": 8192,
                "timeout": 0.1,
            },
            "schedule": {
                "aggregation_period_minutes": 1,
            },
            "output": {
                "format": "csv_zip",
                "remote_path": "fidas",
                "remove_on_success": True,
            },
        },
    )
    assert isinstance(driver.writer, DummyWriter)
    return driver


def _utc(y: int, mo: int, d: int, h: int, mi: int, s: int) -> datetime:
    """Build a timezone-aware UTC datetime for deterministic tests."""
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def test_parse_record_extracts_numeric_channels() -> None:
    """A FIDAS UDP payload should be parsed into record id, checksum, and channels."""
    raw = "6082<sendVal 60=10.0;61=2.5;110=7.0;bad=value>3E"

    parsed = FIDAS.parse_record(raw)

    assert parsed["record_id"] == 6082
    assert parsed["checksum"] == "3E"
    assert parsed["60"] == pytest.approx(10.0)
    assert parsed["61"] == pytest.approx(2.5)
    assert parsed["110"] == pytest.approx(7.0)
    assert "bad" not in parsed


def test_append_record_emits_median_aggregate_and_rollover_flushes_buffer(
    fidas_driver: FIDAS,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw samples should be aggregated by minute and flushed on rollover."""
    records = [
        {"record_id": 6082, "checksum": "AA", "60": 10.0, "61": 1.0},
        {"record_id": 6082, "checksum": "AB", "60": 14.0, "61": 3.0},
        {},
        {"record_id": 6082, "checksum": "AC", "60": 20.0, "61": 5.0},
    ]
    now_values = [
        _utc(2026, 3, 28, 12, 0, 5),
        _utc(2026, 3, 28, 12, 0, 40),
        _utc(2026, 3, 28, 12, 1, 0),
        _utc(2026, 3, 28, 12, 1, 10),
    ]

    monkeypatch.setattr(fidas_driver, "get_record", lambda: records.pop(0))
    monkeypatch.setattr(fidas_driver, "_now_utc", lambda: now_values.pop(0))

    writer = fidas_driver.writer
    assert isinstance(writer, DummyWriter)

    fidas_driver.append_record()
    fidas_driver.append_record()
    assert writer.appended == []

    fidas_driver.append_record()
    assert len(writer.appended) == 1
    row_1200 = writer.appended[0]
    assert row_1200["dtm"] == "2026-03-28 12:00:00"
    assert row_1200["60"] == pytest.approx(12.0)
    assert row_1200["61"] == pytest.approx(2.0)
    assert fidas_driver.state.latest["60"] == pytest.approx(12.0)
    assert writer.finalize_calls == 1

    fidas_driver.append_record()
    assert len(writer.appended) == 1

    fidas_driver.rollover()
    assert len(writer.appended) == 2
    row_1201 = writer.appended[1]
    assert row_1201["dtm"] == "2026-03-28 12:01:00"
    assert row_1201["60"] == pytest.approx(20.0)
    assert row_1201["61"] == pytest.approx(5.0)
    assert writer.stage_calls == 1


def test_compute_raw_data_median_returns_empty_dict_without_samples(fidas_driver: FIDAS) -> None:
    """The compatibility helper should be harmless when no raw records are buffered."""
    assert fidas_driver.compute_raw_data_median() == {}