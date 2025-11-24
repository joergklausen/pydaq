# tests/instr/test_acoem_proto_timestamps.py
from datetime import datetime, timezone

from instr.ecotech.acoem_proto import AcoemClient


def test_acoem_timestamp_roundtrip(dummy_driver):
    client = AcoemClient(dummy_driver, params={"serial_id": 1})

    original = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    packed = client._datetime_to_timestamp(original)
    unpacked = client._timestamp_to_datetime(int.from_bytes(packed, "big", signed=False))

    assert unpacked.year == original.year
    assert unpacked.month == original.month
    assert unpacked.day == original.day
    assert unpacked.hour == original.hour
    assert unpacked.minute == original.minute
    assert unpacked.second == original.second
