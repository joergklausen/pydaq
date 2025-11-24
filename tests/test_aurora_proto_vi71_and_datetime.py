# tests/instr/test_aurora_proto_vi71_and_datetime.py
from datetime import datetime

import pytest

from instr.ecotech.aurora_proto import AuroraClient
from tests.conftest import DummyNeph


def test_get_current_operation_vi71_mapping(monkeypatch):
    """
    Ensure VI71 responses are mapped to operation states as in the original driver:

    - "000" -> 0 (Normal / Ambient)
    - "032" -> 1 (Zero)
    - "016" -> 2 (Span)
    - anything else -> 9 (unknown/error)
    """
    drv = DummyNeph()
    client = AuroraClient(drv, params={"serial_id": 5})  # type: ignore[arg-type]

    sent_cmds: list[str] = []
    responses = iter(["000", "032", "016", "garbage"])

    def fake_send(cmd: str, expect_response: bool = True) -> str:
        sent_cmds.append(cmd)
        return next(responses)

    monkeypatch.setattr(client, "_send", fake_send)

    assert client.get_current_operation() == 0  # "000"
    assert client.get_current_operation() == 1  # "032"
    assert client.get_current_operation() == 2  # "016"
    assert client.get_current_operation() == 9  # unknown

    # All commands should be VI{05}71
    assert sent_cmds == ["VI0571", "VI0571", "VI0571", "VI0571"]


def test_get_datetime_uses_vi64_vi80_vi81_and_parses(monkeypatch):
    """
    Exercise get_datetime(), i.e., VI64/VI80/VI81 + _aurora_timestamp_to_datetime.
    """
    serial_id = 2
    drv = DummyNeph()
    client = AuroraClient(drv, params={"serial_id": serial_id})  # type: ignore[arg-type]

    commands: list[str] = []

    def fake_send(cmd: str, expect_response: bool = True) -> str:
        commands.append(cmd)
        if cmd == f"VI{serial_id:02d}64":
            return "D/M/Y"
        if cmd == f"VI{serial_id:02d}80":
            return "1/2/2025"  # 1 Feb 2025 in D/M/Y
        if cmd == f"VI{serial_id:02d}81":
            return "03:04:05"
        raise AssertionError(f"Unexpected command {cmd}")

    monkeypatch.setattr(client, "_send", fake_send)

    dt = client.get_datetime()

    assert commands == [
        f"VI{serial_id:02d}64",
        f"VI{serial_id:02d}80",
        f"VI{serial_id:02d}81",
    ]

    assert isinstance(dt, datetime)
    assert dt.year == 2025
    assert dt.month == 2
    assert dt.day == 1
    assert dt.hour == 3
    assert dt.minute == 4
    assert dt.second == 5
