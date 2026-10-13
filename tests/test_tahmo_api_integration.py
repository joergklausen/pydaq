"""Read-only TAHMO integration probe using the station's YAML configuration.

Run from the repository root with:
    pytest -q -s tests/test_tahmo_api_integration.py

Change STATION_CONFIG below if testing another station. This test never
modifies pydaq's state or production data/outbox directories.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from pydaq.instruments.tahmo import TahmoConfig, ZentraV4Client, write_batch_zip

STATION_CONFIG = Path("pydaq/configs/mkn.yml")
TEST_START = "2026-10-12 00:00:00"
TEST_END = "2026-10-12 03:00:00"


def test_tahmo_short_live_download(tmp_path: Path) -> None:
    """Download a short interval for the TAHMO configured in the station YAML."""
    assert STATION_CONFIG.is_file(), f"Station config not found: {STATION_CONFIG}"
    station = yaml.safe_load(STATION_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(station, dict), "Station config must be a YAML mapping"
    instruments = station.get("instruments")
    assert isinstance(instruments, dict), "Expected 'instruments' YAML mapping"
    instrument = instruments.get("tahmo")
    assert isinstance(instrument, dict), "Expected 'instruments.tahmo' mapping"

    config = TahmoConfig.from_parameters(instrument)
    assert config.token_file.is_file(), f"Token file not found: {config.token_file}"

    client = ZentraV4Client(
        server=config.server,
        token_file=config.token_file,
        timeout_seconds=config.timeout_seconds,
    )
    batch = client.fetch_readings(
        device_sn=config.device_sn,
        start_date=TEST_START,
        end_date=TEST_END,
        per_page=min(config.per_page, 100),
    )
    assert batch.rows, (
        f"No readings: device={config.device_sn}, start={TEST_START}, end={TEST_END}"
    )
    assert batch.last_mrid is not None

    target = tmp_path / f"tahmo-{config.device_sn}-probe.zip"
    write_batch_zip(batch=batch, path=target)
    with ZipFile(target) as archive:
        names = archive.namelist()
        assert len(names) == 1
        reader = csv.DictReader(io.StringIO(archive.read(names[0]).decode("utf-8")))
        rows = list(reader)

    assert rows
    assert "measurement" in rows[0]
    assert "mrid" in rows[0]
    print(f"TAHMO probe device={config.device_sn} rows={len(rows)} last_mrid={batch.last_mrid}")
    print(f"Temporary ZIP: {target}")
