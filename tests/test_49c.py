from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from pydaq.instruments.registry import get_driver_class


class FakeLineComms:
    """Capture commands without opening a real serial port."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        cmd: str,
        *,
        prefix: bytes = b"",
        terminator: str | None = None,
    ) -> str:
        self.requests.append(
            {
                "cmd": cmd,
                "prefix": prefix,
                "terminator": terminator,
            }
        )
        return "06:46 07-29-26 20.652 0C105000 35290 34634 27.4 52.8 0.0 0.494 0.491 495.5"


def make_49c(tmp_path: Path, *, instrument_id: object = 49):
    instrument_class = get_driver_class("49c")

    parameters: dict[str, Any] = {
        "id": instrument_id,
        "io": {
            "kind": "serial",
            "port": "COM3",
            "baudrate": 9600,
            "parity": "N",
            "stopbits": 1,
            "timeout_seconds": 2,
        },
        "processing": {
            "sample_command": "lrec",
        },
    }

    return instrument_class(
        name="49c",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logging.getLogger("pytest.49c"),
        headers=None,
        parameters=parameters,
    )


def test_registry_resolves_49c_driver() -> None:
    instrument_class = get_driver_class("49c")

    assert instrument_class.__module__ == "pydaq.instruments.thermo"
    assert instrument_class.__name__ == "Thermo49C"
    assert instrument_class.DEFAULT_SAMPLE_COMMAND == "lrec"


def test_49c_uses_configured_address_byte(tmp_path: Path) -> None:
    instrument = make_49c(tmp_path, instrument_id=49)

    assert instrument._resolve_instrument_id_byte() == b"\xb1"


def test_49c_rejects_missing_id(tmp_path: Path) -> None:
    instrument_class = get_driver_class("49c")

    instrument = instrument_class(
        name="49c",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logging.getLogger("pytest.49c"),
        headers=None,
        parameters={
            "io": {
                "kind": "serial",
                "port": "COM3",
                "baudrate": 9600,
                "parity": "N",
                "stopbits": 1,
                "timeout_seconds": 2,
            },
            "processing": {
                "sample_command": "lrec",
            },
        },
    )

    with pytest.raises(
        ValueError,
        match=r"requires.*'id'.*0\.\.127",
    ):
        instrument._resolve_instrument_id_byte()
        

def test_49c_sends_addressed_lrec_command(tmp_path: Path) -> None:
    instrument = make_49c(tmp_path, instrument_id=49)
    fake_line = FakeLineComms()

    instrument._line = fake_line
    instrument._instrument_id_byte = instrument._resolve_instrument_id_byte()

    raw = instrument._send("lrec")

    assert raw
    assert fake_line.requests == [
        {
            "cmd": "lrec",
            "prefix": b"\xb1",
            "terminator": None,
        }
    ]


def test_49c_parses_lrec_record(tmp_path: Path) -> None:
    instrument = make_49c(tmp_path, instrument_id=49)
    instrument._line = FakeLineComms()
    instrument._instrument_id_byte = instrument._resolve_instrument_id_byte()

    record = instrument.get_record()

    assert record["time"] == "06:46"
    assert record["date"] == "07-29-26"
    assert record["o3"] == pytest.approx(20.652)
    assert record["flags"] == "0C105000"
    assert record["cellai"] == 35290
    assert record["cellbi"] == 34634
    assert record["bncht"] == pytest.approx(27.4)
    assert record["flowa"] == pytest.approx(0.494)
    assert record["pres"] == pytest.approx(495.5)