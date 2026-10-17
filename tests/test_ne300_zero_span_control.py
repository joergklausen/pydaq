"""Unit tests for NE-300 remote operation utility (no hardware required)."""

from __future__ import annotations

from pathlib import Path
import struct
from typing import Any

import pytest

from pydaq.utils import ne300_zero_span_control as remote


def _reply(command: int, body: bytes, serial_id: int = 0) -> bytes:
    prefix = bytes([2, serial_id, command, 3]) + len(body).to_bytes(2, "big") + body
    return prefix + bytes([remote.AcoemRemote._checksum(prefix), 4])


class FakeSocket:
    def __init__(self, response: bytes = b"", chunksize: int = 3) -> None:
        self.response = response
        self.chunksize = chunksize
        self.sent: list[bytes] = []
        self.timeout: float | None = None
        self.read_count = 0

    def __enter__(self) -> FakeSocket:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, requested: int) -> bytes:
        self.read_count += 1
        result = self.response[: min(requested, self.chunksize)]
        self.response = self.response[len(result) :]
        return result


@pytest.fixture
def endpoint() -> remote.AcoemEndpoint:
    return remote.AcoemEndpoint("example.org", 32783, 0)


def test_load_endpoint_uses_mkn_yaml_shape(tmp_path: Path) -> None:
    config = tmp_path / "mkn.yml"
    config.write_text(
        "instruments:\n  ne300:\n    driver: ne300\n    id: 0\n"
        "    io:\n      kind: tcp\n      protocol: acoem\n"
        "      host: 192.0.2.12\n      port: 32783\n      timeout_seconds: 2\n",
        encoding="utf-8",
    )
    assert remote.load_endpoint(config) == remote.AcoemEndpoint("192.0.2.12", 32783, 0, 2)


def test_wrong_instrument_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "mkn.yml"
    config.write_text("instruments:\n  ne300:\n    driver: 49i\n    io: {}\n")
    with pytest.raises(ValueError, match="driver: ne300"):
        remote.load_endpoint(config)


def test_status_reads_4035_and_4036(monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint) -> None:
    fake = FakeSocket(_reply(4, struct.pack(">ii", 1, 7)))
    monkeypatch.setattr(remote.socket, "create_connection", lambda *_args, **_kwargs: fake)
    assert remote.AcoemRemote(endpoint).status() == {4035: 1, 4036: 7}
    assert fake.sent == [remote.AcoemRemote(endpoint)._frame(4, struct.pack(">ii", 4035, 4036))]


def test_command_5_sends_without_reading(monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint) -> None:
    fake = FakeSocket()
    monkeypatch.setattr(remote.socket, "create_connection", lambda *_args, **_kwargs: fake)
    client = remote.AcoemRemote(endpoint)
    assert client._request(5, struct.pack(">ii", 4035, 1), expect_response=False) == b""
    assert fake.sent == [client._frame(5, struct.pack(">ii", 4035, 1))]
    assert fake.read_count == 0


def test_bad_checksum_is_rejected(monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint) -> None:
    packet = bytearray(_reply(4, struct.pack(">i", 0)))
    packet[-2] ^= 0xFF
    monkeypatch.setattr(
        remote.socket, "create_connection", lambda *_args, **_kwargs: FakeSocket(bytes(packet))
    )
    with pytest.raises(ValueError, match="checksum"):
        remote.AcoemRemote(endpoint).get_values([4035])


def test_error_response_is_rejected(monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint) -> None:
    monkeypatch.setattr(
        remote.socket, "create_connection", lambda *_args, **_kwargs: FakeSocket(_reply(0, b"\x05"))
    )
    with pytest.raises(RuntimeError, match="error 5"):
        remote.AcoemRemote(endpoint).get_values([4035])


def test_unverified_write_fails(monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint) -> None:
    client = remote.AcoemRemote(endpoint)
    requests: list[int] = []

    def _fake_request(command: int, body: bytes, *, expect_response: bool) -> bytes:
        requests.append(command)
        return b"" if command == 5 else struct.pack(">i", 0)

    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setattr(remote.time, "monotonic", iter([0.0, 1.0]).__next__)
    with pytest.raises(RuntimeError, match="no evidence"):
        client.set_operation(1, verify_seconds=0.5)
    assert requests == [5, 4]


def test_failed_phase_attempts_to_restore_ambient(monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint) -> None:
    client = remote.AcoemRemote(endpoint)
    actions: list[int] = []
    monkeypatch.setattr(client, "status", lambda: {4035: 0})

    def fake_set_operation(mode: int, *, verify_seconds: float = 5.0) -> None:
        actions.append(mode)
        if mode == 1:
            raise RuntimeError("zero failed")

    monkeypatch.setattr(client, "set_operation", fake_set_operation)
    with pytest.raises(RuntimeError, match="zero failed"):
        remote.run_modes(client, [(1, 1.0)])
    assert actions == [1, 0]


def test_nonambient_start_is_blocked(monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint) -> None:
    client = remote.AcoemRemote(endpoint)
    monkeypatch.setattr(client, "status", lambda: {4035: 2})
    with pytest.raises(RuntimeError, match="Refusing"):
        remote.run_modes(client, [(1, 1.0)])


def test_cli_status_never_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("instruments: {ne300: {driver: ne300, io: {host: example.org, port: 123}}}")
    monkeypatch.setattr(remote.AcoemRemote, "status", lambda self: {4035: 0, 4036: 9})
    def prohibit(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unexpected write")
    monkeypatch.setattr(remote.AcoemRemote, "set_operation", prohibit)
    assert remote.main(["-c", str(cfg), "status"]) == 0


def test_cli_rejects_state_without_experimental(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("instruments: {ne300: {driver: ne300, io: {host: example.org, port: 123}}}")
    with pytest.raises(SystemExit) as result:
        remote.main(["-c", str(cfg), "zero", "--seconds", "30"])
    assert result.value.code == 2


def test_phase_sequence_restores_ambient(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint
) -> None:
    client = remote.AcoemRemote(endpoint)
    modes: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(client, "status", lambda: {4035: 0})
    monkeypatch.setattr(
        client, "set_operation", lambda mode, *, verify_seconds=5.0: modes.append(mode)
    )
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: sleeps.append(seconds))
    remote.run_modes(client, [(1, 2.5), (2, 3.0)])
    assert modes == [1, 2, 0]
    assert sleeps == [2.5, 3.0]


def test_keyboard_interrupt_attempts_ambient(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint
) -> None:
    client = remote.AcoemRemote(endpoint)
    modes: list[int] = []
    monkeypatch.setattr(client, "status", lambda: {4035: 0})
    monkeypatch.setattr(
        client, "set_operation", lambda mode, *, verify_seconds=5.0: modes.append(mode)
    )

    def interrupted_sleep(_seconds: float) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(remote.time, "sleep", interrupted_sleep)
    with pytest.raises(KeyboardInterrupt):
        remote.run_modes(client, [(1, 20.0)])
    assert modes == [1, 0]


def test_malformed_reply_serial_id_rejected(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint
) -> None:
    monkeypatch.setattr(
        remote.socket,
        "create_connection",
        lambda *_args, **_kwargs: FakeSocket(_reply(4, struct.pack(">i", 1), 22)),
    )
    with pytest.raises(ValueError, match="serial ID"):
        remote.AcoemRemote(endpoint).get_values([4035])


def test_truncated_reply_rejected(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint
) -> None:
    monkeypatch.setattr(
        remote.socket,
        "create_connection",
        lambda *_args, **_kwargs: FakeSocket(_reply(4, struct.pack(">i", 1))[:-3]),
    )
    with pytest.raises(ConnectionError, match="closed during response"):
        remote.AcoemRemote(endpoint).get_values([4035])
