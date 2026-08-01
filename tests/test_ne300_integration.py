from __future__ import annotations

"""Live integration test for the configured ACOEM NE300.

Run on a host that can reach the configured instrument:

    python -m pytest -vv -rs -s tests/test_ne300_integration.py --run-integration --station-config pydaq/configs/mkn.yml
"""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any, Iterator

import pytest

import pydaq.instruments.instrument as instrument_mod
from pydaq.instruments.registry import get_driver_class
from pydaq.utils.config_handler import load_config


class DummyWriter:
    """Writer stub that captures rows without creating station data files."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args
        self.headers = list(kwargs.get("headers") or [])
        self.appended: list[dict[str, Any]] = []
        self.finalize_calls = 0
        self.stage_calls = 0

        # NE300 checks this attribute before changing a dynamic header.
        self._file_handle = None

    def append(self, record: dict[str, Any]) -> None:
        self.appended.append(dict(record))

    def finalize_if_needed(self, now: Any = None) -> None:
        del now
        self.finalize_calls += 1

    def stage_current(self) -> None:
        self.stage_calls += 1


@pytest.fixture
def live_ne300_driver(
    monkeypatch: pytest.MonkeyPatch,
    station_config_path: Path,
    tmp_path: Path,
) -> Iterator[Any]:
    """Construct NE300 from the real station configuration."""
    monkeypatch.setattr(instrument_mod, "HourlyCsvWriter", DummyWriter)

    application_config = load_config(station_config_path)
    instrument_config = application_config.instruments.get("ne300")
    if instrument_config is None:
        pytest.skip(
            f"No 'instruments.ne300' section in {station_config_path}"
        )
    if not bool(instrument_config.enabled):
        pytest.skip(f"NE300 is disabled in {station_config_path}")

    instrument_class = get_driver_class(instrument_config.driver)
    data_directory = tmp_path / "data" / instrument_config.name
    outbox_directory = tmp_path / "outbox" / instrument_config.name
    data_directory.mkdir(parents=True, exist_ok=True)
    outbox_directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("pytest.ne300.integration")
    logger.setLevel(logging.INFO)

    # Preserve all top-level InstrumentConfig fields, including id and io.
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
        comms = getattr(instrument, "comms", None)
        if comms is not None:
            comms.close()
    except Exception:
        pass


@pytest.mark.integration
def test_ne300_integration_uses_command7_header_and_writes_records(
    live_ne300_driver: Any,
) -> None:
    """Retrieve live logger data and verify the packet-defined output schema."""
    instrument = live_ne300_driver
    instrument.initialize()

    writer = instrument.writer
    assert isinstance(writer, DummyWriter)

    # initialize() starts at the current minute. Move the cursor back far
    # enough to retrieve existing one-minute logger records in one command-7
    # request, while leaving the normal append_record() path unchanged.
    end = instrument._floor_to_minute(datetime.now(timezone.utc))
    start = end - timedelta(minutes=30)
    instrument._logged_cursor = start
    instrument._last_logged_dtm = None
    instrument.logged_chunk_seconds = int((end - start).total_seconds())

    instrument.append_record()

    assert writer.appended, (
        "NE300 returned no writable command-7 records for the preceding "
        "30 minutes. Check host/port connectivity, instrument time, and "
        "whether its internal data logger is active."
    )
    assert instrument.logged_parameter_ids, (
        "The command-7 response did not establish a logged-parameter header."
    )

    expected_headers = ["dtm", "4035", "2002"] + [
        str(parameter) for parameter in instrument.logged_parameter_ids
    ]
    assert instrument.HEADERS == expected_headers
    assert writer.headers == expected_headers
    assert len(expected_headers) == len(instrument.logged_parameter_ids) + 3

    # Special record metadata must not also occur among dynamic columns.
    assert 4035 not in instrument.logged_parameter_ids
    assert 2002 not in instrument.logged_parameter_ids
    assert len(instrument.logged_parameter_ids) == len(
        set(instrument.logged_parameter_ids)
    )

    for row in writer.appended:
        assert list(row) == expected_headers, (
            f"Row/header mismatch: headers={expected_headers!r} row={row!r}"
        )
        datetime.strptime(row["dtm"], "%Y-%m-%d %H:%M:%S")
        assert isinstance(row["4035"], int)
        assert isinstance(row["2002"], int)
        assert row["2002"] > 0

    assert instrument.state.latest == writer.appended[-1]
