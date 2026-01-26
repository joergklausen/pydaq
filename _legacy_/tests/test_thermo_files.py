# tests/test_thermo_files.py
from __future__ import annotations

from pathlib import Path

from instr.thermo import Thermo


def test_save_and_stage_text(tmp_path, monkeypatch):
    root = tmp_path / "results"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "staging").mkdir(parents=True, exist_ok=True)

    cfg = {
        "simulate": True,
        "local": {"root": str(root), "data": "data", "staging": "staging"},
        "instruments": [{"name": "thermo", "class": "instr.thermo.Thermo",
                         "model": "49i",
                         "params": {
                             "communication": "serial",
                             "serial": "COM1",
                             "get_data": "lr00",
                             "averaging_interval": 1,
                             "reporting_interval": 60,
                             "staging_zip": True,
                             "filename_extension": "dat",
                             "header": "# custom header"
                         }}],
    }
    monkeypatch.setattr("instr.instrument.load_config", lambda _: cfg)

    t = Thermo("thermo", "ignored.yaml")
    t.accumulate_data("lr00 123.4")
    t.save_data_file()
    assert t._saved_data_path and t._saved_data_path.exists()
    assert t._saved_data_path.suffix == ".dat"
    txt = t._saved_data_path.read_text(encoding="utf-8")
    assert txt.splitlines()[0] == "# custom header"
    t.stage_data_file()  # zipped copy

# # tests/test_thermo_files.py
# from __future__ import annotations

# from pathlib import Path
# from types import SimpleNamespace

# from instr.thermo import Thermo


# def test_save_and_stage(tmp_path, monkeypatch):
#     root = tmp_path / "results"
#     (root / "data").mkdir(parents=True, exist_ok=True)
#     (root / "staging").mkdir(parents=True, exist_ok=True)

#     cfg = {
#         "simulate": True,
#         "local": {"root": str(root), "data": "data", "staging": "staging", "logging": "logs"},
#         "logging": {"file_name": "pydaq.log", "level_console": "INFO", "level_file": "ERROR"},
#         "ports": {"COM1": {"protocol": "RS232", "baudrate": 9600, "bytesize": 8,
#                            "stopbits": 1, "parity": "N", "timeout": 0.1}},
#         "transfer": {"sftp": {"host": "x", "usr": "y", "key_path": "~/.ssh/id", "remote_path": "tests"}},
#         "instruments": {"thermo": {
#             "id": 49, "serial_number": None, "model": "49i", "communication": "serial",
#             "serial": "COM1",
#             "socket": {"mode": "tcp", "host": "127.0.0.1", "port": 9, "timeout": 1, "sleep": 0.1},
#             "get_config": [], "set_config": [], "get_data": "lr00",
#             "averaging_interval": 1, "reporting_interval": 60, "staging_zip": True,
#         }},
#     }
#     monkeypatch.setattr("instr.instrument.load_config", lambda _: cfg)

#     # Inject a harmless dummy serial so instantiation never touches real hardware
#     class DummySerial:
#         def __init__(self, *a, **k): self.is_open = False
#         def open(self): self.is_open = True
#         def close(self): self.is_open = False
#         def reset_input_buffer(self): pass
#         def write(self, *_a, **_k): pass
#         def read(self, _n): return b""

#     monkeypatch.setattr("instr.instrument.serial", SimpleNamespace(Serial=DummySerial), raising=False)

#     t = Thermo("thermo", "ignored.yaml")
#     t.accumulate_data("lr00 123.4")
#     t.save_data_file()
#     assert t._saved_data_path and t._saved_data_path.exists()

#     t.stage_data_file()
#     # Either zipped or copied; original remains
#     assert t._saved_data_path.exists()
