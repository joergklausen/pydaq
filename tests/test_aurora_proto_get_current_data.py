from instr.ecotech.aurora_proto import AuroraClient
from tests.conftest import DummyNeph


def test_get_current_data_uses_vi099_socket(dummy_driver, monkeypatch):
    """
    When communicating via socket, AuroraClient should send VI{ID:02d}99
    (e.g. VI03099 for serial_id == 3).
    """
    client = AuroraClient(dummy_driver, params={"serial_id": 3})

    sent_cmds: list[str] = []

    def fake_send(cmd: str, expect_response: bool = True) -> str:
        sent_cmds.append(cmd)
        # Return something that looks like VI099 output
        return "2025-01-01 00:00:00, 1.0, 2.0, 0A"

    # We don't actually care about serial vs socket here; just check the command
    monkeypatch.setattr(client, "_send", fake_send)

    line = client.get_current_data(sep=",")

    assert sent_cmds == ["VI0399"]  # station ID 3 -> "VI03"
    assert line.startswith("2025-01-01 00:00:00")
    assert ",1.0,2.0,0A" in line


def test_get_current_data_uses_vi099_serial(monkeypatch):
    """
    When communicating via serial, AuroraClient should send plain VI099.
    """
    drv = DummyNeph()
    # Pretend this driver is configured for serial comms
    monkeypatch.setattr(drv, "_use_serial", lambda: True)

    client = AuroraClient(drv, params={"serial_id": 3})  # type: ignore[arg-type]

    sent_cmds: list[str] = []

    def fake_send(cmd: str, expect_response: bool = True) -> str:
        sent_cmds.append(cmd)
        return "2025-01-01 00:00:00, 1.0, 2.0, 0A"

    monkeypatch.setattr(client, "_send", fake_send)

    line = client.get_current_data(sep=",")

    assert sent_cmds == ["VI099"]
    assert line.startswith("2025-01-01 00:00:00")
