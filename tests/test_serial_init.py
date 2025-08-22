import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from instr.instrument import Instrument

# Fixture to mock serial.Serial
@pytest.fixture
def mock_serial():
    with patch("instr.instrument.serial.Serial") as mock_class:
        mock_instance = MagicMock()
        mock_instance.is_open = False
        mock_class.return_value = mock_instance
        yield mock_class  # Return the constructor!

# Dummy subclass of Instrument for testing
from instr.instrument import Instrument

class DummySerialInstrument(Instrument):
    def get_data(self):
        return "2025-07-27 12:00:00, 2.34\n"

    def accumulate_data(self, data: str):
        raise NotImplementedError

    def _serial_comm(self, cmd: str) -> str:
        raise NotImplementedError

    def _socket_comm(self, cmd: str) -> str:
        raise NotImplementedError

    def set_datetime(self):
        raise NotImplementedError

    def get_config(self) -> dict:
        raise NotImplementedError

    def set_config(self) -> dict:
        raise NotImplementedError


# Test for serial initialization
def test_serial_instrument_initialization(mock_serial, tmp_path):
    config = Path("config") / "test.yaml"
    config.write_text(f"""
                      paths:
                        data: {tmp_path}/data
                        staging: {tmp_path}/staging

                      instruments:
                        dummy:
                          id: 99
                          serial_number: TEST-SERIAL
                          communication: serial
                          serial: COM1
                          data: dummy_data
                          staging: dummy_stage
                          COM1:
                            baudrate: 9600
                            bytesize: 8
                            parity: N
                            stopbits: 1
                            timeout: 2
                      """)

    instr = DummySerialInstrument("dummy", str(config))

    mock_serial.assert_called_once_with(
        port="COM1",
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=2
    )
