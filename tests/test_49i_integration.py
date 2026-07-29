from __future__ import annotations

"""Live integration test for a configured Thermo 49i instrument.

The test follows the production construction path closely:

- load the station YAML with ``load_config``;
- resolve the driver through the explicit driver registry;
- pass the complete ``InstrumentConfig`` mapping to the driver;
- initialize the instrument and collect one live sample.

Run explicitly on a host that can reach the configured analyzer:

    python -m pytest -vv -rs -s tests/test_49i_integration.py \\
        --run-integration \\
        --station-config pydaq/configs/mkn.yml
"""

from dataclasses import asdict
import logging
import os
from pathlib import Path
import sys
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pydaq.instruments.instrument as instrument_mod
from pydaq.instruments.registry import get_driver_class
from pydaq.utils.config_handler import load_config


class DummyWriter:
    """Minimal writer stub that avoids persistent file output."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.appended: list[dict[str, Any]] = []
        self.finalize_calls = 0
        self.stage_calls = 0

    def append(self, record: dict[str, Any]) -> None:
        self.appended.append(dict(record))

    def finalize_if_needed(self) -> None:
        self.finalize_calls += 1

    def stage_current(self) -> None:
        self.stage_calls += 1


def _resolve_station_config(pytestconfig: pytest.Config) -> Path:
    """Resolve the station config from pytest, the environment, or mkn.yml."""
    option_value: str | None
    try:
        raw_option = pytestconfig.getoption("station_config")
        option_value = str(raw_option) if raw_option else None
    except (ValueError, AttributeError):
        option_value = None

    raw = option_value or os.environ.get("PYDAQ_STATION_CONFIG")
    if not raw:
        default_path = ROOT / "pydaq" / "configs" / "mkn.yml"
        if default_path.exists():
            return default_path.resolve()
        pytest.skip(
            "No station config supplied. Use --station-config or "
            "PYDAQ_STATION_CONFIG."
        )

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()

    if not path.exists():
        pytest.skip(f"Station config not found: {path}")
    return path


@pytest.fixture
def live_49i_driver(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
    tmp_path: Path,
) -> Iterator[Any]:
    """Instantiate the configured 49i using production-equivalent parameters."""
    monkeypatch.setattr(instrument_mod, "HourlyCsvWriter", DummyWriter)

    config_path = _resolve_station_config(pytestconfig)
    application_config = load_config(config_path)

    instrument_config = application_config.instruments.get("49i")
    if instrument_config is None:
        pytest.skip(f"No 'instruments.49i' section in {config_path}")
    if not bool(instrument_config.enabled):
        pytest.skip(f"49i is disabled in {config_path}")

    instrument_class = get_driver_class(instrument_config.driver)

    data_directory = tmp_path / "data" / instrument_config.name
    outbox_directory = tmp_path / "outbox" / instrument_config.name
    data_directory.mkdir(parents=True, exist_ok=True)
    outbox_directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("pytest.49i.integration")
    logger.setLevel(logging.INFO)

    # Important: do not rebuild a partial parameter mapping here.
    # The Thermo address ``id`` is a top-level InstrumentConfig field.
    driver_parameters = asdict(instrument_config)

    instrument = instrument_class(
        name=instrument_config.name,
        data_dir=data_directory,
        outbox_dir=outbox_directory,
        logger=logger,
        headers=getattr(instrument_class, "HEADERS", None),
        output_format=instrument_config.output.format,
        parameters=driver_parameters,
    )

    yield instrument

    try:
        line = getattr(instrument, "_line", None)
        if line is not None:
            line.close()
    except Exception:
        pass


@pytest.mark.integration
def test_49i_integration_reads_live_record(live_49i_driver: Any) -> None:
    """Collect and validate one live Thermo 49i record."""
    live_49i_driver.initialize()

    writer = live_49i_driver.writer
    assert isinstance(writer, DummyWriter)

    live_49i_driver.append_record()

    assert len(writer.appended) == 1, (
        "No valid record was written by Thermo 49i. Check host/port "
        "connectivity, instrument responsiveness, the configured instrument "
        "id, and the sample command."
    )

    row = writer.appended[0]
    assert row.get("dtm"), f"Record missing dtm: {row!r}"
    assert "o3" in row, f"Record missing o3 field: {row!r}"
    assert row["o3"] is not None, f"Record contains null o3 value: {row!r}"
    assert isinstance(row["o3"], (int, float)), f"o3 is not numeric: {row!r}"
    assert live_49i_driver.state.latest == row
