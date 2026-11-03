"""Regression tests for instrument startup fault isolation in pydaq.

Place in tests/test_instrument_activation_isolation.py.
"""
from dataclasses import replace
import logging
from pathlib import Path

from pydaq.pydaq import Orchestrator
from pydaq.utils.config_handler import load_config


class FakeInstrument:
    def __init__(self):
        self.enabled = False
        self.started = False
        self.stopped = False

    def set_enabled(self, value: bool) -> None:
        self.enabled = value

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def make_orchestrator():
    # Bypass normal startup: no network connections or live drivers.
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.instruments = {}
    orchestrator._instrument_config_fingerprints = {}
    orchestrator._last_status_sample_ts = {}
    orchestrator._last_no_sample_notice = {}
    orchestrator.logger = logging.getLogger("test.pydaq.startup")
    return orchestrator


def two_instrument_config():
    original = load_config(Path("pydaq/configs/mkn.yml"))
    # Put broken Tapo first; it must not prevent the subsequent METEO startup.
    return replace(original, instruments={
        "tapo": original.instruments["tapo"],
        "meteo": original.instruments["meteo"],
    })


def test_missing_optional_dependency_skips_only_one_instrument(monkeypatch):
    orchestrator = make_orchestrator()
    configured = two_instrument_config()
    healthy = FakeInstrument()
    scheduled = []

    def create(config):
        if config.name == "tapo":
            raise ImportError("No module named 'cv2'")
        return healthy

    monkeypatch.setattr(orchestrator, "_create_instrument_instance", create)
    monkeypatch.setattr(orchestrator, "_schedule_instrument_jobs",
                        lambda cfg, inst: scheduled.append(cfg.name))
    orchestrator._apply_configuration(configured)
    assert "tapo" not in orchestrator.instruments
    assert "tapo" not in orchestrator._instrument_config_fingerprints
    assert orchestrator.instruments["meteo"] is healthy
    assert healthy.started
    assert scheduled == ["meteo"]


def test_partially_started_instrument_is_stopped(monkeypatch):
    orchestrator = make_orchestrator()
    configured = two_instrument_config()
    tapo, meteo = FakeInstrument(), FakeInstrument()

    monkeypatch.setattr(orchestrator, "_create_instrument_instance",
                        lambda cfg: tapo if cfg.name == "tapo" else meteo)

    def schedule_jobs(cfg, inst):
        if cfg.name == "tapo":
            raise RuntimeError("broken schedule")

    monkeypatch.setattr(orchestrator, "_schedule_instrument_jobs", schedule_jobs)
    orchestrator._apply_configuration(configured)
    assert tapo.started
    assert tapo.stopped
    assert not tapo.enabled
    assert "tapo" not in orchestrator.instruments
    assert orchestrator.instruments["meteo"] is meteo
    assert meteo.started
