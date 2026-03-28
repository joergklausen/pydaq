from __future__ import annotations

"""Live integration test for a configured FIDAS instrument.

This test reads the station YAML, binds the configured UDP listener, and waits for one
real FIDAS datagram. It is intended for use on the deployment host with the instrument
actually attached and streaming.

Typical usage::

    python -m pytest -vv -rs -s tests/test_fidas_integration.py \
      --station-config ./pydaq/configs/nrb.yml

Notes
-----
- Stop any running pydaq instance first, otherwise the UDP port may already be in use.
- The test skips cleanly when no config is supplied, when FIDAS is disabled, or when the
  configured UDP socket is already bound by another process.
- Total wait time defaults to 30 seconds and can be overridden via the environment variable
  ``PYDAQ_FIDAS_WAIT_SECONDS``.
"""

import errno
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pydaq.instruments.instrument as instrument_mod
from pydaq.instruments.fidas import FIDAS


class DummyWriter:
    """Minimal writer stub so the live test exercises only network receive + parsing."""

    def __init__(self, *args, **kwargs) -> None:
        self.appended: list[dict[str, Any]] = []

    def append(self, record: dict[str, Any]) -> None:
        self.appended.append(dict(record))

    def finalize_if_needed(self) -> None:
        return

    def stage_current(self) -> None:
        return


def _resolve_station_config(pytestconfig: pytest.Config) -> Path:
    """Resolve the station config path from pytest option, env var, or default path."""
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


def _load_fidas_section(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load raw YAML and return the FIDAS instrument section plus its parent config."""
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    instruments = config.get("instruments") or {}
    fidas_cfg = instruments.get("fidas")
    if not isinstance(fidas_cfg, dict):
        pytest.skip(f"No 'instruments.fidas' section in {config_path}")
    if not bool(fidas_cfg.get("enabled", False)):
        pytest.skip(f"FIDAS is disabled in {config_path}")
    return config, fidas_cfg


@pytest.fixture
def live_fidas_driver(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
    tmp_path: Path,
) -> FIDAS:
    """Instantiate the FIDAS driver from station YAML with file writing stubbed out."""
    monkeypatch.setattr(instrument_mod, "HourlyCsvWriter", DummyWriter)

    config_path = _resolve_station_config(pytestconfig)
    _config, fidas_cfg = _load_fidas_section(config_path)

    io_cfg = dict(fidas_cfg.get("io") or {})
    schedule_cfg = dict(fidas_cfg.get("schedule") or {})
    output_cfg = dict(fidas_cfg.get("output") or {})

    logger = logging.getLogger("pytest.fidas.integration")
    logger.setLevel(logging.INFO)

    driver = FIDAS(
        name="fidas",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logger,
        parameters={
            "io": io_cfg,
            "schedule": schedule_cfg,
            "output": output_cfg,
        },
    )

    try:
        driver.initialize()
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            pytest.skip(
                f"FIDAS UDP socket {driver.host}:{driver.port} is already in use. "
                "Stop pydaq before running this live integration test."
            )
        raise

    yield driver
    driver.close()


@pytest.mark.integration
def test_fidas_integration_receives_live_record(live_fidas_driver: FIDAS) -> None:
    """Bind the configured UDP socket and receive one real FIDAS datagram."""
    total_wait_seconds = float(os.environ.get("PYDAQ_FIDAS_WAIT_SECONDS", "30"))
    deadline = time.monotonic() + max(1.0, total_wait_seconds)

    raw = ""
    while time.monotonic() < deadline:
        raw = live_fidas_driver.receive_udp_record()
        if raw:
            break

    assert raw, (
        f"No UDP record received from FIDAS within {total_wait_seconds:.0f} s on "
        f"{live_fidas_driver.host}:{live_fidas_driver.port}. "
        "Check that the instrument is attached, streaming, and targeting this host/port."
    )

    parsed = live_fidas_driver.parse_record(raw)
    assert parsed, f"Received UDP payload but could not parse it: {raw!r}"
    assert "record_id" in parsed and parsed["record_id"] not in {None, ""}
    assert "checksum" in parsed

    numeric_channels = [key for key in parsed if key.isdigit()]
    assert numeric_channels, f"Parsed record contains no numeric channels: {parsed!r}"

    expected_core = {"60", "61", "62", "63", "64", "65"}
    assert expected_core.intersection(numeric_channels), (
        "Parsed record did not include expected PM channels 60..65. "
        f"Channels present: {sorted(numeric_channels)[:20]}"
    )
