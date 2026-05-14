from __future__ import annotations

"""Unit tests for the pydaq iQAir AirVisual Outdoor (AVO) driver.

The unit tests avoid real network access.  The download-cycle test monkeypatches
``AVO._download_data`` rather than ``requests.get`` so it remains independent of
whether the optional ``requests`` package is installed in the local test
environment.  A separate integration test may use a live URL from the station
configuration.
"""

import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

pl = pytest.importorskip("polars")

import pydaq.instruments.avo as avo_module
from pydaq.instruments.avo import AVO, AVOSource


def test_flatten_data_flattens_nested_airvisual_values() -> None:
    payload = {
        "ts": "2026-05-11T00:00:00Z",
        "pm25": {"aqius": 42, "conc": 12.5},
        "weather": {"tp": 20.1, "hm": 70},
    }

    assert AVO.flatten_data(payload) == {
        "ts": "2026-05-11T00:00:00Z",
        "pm25_aqius": 42,
        "pm25_conc": 12.5,
        "weather_tp": 20.1,
        "weather_hm": 70,
    }


def test_parse_timestamp_to_utc_naive_accepts_z_suffix() -> None:
    assert AVO._parse_timestamp_to_utc_naive("2026-05-11T00:00:00Z") == datetime(2026, 5, 11, 0, 0, 0)
    assert AVO._parse_timestamp_to_utc_naive("2026-05-11T02:00:00+02:00") == datetime(2026, 5, 11, 0, 0, 0)


def test_normalize_sources_accepts_multiple_url_forms(tmp_path: Path) -> None:
    driver = AVO(
        name="avo",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logging.getLogger("test"),
        parameters={
            "io": {
                "urls": {
                    "url_nairobi": "https://example.test/nairobi",
                    "bomet": {"url": "https://example.test/bomet", "validated": True},
                }
            }
        },
    )

    sources = driver._normalize_sources(params=driver.parameters, io_cfg=driver.parameters["io"])

    assert [source.name for source in sources] == ["nairobi", "bomet"]
    assert [source.url for source in sources] == [
        "https://example.test/nairobi",
        "https://example.test/bomet",
    ]
    assert [source.validated for source in sources] == [False, True]


def _payload_for_station(station_name: str, ts: str, co2: float) -> dict[str, Any]:
    return {
        "name": station_name,
        "historical": {
            "instant": [
                {
                    "ts": ts,
                    "co2": co2,
                    "pm25": {"aqius": 42, "aqicn": 12, "conc": 13.5},
                    "pm10": {"aqius": 21, "aqicn": 8, "conc": 25.0},
                    "tp": 24.1,
                    "hm": 65,
                    "pr": 815.1,
                }
            ]
        },
    }


def test_download_cycle_writes_and_stages_parquet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run one AVO download cycle without real HTTP access."""

    # The driver checks that module-level ``requests`` is not None during
    # initialization.  The actual network method is monkeypatched, so a dummy
    # object is enough for this unit test.
    monkeypatch.setattr(avo_module, "requests", SimpleNamespace())

    calls: list[tuple[str, str, bool]] = []

    def fake_download_data(self: AVO, source: AVOSource) -> Mapping[str, Any]:
        calls.append((source.name, source.url, source.validated))
        if source.name == "bomet":
            return _payload_for_station("Bomet AVO", "2026-05-11T00:01:00Z", 411)
        return _payload_for_station("Nairobi AVO", "2026-05-11T00:00:00Z", 410)

    monkeypatch.setattr(AVO, "_download_data", fake_download_data)

    driver = AVO(
        name="avo",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logging.getLogger("test"),
        parameters={
            "io": {
                "urls": {
                    "url_nairobi": "https://example.test/nairobi",
                    "bomet": {"url": "https://example.test/bomet", "validated": True},
                },
                "retries": 0,
            },
            "processing": {"datasets": ["instant"]},
            "output": {"data_path": "avo", "staging_path": "avo"},
        },
    )
    driver.initialize()

    summary = driver.get_record()

    assert summary["errors"] == ""
    assert summary["sources_total"] == 2
    assert summary["sources_ok"] == 2
    assert summary["sources_failed"] == 0
    assert summary["files_written"] == 2
    assert summary["rows_written"] == 2
    assert calls == [
        ("nairobi", "https://example.test/nairobi", False),
        ("bomet", "https://example.test/bomet", True),
    ]

    data_files = sorted((tmp_path / "data" / "avo").glob("*_avo_instant-*.parquet"))
    staged_files = sorted((tmp_path / "outbox" / "avo").glob("*_avo_instant-*.parquet"))
    assert [path.name for path in data_files] == [path.name for path in staged_files]
    assert len(data_files) == 2

    frame = pl.read_parquet(data_files[0])
    assert frame.height == 1
    assert "dtm" in frame.columns
    assert "pm25_conc" in frame.columns
