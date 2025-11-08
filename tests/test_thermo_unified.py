# tests/test_thermo_unified.py
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import pytest

from instr.thermo import Thermo


def build_config(tmp_path: Path, *, model: str, comm: str):
    root = tmp_path / "results"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "staging").mkdir(parents=True, exist_ok=True)

    return {
        "simulate": True,
        "local": {"root": str(root), "data": "data", "staging": "staging", "logging": "logs"},
        "logging": {"file_name": "pydaq.log", "level_console": "INFO", "level_file": "ERROR"},
        "ports": {"COM1": {"protocol": "RS232", "baudrate": 9600, "bytesize": 8,
                           "stopbits": 1, "parity": "N", "timeout": 0.1}},
        "transfer": {
            "sftp": {"host": "sftp.example.org", "usr": "user", "key_path": "~/.ssh/id", "remote_path": "tests"},
            "s3": {
                "endpoint_url": "https://example.org/",
                "aws_region": "eu-central-1",
                "aws_s3_bucket_name": "bucket",
                "default_prefix": "mkn",
                "aws_access_key_id": "id",
                "aws_secret_access_key": "~/.aws/secret",
            },
        },
        "instruments": {
            "thermo": {
                "id": 49,
                "serial_number": None,
                "model": model,
                "communication": comm,
                "serial": "COM1",
                "socket": {"mode": "tcp", "host": "127.0.0.1", "port": 9999, "timeout": 1, "sleep": 0.1},
                "get_config": ["mode", "range"],
                "set_config": ["set mode remote", "set range 1"],
                "get_data": "lr00",
                "averaging_interval": 1,
                "reporting_interval": 60,
                "staging_zip": True,
            }
        },
    }


@pytest.fixture(params=[("49C", "serial"), ("49i", "serial"), ("49i", "socket")])
def config_and_patch(request, tmp_path, monkeypatch):
    model, comm = request.param
    cfg = build_config(tmp_path, model=model, comm=comm)
    # Make Instrument load our in-memory config
    monkeypatch.setattr("instr.instrument.load_config", lambda _: cfg)
    return model, comm


def test_init_and_routing(config_and_patch):
    model, comm = config_and_patch
    # simulate=True by default, so no hardware is touched
    t = Thermo("thermo", "ignored.yaml")
    if model == "49C":
        assert t._instr_comms == "serial"
    else:
        assert t._instr_comms in {"serial", "socket"}
    assert t.get_data() != ""


def test_get_set_config(config_and_patch):
    _ = config_and_patch
    t = Thermo("thermo", "ignored.yaml")
    got = t.get_config()
    setr = t.set_config()
    assert "mode" in got
    assert setr["set mode remote"] == "OK"


def test_lazy_serial_open_close(tmp_path, monkeypatch):
    cfg = build_config(tmp_path, model="49i", comm="serial")
    monkeypatch.setattr("instr.instrument.load_config", lambda _: cfg)

    # Build a tiny 'serial' module with a Serial class we control.
    ctor_count = {"n": 0}

    class DummySerial:
        def __init__(self, *a, **k):
            ctor_count["n"] += 1
            self.is_open = False
            self._reads = 0

        def open(self):
            self.is_open = True

        def close(self):
            self.is_open = False

        def reset_input_buffer(self):  # optional
            pass

        def write(self, payload: bytes):
            # optional: store payload if you want to assert on it
            self._last_write = payload

        def read(self, n: int) -> bytes:
            # first call returns some bytes; second call returns empty to stop the loop
            if self._reads == 0:
                self._reads += 1
                return b"lr00*CHK"  # echoed cmd + checksum-ish
            return b""

    dummy_serial_module = SimpleNamespace(Serial=DummySerial)
    # Inject our dummy into the instrument module namespace
    monkeypatch.setattr("instr.instrument.serial", dummy_serial_module, raising=False)

    t = Thermo("thermo", "ignored.yaml")
    t.simulate = False  # force real serial path through the decorator

    # No instance yet
    assert ctor_count["n"] == 0

    # First command should construct/open/close exactly once
    _ = t.send_command("lr00")
    assert ctor_count["n"] == 1

# # tests/test_thermo_unified.py
# from __future__ import annotations

# import pytest
# from unittest.mock import patch, MagicMock
# from pathlib import Path

# from instr.thermo import Thermo


# def _config_like_attached(tmp_path: Path, *, model: str, comm: str):
#     """
#     Build a config dict with the same content/keys as the attached config.yaml,
#     but in the mapping form expected by the current Instrument base:

#       config["instruments"][<name>] -> params dict
#     """
#     root = tmp_path / "results"
#     data = "data"
#     staging = "staging"

#     cfg = {
#         "simulate": True,
#         "local": {
#             "root": str(root),     # attached YAML uses a string root
#             "data": data,          # subfolder names, relative to root
#             "staging": staging,
#             "logging": "logs",
#         },
#         "logging": {"file_name": "pydaq.log", "level_console": "INFO", "level_file": "ERROR"},
#         "transfer": {
#             "sftp": {
#                 "host": "sftp.meteoswiss.ch",
#                 "usr": "gaw_kenya",
#                 "key_path": "~/.ssh/private-open-ssh-4096-mkn.ppk",
#                 "remote_path": "tests",
#                 "proxy_url": None,
#                 "proxy_port": 1080,
#             },
#             "s3": {  # present in attached file; not used here
#                 "endpoint_url": "https://servicedevt.meteoswiss.ch/",
#                 "aws_region": "eu-central-1",
#                 "aws_s3_bucket_name": "ch.meteoswiss.gawkenya",
#                 "default_prefix": "mkn",
#                 "aws_access_key_id": "gawkenya_native",
#                 "aws_secret_access_key": "~/.aws/minio-devt-secret-key",
#             },
#         },
#         "ports": {
#             "COM1": {"protocol": "RS232", "baudrate": 9600, "bytesize": 8, "stopbits": 1, "parity": "N", "timeout": 0.1},
#         },
#         # Current Instrument expects a mapping keyed by instrument name
#         "instruments": {
#             "thermo": {
#                 "id": 49,
#                 "serial_number": None,
#                 "model": model,
#                 "communication": comm,     # "serial" or "socket" (socket for 49i only)
#                 "serial": "COM1",
#                 "socket": {
#                     "mode": "tcp",
#                     "host": "192.168.100.2",
#                     "port": 9880,
#                     "timeout": 5,
#                     "sleep": 0.1,
#                 },
#                 "get_config": [
#                     "date", "time", "mode", "gas unit", "temp comp", "pres comp", "range", "format", "avg time",
#                     "lrec per", "lrec format", "lrec",
#                 ],
#                 "set_config": [
#                     "set mode remote", "set gas unit ppb", "set temp comp on", "set pres comp on",
#                     "set range 1", "set format 00", "set lrec format 0", "set save params",
#                 ],
#                 "get_data": "lr00",
#                 "averaging_interval": 1,
#                 "reporting_interval": 60,
#                 "staging_zip": True,
#             }
#         },
#     }
#     # Create the local directories (Instrument will likely need them)
#     (root / data).mkdir(parents=True, exist_ok=True)
#     (root / staging).mkdir(parents=True, exist_ok=True)
#     return cfg


# @pytest.fixture(params=[("49C", "serial"), ("49i", "serial"), ("49i", "socket")])
# def thermo_cfg(request, tmp_path, monkeypatch):
#     model, comm = request.param
#     cfg = _config_like_attached(tmp_path, model=model, comm=comm)
#     # Patch the config loader exactly where Instrument imports it
#     monkeypatch.setattr("utils.instrument.load_config", lambda _: cfg)
#     return model, comm


# def test_init_and_model_logic(thermo_cfg, monkeypatch):
#     model, comm = thermo_cfg

#     # Patch serial constructor at the place Instrument/Thermo will call it (lazy open)
#     with patch("utils.instrument.serial.Serial") as mock_serial_cls:
#         mock_serial = MagicMock()
#         mock_serial.is_open = False
#         mock_serial_cls.return_value = mock_serial

#         # Patch sockets only if needed
#         with patch("instr.thermo.socket.create_connection") as mock_conn:
#             mock_sock = MagicMock()
#             mock_conn.return_value.__enter__.return_value = mock_sock

#             t = Thermo("thermo", "ignored.yaml")

#             # If 49C + "socket" was requested, driver must fall back to serial
#             if model == "49C":
#                 assert t._instr_comms == "serial"
#             else:
#                 assert t._instr_comms in {"serial", "socket"}

#             # simulate=True by default; send_command returns stubs, no I/O
#             assert t.get_data() != ""


# def test_get_and_set_config(thermo_cfg, monkeypatch):
#     # simulate=True returns "OK" for set*, "SIM"/numbers for get
#     t = Thermo("thermo", "ignored.yaml")
#     got = t.get_config()
#     setr = t.set_config()
#     assert "mode" in got and isinstance(got["mode"], str)
#     assert "set mode remote" in setr and setr["set mode remote"] == "OK"


# def test_routing_serial_vs_socket(thermo_cfg, monkeypatch):
#     model, comm = thermo_cfg
#     t = Thermo("thermo", "ignored.yaml")

#     # Force simulate=False to exercise routing, but patch out the transports
#     t.simulate = False

#     called = {"serial": 0, "socket": 0}

#     def fake_serial(cmd: str) -> str:
#         called["serial"] += 1
#         return "SER"

#     def fake_socket(cmd: str) -> str:
#         called["socket"] += 1
#         return "NET"

#     monkeypatch.setattr(t, "_serial_comm", fake_serial)
#     monkeypatch.setattr(t, "_socket_comm", fake_socket)

#     _ = t.send_command("lr00")

#     if model == "49C" or comm == "serial":
#         assert called["serial"] == 1 and called["socket"] == 0
#     else:
#         assert called["socket"] == 1 and called["serial"] == 0
