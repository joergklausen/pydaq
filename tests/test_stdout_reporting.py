from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from pydaq.pydaq import Orchestrator


@dataclass
class DummyState:
    latest: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    last_sample_ts: float = 0.0


class DummyInstrument:
    def __init__(
        self,
        latest: dict[str, Any] | None = None,
        *,
        last_sample_ts: float = 0.0,
        emits_own_status: bool = False,
    ) -> None:
        self.state = DummyState(
            latest=latest or {},
            last_sample_ts=last_sample_ts,
        )
        self.EMITS_OWN_STATUS = emits_own_status


def build_orchestrator(instrument_name: str, instrument: DummyInstrument) -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.logger = logging.getLogger("pydaq.stdout-test")
    orchestrator.instruments = {instrument_name: instrument}
    orchestrator._last_status_sample_ts = {}
    return orchestrator


def info_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ]


def test_shared_formatter_logs_new_49i_sample_once(caplog) -> None:
    instrument = DummyInstrument(
        {"dtm": "2026-08-01 14:05:00", "o3": 27.436, "flags": "0"},
        last_sample_ts=100.0,
    )
    orchestrator = build_orchestrator("49i", instrument)

    with caplog.at_level(logging.DEBUG, logger="pydaq.stdout-test"):
        orchestrator._log_latest_for_instrument("49i")
        orchestrator._log_latest_for_instrument("49i")

    assert info_messages(caplog) == ["[49i] O3=27.4 ppb flags=0"]


def test_shared_formatter_logs_next_sample(caplog) -> None:
    instrument = DummyInstrument(
        {"dtm": "2026-08-01 14:05:00", "T": 24.1608, "RH": 51.7233, "Td": 13.6125},
        last_sample_ts=100.0,
    )
    orchestrator = build_orchestrator("hmp110-inlet", instrument)

    with caplog.at_level(logging.INFO, logger="pydaq.stdout-test"):
        orchestrator._log_latest_for_instrument("hmp110-inlet")
        instrument.state.latest = {
            "dtm": "2026-08-01 14:06:00",
            "T": 24.2123,
            "RH": 51.8123,
            "Td": 13.7012,
        }
        instrument.state.last_sample_ts = 160.0
        orchestrator._log_latest_for_instrument("hmp110-inlet")

    assert info_messages(caplog) == [
        "[hmp110-inlet] T=24.2 °C RH=51.7 % Td=13.6 °C",
        "[hmp110-inlet] T=24.2 °C RH=51.8 % Td=13.7 °C",
    ]


def test_driver_owned_status_is_not_duplicated(caplog) -> None:
    instrument = DummyInstrument(
        {"dtm": "2026-08-01 14:05:00", "61": 0.0146},
        last_sample_ts=100.0,
        emits_own_status=True,
    )
    orchestrator = build_orchestrator("fidas", instrument)

    with caplog.at_level(logging.DEBUG, logger="pydaq.stdout-test"):
        orchestrator._log_latest_for_instrument("fidas")

    assert info_messages(caplog) == []
    assert "[fidas] latest=" in caplog.text


def test_driver_owned_status_still_gets_no_sample_warning(caplog) -> None:
    instrument = DummyInstrument(emits_own_status=True)
    orchestrator = build_orchestrator("ne300", instrument)

    with caplog.at_level(logging.WARNING, logger="pydaq.stdout-test"):
        orchestrator._log_latest_for_instrument("ne300")

    assert "[ne300] no sample available yet" in caplog.text


def test_disable_clears_last_status_timestamp(monkeypatch) -> None:
    instrument = SimpleNamespace(
        set_enabled=lambda value: None,
        stop=lambda: None,
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.logger = logging.getLogger("pydaq.stdout-test")
    orchestrator.instruments = {"49c": instrument}
    orchestrator._instrument_config_fingerprints = {"49c": "abc"}
    orchestrator._last_status_sample_ts = {"49c": 100.0}

    monkeypatch.setattr("pydaq.pydaq.schedule.clear", lambda tag: None)
    orchestrator._disable_instrument("49c", "test")

    assert "49c" not in orchestrator._last_status_sample_ts
