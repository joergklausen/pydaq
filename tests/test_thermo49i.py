# tests/test_thermo49i.py
from unittest.mock import MagicMock, patch
import pytest

from instr.thermo import Thermo49i


@pytest.fixture(params=["serial", "socket"])
def thermo49i(request, tmp_path, monkeypatch):
    """
    Single fixture for Thermo49i covering both comm modes.
    - Uses an in-memory config via monkeypatched loader (no YAML file I/O).
    - Patches serial/socket at their actual use sites to avoid hardware/network.
    """
    mode = request.param

    config = {
        "paths": {"data": str(tmp_path / "data"), "staging": str(tmp_path / "staging")},
        "ports": {"COM1": {"baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1, "timeout": 2}},
        "instruments": {
            "test49i": {
                "id": 49,
                "serial_number": "TEST-SN 1234",
                "simulate": True,          # keeps generic tests simple (send_command won't hit comms)
                "get_config": ["mode", "range"],
                "set_config": ["set mode remote", "set range 1"],
                "get_data": "o3",
                "averaging_interval": 1,
                "reporting_interval": 60,
                "communication": mode,
                "serial": "COM1",
                "socket": {
                    "host": "127.0.0.1",
                    "port": 10001,
                    "timeout": 5,
                    "sleep": 0.1,
                    "mode": "tcp",
                },
            }
        },
    }

    # Patch the loader where Instrument.__init__ reads it
    monkeypatch.setattr("instr.instrument.load_yaml_config", lambda _: config)

    if mode == "serial":
        # Patch serial where it's constructed (instr.instrument.serial.Serial)
        class DummySerial:
            def __init__(self, *a, **k): self.is_open = False
            def open(self): self.is_open = True
            def close(self): self.is_open = False
            def write(self, *a, **k): pass
            @property
            def in_waiting(self): return 0
            def read(self, *a, **k): return b""

        monkeypatch.setattr("instr.instrument.serial.Serial", DummySerial)
    else:
        # Patch socket where _tcp_comm uses it (instr.thermo.socket.socket)
        class DummySock:
            def __init__(self, *a, **k): pass
            def settimeout(self, *a, **k): pass
            def connect(self, *a, **k): pass
            def sendall(self, *a, **k): pass
            def recv(self, *a, **k): return b"\x00"  # terminator byte
            def __enter__(self): return self
            def __exit__(self, *exc): pass

        monkeypatch.setattr("instr.thermo.socket.socket", lambda *a, **k: DummySock())

    return Thermo49i("test49i", "ignored.yaml")


# ---------- Generic behavior tests (use unified fixture) ----------

def test_get_data(thermo49i):
    result = thermo49i.get_data()
    assert isinstance(result, str)
    assert result.strip() != ""


def test_config_methods(thermo49i):
    get_cfg = thermo49i.get_config()
    set_cfg = thermo49i.set_config()
    assert "mode" in get_cfg
    assert "set mode remote" in set_cfg


def test_datetime_staging(thermo49i):
    thermo49i.set_datetime()
    thermo49i.accumulate_data("simulated data")
    thermo49i.save_data_file()
    thermo49i.stage_data_file()
    assert thermo49i._saved_data_path.exists()


def test_save_data_file(thermo49i):
    thermo49i.accumulate_data("O3 123.4\nO3 125.6")
    thermo49i.save_data_file()
    assert thermo49i._saved_data_path.exists()
    assert thermo49i._saved_data_path.read_text().startswith(thermo49i._header)


def test_stage_data_file(thermo49i):
    thermo49i.accumulate_data("O3 120.0\nO3 121.1")
    thermo49i.save_data_file()
    thermo49i.stage_data_file()
    assert thermo49i._saved_data_path.exists()


# ---------- Helpers for get_all_lrec() ----------

def _install_fake_send(instance, monkeypatch, no_of_lrec=15):
    """
    Fake send_command to drive get_all_lrec() for ANY "lrec <idx> <batch>" sequence.
    Returns deterministic batch payloads in order, and records calls.
    """
    calls = []
    lrec_format_response = "set lrec format 0"
    batches = ["DATA_BATCH_1", "DATA_BATCH_2"]
    lrec_call_count = 0

    def fake_send(cmd: str) -> str:
        nonlocal lrec_call_count
        calls.append(cmd)

        if cmd == "lrec format":
            return lrec_format_response
        if cmd == "set lrec format 0":
            return "ok"
        if cmd == "no of lrec":
            return f"{no_of_lrec} records"
        if cmd.startswith("lrec "):
            payload = batches[min(lrec_call_count, len(batches) - 1)]
            lrec_call_count += 1
            return payload
        if cmd == f"set {lrec_format_response}":  # restore: "set set lrec format 0"
            return "ok"
        return "ok"

    # Override the instance method entirely (simulation flag is irrelevant now)
    monkeypatch.setattr(instance, "send_command", fake_send)
    return calls, batches[0], batches[1]


def test_get_all_lrec_saves_and_stages(monkeypatch, thermo49i):
    calls, b1, b2 = _install_fake_send(thermo49i, monkeypatch, no_of_lrec=15)

    thermo49i.save_data_file = MagicMock()
    thermo49i.stage_data_file = MagicMock()

    result = thermo49i.get_all_lrec(save=True)

    assert b1 in result
    assert b2 in result
    assert "lrec format" in calls
    assert "set lrec format 0" in calls
    assert "no of lrec" in calls
    assert any(c.startswith("lrec ") for c in calls)
    assert "set set lrec format 0" in calls
    thermo49i.save_data_file.assert_called_once()
    thermo49i.stage_data_file.assert_called_once()


def test_get_all_lrec_no_save(monkeypatch, thermo49i):
    calls, b1, b2 = _install_fake_send(thermo49i, monkeypatch, no_of_lrec=12)

    thermo49i.save_data_file = MagicMock()
    thermo49i.stage_data_file = MagicMock()

    result = thermo49i.get_all_lrec(save=False)

    assert b1 in result or b2 in result
    thermo49i.save_data_file.assert_not_called()
    thermo49i.stage_data_file.assert_not_called()
    assert "lrec format" in calls
    assert "set lrec format 0" in calls
    assert "no of lrec" in calls
    assert any(c.startswith("lrec ") for c in calls)
    assert "set set lrec format 0" in calls
