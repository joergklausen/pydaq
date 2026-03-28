from __future__ import annotations

"""Live integration test for a configured Thermo 49i instrument.

This test follows the same instantiation path used by ``pydaq.py``:
- load the station YAML via ``load_config``
- resolve the driver with ``get_driver_class``
- build the instrument instance with the same constructor arguments as the orchestrator
- call ``initialize()`` and then one live sample through ``append_record()``

It is intended for use on the deployment host with the instrument actually reachable.

Typical usage::

    python -m pytest -vv -rs -s tests/test_49i_integration.py \
      --station-config ./pydaq/configs/nrb.yml

Notes
-----
- The test skips cleanly when no config is supplied or when the 49i is disabled.
- It stubs out file writing so only the live connection + parsing path is exercised.
- If the instrument is unreachable or returns no valid sample, the test fails with a
  targeted message.
"""

from dataclasses import asdict
import logging
import os
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pydaq.instruments.instrument as instrument_mod
from pydaq.instruments.instrument import get_driver_class
from pydaq.utils.config_handler import load_config


class DummyWriter:
    """Minimal writer stub so the live test avoids filesystem side effects."""

    def __init__(self, *args, **kwargs) -> None:
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
    """Resolve the station config path from pytest option, env var, or a default path."""
    option_value = None
    try:
        option_value = pytestconfig.getoption("station_config")
    except Exception:
        option_value = None

    raw = option_value or os.environ.get("PYDAQ_STATION_CONFIG")
    if not raw:
        default_path = ROOT / "pydaq" / "configs" / "mkn.yml"
        if default_path.exists():
            return default_path
        pytest.skip("No station config supplied. Use --station-config or PYDAQ_STATION_CONFIG.")

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if not path.exists():
        pytest.skip(f"Station config not found: {path}")
    return path


@pytest.fixture
def live_49i_driver(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
    tmp_path: Path,
):
    """Instantiate the configured 49i exactly like the orchestrator does."""
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

    driver_parameters = {
        "io": instrument_config.io,
        "init": instrument_config.init,
        "processing": instrument_config.processing,
        "output": asdict(instrument_config.output),
    }

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
        if hasattr(instrument, "_line"):
            instrument._line.close()  # type: ignore[attr-defined]
    except Exception:
        pass


@pytest.mark.integration
def test_49i_integration_reads_live_record(live_49i_driver) -> None:
    """Connect to the configured 49i and collect one live sample via append_record()."""
    live_49i_driver.initialize()

    writer = live_49i_driver.writer
    assert isinstance(writer, DummyWriter)

    live_49i_driver.append_record()

    assert len(writer.appended) == 1, (
        "No valid record was written by Thermo 49i. "
        "Check host/port connectivity, instrument responsiveness, and the configured sample command."
    )

    row = writer.appended[0]
    assert row.get("dtm"), f"Record missing dtm: {row!r}"
    assert "o3" in row, f"Record missing o3 field: {row!r}"
    assert row["o3"] is not None, f"Record contains null o3 value: {row!r}"
    assert isinstance(row["o3"], (int, float)), f"o3 is not numeric: {row!r}"

    assert live_49i_driver.state.latest == row
