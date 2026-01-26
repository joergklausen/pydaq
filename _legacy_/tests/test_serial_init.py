import pytest
from types import SimpleNamespace

from instr.instrument import Instrument, with_serial


# Minimal concrete subclass for instantiation
class DummySerialInstrument(Instrument):
    @with_serial
    def _serial_comm(self, cmd: str) -> str:
        # Calling this triggers the lazy serial construction in the decorator
        return ""

    def get_data(self) -> str:
        return "t, 2.34\n"

    def accumulate_data(self, data: str) -> None:
        pass

    def _socket_comm(self, cmd: str) -> str:
        return ""

    def set_datetime(self) -> None:
        pass

    def get_config(self) -> dict:
        return {}

    def set_config(self) -> dict:
        return {}
    
    def display_data(self) -> str:
        return "O3: 2.34 ppb"


def test_serial_instrument_initialization(monkeypatch, tmp_path):
    # Config in the schema Instrument.__init__ expects:
    # local.root is a *path string*, with separate "data" and "staging" keys.
    cfg = {
        "local": {"root": str(tmp_path), "data": "data", "staging": "staging"},
        "ports": {"COM1": {"baudrate": 9600, "bytesize": 8, "parity": "N",
                           "stopbits": 1, "timeout": 2}},
        "instruments": {
            "dummy": {
                "id": 99,
                "serial_number": "TEST-SERIAL",
                "communication": "serial",
                "serial": "COM1",
                "averaging_interval": 1,
                "reporting_interval": 60,
            }
        },
    }

    # Make Instrument load our in-memory config
    monkeypatch.setattr("instr.instrument.load_config", lambda _: cfg)

    # Build a controllable dummy serial module with a Serial class.
    captured = {}

    class DummySerial:
        def __init__(self, *, port, baudrate, bytesize, parity, stopbits, timeout):
            captured["port"] = port
            captured["baudrate"] = baudrate
            captured["bytesize"] = bytesize
            captured["parity"] = parity
            captured["stopbits"] = stopbits
            captured["timeout"] = timeout
            self.is_open = False

        def open(self):
            self.is_open = True

        def close(self):
            self.is_open = False

        def reset_input_buffer(self):
            pass

        def write(self, _payload: bytes):
            pass

        def read(self, _n: int) -> bytes:
            return b""  # nothing to read; decorator will still close

    # Inject our dummy serial module into the exact namespace the code uses
    monkeypatch.setattr("instr.instrument.serial", SimpleNamespace(Serial=DummySerial), raising=False)

    # Instantiate and then trigger the lazy serial init by calling a @with_serial method
    instr = DummySerialInstrument("dummy", "ignored.yaml")
    instr._serial_comm("lr00")

    # Validate the constructor args captured by DummySerial
    assert captured == {
        "port": "COM1",
        "baudrate": 9600,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 2,
    }

# import pytest
# from unittest.mock import MagicMock, patch

# from instr.instrument import Instrument


# # Minimal concrete subclass for instantiation
# class DummySerialInstrument(Instrument):
#     def get_data(self): return "t, 2.34\n"
#     def accumulate_data(self, data: str): pass
#     def _serial_comm(self, cmd: str) -> str: return ""
#     def _socket_comm(self, cmd: str) -> str: return ""
#     def set_datetime(self): pass
#     def get_config(self) -> dict: return {}
#     def set_config(self) -> dict: return {}


# def test_serial_instrument_initialization(monkeypatch):
#     # Provide a config in the exact schema expected by Instrument.__init__
#     cfg = {
#         "local": {"root": {"data": "./data", "staging": "./staging"}},
#         "ports": {"COM1": {"baudrate": 9600, "bytesize": 8, "parity": "N",
#                            "stopbits": 1, "timeout": 2}},
#         "instruments": {
#             "dummy": {
#                 "id": 99,
#                 "serial_number": "TEST-SERIAL",
#                 "communication": "serial",
#                 "serial": "COM1",
#                 "averaging_interval": 1,
#                 "reporting_interval": 60,
#             }
#         },
#     }

#     # Make Instrument load our in-memory config instead of reading YAML
#     monkeypatch.setattr("instr.instrument.load_config", lambda _: cfg)

#     # Patch serial.Serial at the exact place it is constructed
#     with patch("instr.instrument.serial.Serial") as mock_serial_cls:
#         mock_instance = MagicMock()
#         mock_instance.is_open = False
#         mock_serial_cls.return_value = mock_instance

#         # Instantiate
#         _ = DummySerialInstrument("dummy", "ignored.yaml")

#         # Validate serial parameters
#         mock_serial_cls.assert_called_once_with(
#             port="COM1",
#             baudrate=9600,
#             bytesize=8,
#             parity="N",
#             stopbits=1,
#             timeout=2,
#         )


# # import pytest
# # from unittest.mock import MagicMock, patch
# # from pathlib import Path
# # from instr.instrument import Instrument

# # # Fixture to mock serial.Serial
# # @pytest.fixture
# # def mock_serial():
# #     with patch("instr.instrument.serial.Serial") as mock_class:
# #         mock_instance = MagicMock()
# #         mock_instance.is_open = False
# #         mock_class.return_value = mock_instance
# #         yield mock_class  # Return the constructor!

# # # Dummy subclass of Instrument for testing
# # from instr.instrument import Instrument

# # class DummySerialInstrument(Instrument):
# #     def get_data(self):
# #         return "2025-07-27 12:00:00, 2.34\n"

# #     def accumulate_data(self, data: str):
# #         raise NotImplementedError

# #     def _serial_comm(self, cmd: str) -> str:
# #         raise NotImplementedError

# #     def _socket_comm(self, cmd: str) -> str:
# #         raise NotImplementedError

# #     def set_datetime(self):
# #         raise NotImplementedError

# #     def get_config(self) -> dict:
# #         raise NotImplementedError

# #     def set_config(self) -> dict:
# #         raise NotImplementedError


# # # Test for serial initialization
# # def test_serial_instrument_initialization(mock_serial, tmp_path):
# #     config = Path("config") / "test.yaml"
# #     config.write_text(f"""
# #                       paths:
# #                         data: {tmp_path}/data
# #                         staging: {tmp_path}/staging

# #                       instruments:
# #                         dummy:
# #                           id: 99
# #                           serial_number: TEST-SERIAL
# #                           communication: serial
# #                           serial: COM1
# #                           data: dummy_data
# #                           staging: dummy_stage
# #                           COM1:
# #                             baudrate: 9600
# #                             bytesize: 8
# #                             parity: N
# #                             stopbits: 1
# #                             timeout: 2
# #                       """)

# #     instr = DummySerialInstrument("dummy", str(config))

# #     mock_serial.assert_called_once_with(
# #         port="COM1",
# #         baudrate=9600,
# #         bytesize=8,
# #         parity="N",
# #         stopbits=1,
# #         timeout=2
# #     )
