"""Unit tests for NE-300 remote operation utility (no hardware required)."""

from __future__ import annotations

from pathlib import Path
import logging
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
    now = [100.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])

    def advance(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(remote.time, "sleep", advance)
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


def test_unverified_can_dwell_for_set_duration_and_return_to_ambient(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint
) -> None:
    client = remote.AcoemRemote(endpoint)
    modes: list[int] = []
    now = [100.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client, "status", lambda: {4035: 0, 4036: 0})

    def set_mode(mode: int, *, verify_seconds: float = 5.0) -> None:
        modes.append(mode)
        if mode == 1:
            now[0] += 4  # 4035 polling consumed 4 seconds
            raise remote.UnverifiedStateError("4035 remained 0")

    monkeypatch.setattr(client, "set_operation", set_mode)
    monkeypatch.setattr(remote.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    remote.run_modes(client, [(1, 10)], allow_unverified=True)
    assert modes == [1, 0]
    assert now[0] == 110.0  # includes verification time, not 10 more seconds


def test_unverified_rejected_by_default_but_ambient_attempted(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint
) -> None:
    client = remote.AcoemRemote(endpoint)
    modes: list[int] = []
    monkeypatch.setattr(client, "status", lambda: {4035: 0})

    def set_mode(mode: int, **_kwargs: Any) -> None:
        modes.append(mode)
        if mode == 2:
            raise remote.UnverifiedStateError("not reported")

    monkeypatch.setattr(client, "set_operation", set_mode)
    with pytest.raises(remote.UnverifiedStateError, match="not reported"):
        remote.run_modes(client, [(2, 5)])
    assert modes == [2, 0]


def test_allow_unverified_does_not_hide_transport_failure(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint
) -> None:
    client = remote.AcoemRemote(endpoint)
    modes: list[int] = []
    monkeypatch.setattr(client, "status", lambda: {4035: 0})

    def set_mode(mode: int, **_kwargs: Any) -> None:
        modes.append(mode)
        if mode == 1:
            raise ConnectionError("connection failed")

    monkeypatch.setattr(client, "set_operation", set_mode)
    with pytest.raises(ConnectionError, match="connection failed"):
        remote.run_modes(client, [(1, 5)], allow_unverified=True)
    assert modes == [1, 0]


def test_cli_passes_unverified_option_to_timed_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("instruments: {ne300: {driver: ne300, io: {host: example.org, port: 123}}}")
    calls: list[tuple[Any, Any]] = []
    def fake_run(client: Any, phases: Any, **kwargs: Any) -> None:
        calls.append((list(phases), kwargs))
    monkeypatch.setattr(remote, "run_modes", fake_run)
    assert remote.main([
        "-c", str(cfg), "zero", "--seconds", "45", "--experimental", "--allow-unverified"
    ]) == 0
    assert calls == [([(1, 45.0)], {"verify_seconds": 30.0, "allow_unverified": True})]


def test_cli_status_uses_timestamped_logger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The read-only status uses the same timestamped log path as zero/span."""
    cfg = tmp_path / "config.yml"
    cfg.write_text("instruments: {ne300: {driver: ne300, io: {host: example.org, port: 123}}}")
    monkeypatch.setattr(remote.AcoemRemote, "status", lambda self: {4035: 1, 4036: 7})
    with caplog.at_level(logging.INFO, logger="pydaq.ne300_zero_span_control"):
        assert remote.main(["-c", str(cfg), "status"]) == 0
    rows = [record for record in caplog.records if "4035 current_operation" in record.message]
    assert len(rows) == 1
    assert rows[0].levelname == "INFO"
    assert "4035 current_operation=1 (zero), 4036 dio_state=7" in rows[0].message
    # logging timestamps are attached by the UTC logging formatter.
    assert rows[0].created > 0


def test_default_verification_time_is_30_seconds() -> None:
    parser = remote.make_parser()
    args = parser.parse_args(["-c", "mkn.yml", "status"])
    assert args.verify_seconds == 30.0
    assert remote.POLL_SECONDS == 1.0


def test_delayed_4035_readback_is_accepted(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint,
) -> None:
    client = remote.AcoemRemote(endpoint)
    clock = [100.0]
    states = iter([0] * 12 + [1])
    cmds: list[int] = []
    monkeypatch.setattr(remote.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote.time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))

    def respond(command: int, body: bytes, *, expect_response: bool) -> bytes:
        cmds.append(command)
        return b"" if command == 5 else struct.pack(">i", next(states))

    monkeypatch.setattr(client, "_request", respond)
    client.set_operation(1)  # succeeds after twelve seconds, within the 30-second default
    assert clock[0] == 112.0
    assert cmds == [5] + [4] * 13


def test_transient_readback_error_is_retried(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint,
) -> None:
    client = remote.AcoemRemote(endpoint)
    clock = [0.0]
    observations: list[object] = [TimeoutError("slow"), TimeoutError("slow"), 2]
    monkeypatch.setattr(remote.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote.time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))
    monkeypatch.setattr(client, "_request", lambda *a, **k: b"")

    def status_values(parameters: Any) -> dict[int, int]:
        state = observations.pop(0)
        if isinstance(state, BaseException):
            raise state
        return {4035: int(state)}

    monkeypatch.setattr(client, "get_values", status_values)
    client.set_operation(2, verify_seconds=10.0)
    assert clock[0] == 2.0


def test_persistent_readback_error_is_not_mislabeled_unverified(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint,
) -> None:
    client = remote.AcoemRemote(endpoint)
    clock = [0.0]
    monkeypatch.setattr(remote.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(remote.time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))
    monkeypatch.setattr(client, "_request", lambda *a, **k: b"")
    monkeypatch.setattr(client, "get_values", lambda _p: (_ for _ in ()).throw(TimeoutError("offline")))
    with pytest.raises(RuntimeError, match="read-back failed"):
        client.set_operation(1, verify_seconds=2.0)


def test_phase_verification_never_exceeds_requested_duration(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint,
) -> None:
    client = remote.AcoemRemote(endpoint)
    seen: list[tuple[int, float]] = []
    now = [0.0]
    monkeypatch.setattr(client, "status", lambda: {4035: 0})
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(remote.time, "sleep", lambda n: now.__setitem__(0, now[0] + n))

    def record(mode: int, *, verify_seconds: float = 30.0) -> None:
        seen.append((mode, verify_seconds))

    monkeypatch.setattr(client, "set_operation", record)
    remote.run_modes(client, [(1, 3.0)], verify_seconds=30.0)
    assert seen == [(1, 3.0), (0, 30.0)]
    assert now[0] == 3.0


def test_verified_delayed_transition_preserves_phase_deadline(
    monkeypatch: pytest.MonkeyPatch, endpoint: remote.AcoemEndpoint,
) -> None:
    client = remote.AcoemRemote(endpoint)
    now = [100.0]
    modes: list[int] = []
    slept: list[float] = []
    monkeypatch.setattr(client, "status", lambda: {4035: 0, 4036: 0})
    monkeypatch.setattr(remote.time, "monotonic", lambda: now[0])

    def set_mode(mode: int, *, verify_seconds: float = 30.0) -> None:
        modes.append(mode)
        if mode == 1:
            assert verify_seconds == 30.0
            now[0] += 17.0  # NE-300 confirms after 17 seconds

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(client, "set_operation", set_mode)
    monkeypatch.setattr(remote.time, "sleep", sleep)
    remote.run_modes(client, [(1, 30.0)])
    assert modes == [1, 0]
    assert slept == [13.0]
    assert now[0] == 130.0


def test_span_return_retries_the_write_not_only_the_read(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: remote.AcoemEndpoint,
) -> None:
    """A lost ambient command must trigger another command-5 write."""
    client = remote.AcoemRemote(endpoint)
    actions: list[int] = []
    waits: list[float] = []
    monkeypatch.setattr(client, "status", lambda: {4035: 0, 4036: 3})

    def fake_set(mode: int, *, verify_seconds: float = 30) -> None:
        actions.append(mode)
        if mode == 0 and actions.count(0) == 1:
            raise remote.UnverifiedStateError("4035 stayed in span")

    monkeypatch.setattr(client, "set_operation", fake_set)
    monkeypatch.setattr(remote.time, "sleep", lambda n: waits.append(n))
    monkeypatch.setattr(remote.time, "monotonic", lambda: 100.0)
    remote.run_modes(client, [(2, 0.01)])
    assert actions == [2, 0, 0]
    assert remote.AMBIENT_RETRY_DELAY_SECONDS in waits


def test_three_unconfirmed_ambient_writes_raise_critical_error(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: remote.AcoemEndpoint,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = remote.AcoemRemote(endpoint)
    commands: list[int] = []

    def fake_set(mode: int, *, verify_seconds: float = 30) -> None:
        commands.append(mode)
        raise remote.UnverifiedStateError("4035 remains 2")

    monkeypatch.setattr(client, "set_operation", fake_set)
    monkeypatch.setattr(remote.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="Ambient return not verified"):
        remote.ensure_ambient(client)
    assert commands == [0, 0, 0]
    assert "NE-300 MAY REMAIN IN ZERO/SPAN" in caplog.text


def test_ambient_status_check_retries_even_if_first_write_poll_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: remote.AcoemEndpoint,
) -> None:
    client = remote.AcoemRemote(endpoint)
    commands: list[int] = []
    states = iter([{4035: 2, 4036: 3}, {4035: 0, 4036: 2}])
    monkeypatch.setattr(client, "status", lambda: next(states))
    monkeypatch.setattr(client, "set_operation", lambda mode, **kwargs: commands.append(mode))
    monkeypatch.setattr(remote.time, "sleep", lambda _: None)
    remote.ensure_ambient(client)
    assert commands == [0, 0]


def test_cli_explicit_ambient_uses_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("instruments: {ne300: {driver: ne300, io: {host: example.org, port: 123}}}")
    calls: list[float] = []

    def fake_ambient(_client: Any, *, verify_seconds: float = 30.0) -> None:
        calls.append(verify_seconds)

    monkeypatch.setattr(remote, "ensure_ambient", fake_ambient)
    assert remote.main(["-c", str(cfg), "ambient", "--experimental"]) == 0
    assert calls == [30.0]
