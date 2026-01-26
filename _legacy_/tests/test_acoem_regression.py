# tests/instr/test_acoem_regression.py
import struct

import pytest

from instr.ecotech.acoem_proto import AcoemClient
from instr.obsolete.acoem import NEPH  # old driver


CONFIG = {
    "logging": {"file": "mkndaq.log"},
    "ne300": {
        "type": "NE300",
        "protocol": "acoem",
        "serial_id": 0,
        "serial_number": "23-0690",
        "mac_number": "00:30:55:0A:78:78",
        "socket": {
            "host": "192.168.3.149",
            "port": "32783",
            "timeout": 5,
            "sleep": 0.5,
        },
        "sampling_interval": 5,  # minutes. logger retrieval interval
        "reporting_interval": 10,
        "data_path": "ne300",
        "staging_path": "ne300",
        "staging_zip": True,
        "remote_path": "ne300",
        "verbosity": 2,  # 0: silent, 1: medium, 2: full
        "zero_span_check_interval": 780,
        "zero_check_duration": 20,
        "span_check_duration": 0,
    },
}  # adapt config as needed


def test_new_acoem_builds_same_get_values_message_as_old(monkeypatch, dummy_driver):
    """
    Regression: the bytes sent for Get Values should be identical between
    the legacy NEPH driver and the new AcoemClient implementation.
    """
    old = NEPH(config=CONFIG, name="ne300")
    new = AcoemClient(dummy_driver, params={"serial_id": old.serial_id})

    sent_old: list[bytes] = []
    sent_new: list[bytes] = []

    def fake_old_tcp(message: bytes, verbosity: int = 0) -> bytes:
        sent_old.append(message)
        # Response body is irrelevant here; decoding is patched out.
        return b""

    def fake_new_tcp(message: bytes, expect_response: bool = True, verbosity: int = 0) -> bytes:
        sent_new.append(message)
        return b""

    # Patch transport so we just capture messages
    monkeypatch.setattr(old, "_tcpip_comm", fake_old_tcp)
    monkeypatch.setattr(new, "_tcp_request", fake_new_tcp)

    # Patch decoders so GetValues returns without touching the payload
    monkeypatch.setattr(old, "_acoem_response2values", lambda *a, **k: {})
    monkeypatch.setattr(new, "_decode_values_response", lambda *a, **k: {})

    params = [1, 2001, 8000]
    _ = old.get_values(parameters=params, verbosity=0)
    _ = new.get_values(parameters=params, verbosity=0)

    assert sent_old == sent_new


def _build_values_response_frame(serial_id: int, command: int, msg_data: bytes) -> bytes:
    """
    Build a minimal Get Values response frame compatible with both decoders.

    We omit checksum/EOT because neither decoder needs them; both only look at:
    - bytes 4–5: msg_len
    - bytes 6..(6+msg_len): msg_data
    """
    msg_len = len(msg_data)
    return bytes([2, serial_id, command, 3]) + msg_len.to_bytes(2, "big") + msg_data


def test_new_acoem_decoding_matches_legacy_response2values(monkeypatch, dummy_driver):
    """
    Regression: given the same synthetic response frame, the new decoder and
    legacy _acoem_response2values should return equivalent values.

    We use:
    - parameter 2001: integer (within 1000 < p < 5000)
    - parameter 8000: float
    """
    old = NEPH(config=CONFIG, name="ne300")
    new = AcoemClient(dummy_driver, params={"serial_id": old.serial_id})

    params = [2001, 8000]
    int_value = -42
    float_value = 1.23

    msg_data = struct.pack(">i", int_value) + struct.pack(">f", float_value)
    response = _build_values_response_frame(old.serial_id, 4, msg_data)

    legacy = old._acoem_response2values(parameters=params, response=response, verbosity=0)
    modern = new._decode_values_response(parameters=params, response=response, verbosity=0)

    assert set(legacy.keys()) == set(modern.keys()) == set(params)

    assert isinstance(modern[2001], int)
    assert modern[2001] == legacy[2001]

    assert isinstance(modern[8000], float)
    assert modern[8000] == pytest.approx(legacy[8000], rel=1e-6)
