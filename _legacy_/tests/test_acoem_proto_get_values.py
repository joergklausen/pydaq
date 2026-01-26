import struct
from datetime import datetime, timezone

import pytest

from instr.ecotech.acoem_proto import AcoemClient


def _build_values_frame(client: AcoemClient, msg_data: bytes) -> bytes:
    """
    Helper to build a synthetic ACOEM Get Values response frame with given msg_data.
    """
    msg_len = len(msg_data)
    header = bytes(
        [
            2,  # STX
            client.serial_id,
            client.CMD_GET_VALUES,
            3,  # ETX / message type
        ]
    ) + msg_len.to_bytes(2, "big")
    frame = header + msg_data
    # checksum is over everything up to msg_data, then append EOT
    return frame + client._checksum(frame) + bytes([4])


def test_get_values_decodes_timestamp_and_float(dummy_driver, monkeypatch):
    """
    Full round-trip through get_values() for:
    - parameter 1: ACOEM timestamp → datetime
    - parameter 8000: float
    """
    client = AcoemClient(dummy_driver, params={"serial_id": 1})
    # 1 = timestamp, 8000 = float (not in any integer range)
    params = [1, 8000]

    dt = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    ts_bytes = client._datetime_to_timestamp(dt)
    float_bytes = struct.pack(">f", 1.23)

    # msg_data = [bytes for param 1] + [bytes for param 8000]
    frame = _build_values_frame(client, ts_bytes + float_bytes)

    sent_messages: list[bytes] = []

    def fake_tcp_request(message: bytes, expect_response: bool = True, verbosity: int = 0) -> bytes:
        sent_messages.append(message)
        return frame

    monkeypatch.setattr(client, "_tcp_request", fake_tcp_request)

    result = client.get_values(params, verbosity=0)

    assert sent_messages, "No message was sent"
    assert 1 in result
    assert isinstance(result[1], datetime)
    # Ensure UTC handling is consistent
    assert result[1].replace(tzinfo=timezone.utc) == dt

    assert 8000 in result
    assert isinstance(result[8000], float)
    assert pytest.approx(result[8000], rel=1e-6) == 1.23
