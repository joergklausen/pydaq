# tests/instr/test_acoem_proto_build_message.py
from instr.ecotech.acoem_proto import AcoemClient


def test_build_message_layout(dummy_driver):
    client = AcoemClient(dummy_driver, params={"serial_id": 7})
    payload = b"\x01\x02\x03\x04"
    msg = client._build_message(command=4, parameter_id=1234, payload=payload)

    # Basic structure
    assert msg[0] == 2  # STX
    assert msg[1] == 7  # SID
    assert msg[2] == client.CMD_GET_VALUES
    assert msg[3] == 3  # ETX

    msg_len = int.from_bytes(msg[4:6], "big")
    msg_data = msg[6 : 6 + msg_len]
    checksum = msg[6 + msg_len]
    eot = msg[7 + msg_len]

    assert eot == 4  # EOT
    assert checksum == client._checksum(msg[: 6 + msg_len])[0]
    # First 4 bytes of msg_data are the parameter_id
    param_id = int.from_bytes(msg_data[:4], "big")
    assert param_id == 1234
    # Remaining payload matches what we passed in
    assert msg_data[4:] == payload
    assert len(msg) == 2 + 4 + msg_len + 1 + 1  # header + msg_data + checksum + EOT
