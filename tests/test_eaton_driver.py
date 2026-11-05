"""Offline tests for the production Eaton driver; no SNMP network access."""
from __future__ import annotations

import json
import logging

from pydaq.instruments.eaton import EATON, OIDS, decode_values, event_changes
from pydaq.instruments.registry import get_driver_class


def test_driver_registered():
    assert get_driver_class("eaton") is EATON


def test_rfc1628_scaling_and_mains_state():
    raw = {
        OIDS["output_source_code"]: "3",
        OIDS["battery_status_code"]: "2",
        OIDS["battery_voltage"]: "840",
        OIDS["battery_capacity_pct"]: "54",
        OIDS["battery_runtime_min"]: "68",
        OIDS["input_voltage"]: "236",
        OIDS["input_frequency"]: "503",
        OIDS["output_voltage"]: "236",
        OIDS["output_frequency"]: "503",
        OIDS["output_load"]: "37",
        OIDS["active_alarms"]: "0",
    }
    values = decode_values(raw)
    assert values["output_source"] == "normal"
    assert values["on_battery"] == 0
    assert values["battery_voltage"] == 84.0
    assert values["input_frequency"] == 50.3
    assert values["battery_capacity_pct"] == 54
    assert values["output_load"] == 37


def test_events_are_transitions_not_first_reading():
    normal = {"output_source_code": 3, "output_source": "normal", "battery_status_code": 2, "active_alarms": 0}
    battery = {**normal, "output_source_code": 5, "output_source": "battery"}
    assert event_changes(None, normal) == []
    assert ("ups_on_battery", "critical") in event_changes(normal, battery)
    assert ("ups_mains_restored", "info") in event_changes(battery, normal)


def test_get_record_persists_events_and_state(tmp_path, monkeypatch):
    data, outbox = tmp_path / "data", tmp_path / "outbox"
    data.mkdir()
    outbox.mkdir()
    instrument = EATON(
        name="eaton", data_dir=data, outbox_dir=outbox,
        logger=logging.getLogger("test.eaton"),
        parameters={"io": {"host": "example.invalid", "user": "example", "security_level": "noAuthNoPriv"}},
    )
    values = {"output_source_code": 3, "battery_capacity_pct": 75, "battery_status_code": 2}
    async def fake_poll(_io):
        return {OIDS[key]: str(val) for key, val in values.items()}
    monkeypatch.setattr("pydaq.instruments.eaton.poll_snmp", fake_poll)
    instrument.get_record()
    assert not list(outbox.glob("*event*"))  # no spurious startup alarm
    values["output_source_code"] = 5
    instrument.get_record()
    events = list(outbox.glob("*event*.json"))
    assert len(events) == 1
    event = json.loads(events[0].read_text(encoding="utf-8"))
    assert event["event"] == "ups_on_battery"
    assert (data / "events" / events[0].name).exists()
    assert json.loads((data / "eaton_last_state.json").read_text())["output_source_code"] == 5
