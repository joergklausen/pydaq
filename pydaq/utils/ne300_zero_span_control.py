"""Standalone, experimental ACOEM NE-300 operating-state utility.

This is NOT a calibration utility. ACOEM command 5 has no response, and
parameter 4035 (CURRENT_OPERATION) is documented as an operating-state
indicator, not explicitly as a writable valve control. State changes used by
the legacy mkndaq driver are retained here only as an *experimental* feature.

Use::

    python -m pydaq.utils.ne300_zero_span_control -c pydaq/configs/mkn.yml status
    python -m pydaq.utils.ne300_zero_span_control -c pydaq/configs/mkn.yml zero --seconds 360 --experimental

Run on the station PC. Disable only ne300 in pydaq during tests.
A TeamViewer disconnect should not stop the locally running command.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
from pathlib import Path
import socket
import struct
import time
from typing import Any, Mapping, Sequence

import yaml


CURRENT_OPERATION = 4035
DIO_STATE = 4036
MODES = {0: "ambient", 1: "zero", 2: "span"}
_TELNET_NEGOTIATION = b"\xff\xfb\x01\xff\xfe\x01\xff\xfb\x03"
_LOG = logging.getLogger("pydaq.ne300_remote")


@dataclass(frozen=True)
class AcoemEndpoint:
    """Validated ACOEM TCP endpoint from station configuration."""

    host: str
    port: int
    serial_id: int
    timeout_seconds: float = 5.0
    read_max_bytes: int = 65536

    def __post_init__(self) -> None:
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("ACOEM requires a valid host and TCP port")
        if not 0 <= self.serial_id <= 255:
            raise ValueError("ACOEM serial ID must be between 0 and 255")
        if self.timeout_seconds <= 0 or self.read_max_bytes < 8:
            raise ValueError("Invalid ACOEM timeout or read_max_bytes")


def load_endpoint(config_path: Path, instrument: str = "ne300") -> AcoemEndpoint:
    """Read only NE-300 TCP settings from the existing pydaq YAML file.

    Args:
        config_path: Full path to the station YAML file.
        instrument: Instrument entry name, normally ``ne300``.

    Returns:
        A validated TCP endpoint. No instrument I/O occurs here.
    """
    with config_path.expanduser().open(encoding="utf-8") as handle:
        config: Any = yaml.safe_load(handle)
    if not isinstance(config, Mapping):
        raise ValueError("Station configuration must be a YAML mapping")
    instruments = config.get("instruments")
    if not isinstance(instruments, Mapping) or instrument not in instruments:
        raise ValueError(f"Instrument {instrument!r} is not configured")
    item = instruments[instrument]
    if not isinstance(item, Mapping) or str(item.get("driver", "")).lower() != "ne300":
        raise ValueError(f"Instrument {instrument!r} must use driver: ne300")
    io = item.get("io")
    if not isinstance(io, Mapping):
        raise ValueError(f"Instrument {instrument!r} is missing io settings")
    if str(io.get("protocol", "acoem")).lower() != "acoem":
        raise ValueError("NE-300 remote control requires io.protocol: acoem")
    if str(io.get("kind", "tcp")).lower() not in {"tcp", "socket", "tcpip"}:
        raise ValueError("NE-300 remote control requires TCP communication")
    return AcoemEndpoint(
        host=str(io.get("host", "")).strip(),
        port=int(io.get("port", 0)),
        serial_id=int(item.get("id", item.get("serial_id", 0))),
        timeout_seconds=float(io.get("timeout_seconds", io.get("timeout", 5))),
        read_max_bytes=int(io.get("read_max_bytes", 65536)),
    )


class UnverifiedStateError(RuntimeError):
    """A state write was sent but 4035 did not confirm the requested mode."""


class AcoemRemote:
    """Minimal independent ACOEM command-4 reader / command-5 sender.

    Does not construct a pydaq Instrument, launch a worker or touch data files.
    """

    def __init__(self, endpoint: AcoemEndpoint) -> None:
        self.endpoint = endpoint

    @staticmethod
    def _checksum(message: bytes) -> int:
        result = 0
        for byte in message:
            result ^= byte
        return result

    def _frame(self, command: int, body: bytes) -> bytes:
        prefix = (
            bytes([2, self.endpoint.serial_id, command, 3])
            + len(body).to_bytes(2, "big")
            + body
        )
        return prefix + bytes([self._checksum(prefix), 4])

    def _request(self, command: int, body: bytes, *, expect_response: bool) -> bytes:
        """Send one packet; CMD 5 deliberately does not await an ACK.

        Args:
            command: ACOEM command number (4 or 5).
            body: Binary command payload.
            expect_response: True for CMD 4, False for CMD 5.

        Returns:
            Validated reply payload, or empty bytes for a CMD-5 write.
        """
        message = self._frame(command, body)
        endpoint = self.endpoint
        frame = b""
        with socket.create_connection(
            (endpoint.host, endpoint.port), timeout=endpoint.timeout_seconds
        ) as connection:
            connection.settimeout(endpoint.timeout_seconds)
            connection.sendall(message)
            if not expect_response:
                return b""  # Appendix A.3.6: there is no CMD-5 response.
            chunks: list[bytes] = []
            while True:
                raw = b"".join(chunks).replace(_TELNET_NEGOTIATION, b"")
                if len(raw) >= 6:
                    body_length = int.from_bytes(raw[4:6], "big")
                    frame_length = 8 + body_length
                    if frame_length > endpoint.read_max_bytes:
                        raise ValueError("ACOEM reply exceeds configured maximum")
                    if len(raw) >= frame_length:
                        frame = raw[:frame_length]
                        break
                if len(raw) >= endpoint.read_max_bytes:
                    raise ValueError("ACOEM reply exceeds configured maximum")
                chunk = connection.recv(min(4096, endpoint.read_max_bytes - len(raw)))
                if not chunk:
                    raise ConnectionError("ACOEM connection closed during response")
                chunks.append(chunk)
        if frame[0] != 2 or frame[1] != endpoint.serial_id or frame[3] != 3:
            raise ValueError("Malformed ACOEM reply header or serial ID")
        if frame[-1] != 4 or frame[-2] != self._checksum(frame[:-2]):
            raise ValueError("Malformed ACOEM reply checksum or EOT")
        reply_command = frame[2]
        if reply_command == 0:
            error_code = frame[6] if len(frame) > 8 else "unknown"
            raise RuntimeError(f"Instrument returned ACOEM error {error_code}")
        if reply_command != command:
            raise ValueError(f"Expected ACOEM reply {command}, got {reply_command}")
        return frame[6:-2]

    def get_values(self, parameters: Sequence[int]) -> dict[int, int]:
        """Query signed integer status parameters with ACOEM command 4."""
        if not parameters:
            return {}
        payload = b"".join(int(parameter).to_bytes(4, "big") for parameter in parameters)
        body = self._request(4, payload, expect_response=True)
        if len(body) != 4 * len(parameters):
            raise ValueError(
                f"ACOEM read returned {len(body)} bytes for {len(parameters)} values"
            )
        return {
            parameter: struct.unpack_from(">i", body, offset=4 * index)[0]
            for index, parameter in enumerate(parameters)
        }

    def status(self) -> dict[int, int]:
        """Read operation and digital I/O state without changing the instrument."""
        return self.get_values([CURRENT_OPERATION, DIO_STATE])

    def set_operation(
        self,
        mode: int,
        *,
        verify_seconds: float = 5.0,
        poll_seconds: float = 0.25,
    ) -> None:
        """Experimentally request an operating state and verify its read-back.

        The protocol permits writing parameters, but the manual does not
        establish that 4035 is writable. A read-back confirms only a reported
        operation state; it does NOT prove physical valve position.

        Args:
            mode: 0 ambient, 1 zero, or 2 span.
            verify_seconds: Maximum read-back polling time.
            poll_seconds: Interval between read-back queries.

        Raises:
            RuntimeError: If the requested state is not reported.
        """
        if mode not in MODES:
            raise ValueError(f"Invalid operation state: {mode}")
        if verify_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("Verification timing must be positive")
        body = CURRENT_OPERATION.to_bytes(4, "big") + struct.pack(">i", mode)
        _LOG.warning("Sending EXPERIMENTAL 4035=%d (%s); no ACK is expected", mode, MODES[mode])
        self._request(5, body, expect_response=False)
        deadline = time.monotonic() + verify_seconds
        while True:
            reported = self.get_values([CURRENT_OPERATION])[CURRENT_OPERATION]
            if reported == mode:
                _LOG.info("Read-back 4035=%d (%s)", reported, MODES[mode])
                return
            if time.monotonic() >= deadline:
                raise UnverifiedStateError(
                    f"Cannot verify operation change to {MODES[mode]}: "
                    f"4035 still reports {reported}; no evidence that command 5 took effect"
                )
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def run_modes(
    client: AcoemRemote,
    phases: Sequence[tuple[int, float]],
    *,
    verify_seconds: float = 5.0,
    allow_unverified: bool = False,
) -> None:
    """Request zero/span for bounded durations and always request ambient.

    This runs in the foreground on the station PC; dropping the TeamViewer
    connection does not stop the locally running Python process.

    ``allow_unverified`` only permits waiting through a reported-state mismatch.
    It does not suppress a network/protocol error or prove valve movement.
    """
    if not phases or any(mode not in (1, 2) or seconds <= 0 for mode, seconds in phases):
        raise ValueError("Only finite, positive-duration zero/span phases are supported")
    if sum(seconds for _, seconds in phases) > 3600:
        raise ValueError("Combined zero/span duration must not exceed 3600 seconds")
    before = client.status()
    if before[CURRENT_OPERATION] != 0:
        raise RuntimeError(
            f"Refusing to interrupt instrument: current operation is "
            f"{before[CURRENT_OPERATION]}, not ambient (0)"
        )
    _LOG.info(
        "Initial status: 4035=%s, 4036=%s; %d timed phase(s)",
        before[CURRENT_OPERATION], before.get(DIO_STATE, "?"), len(phases)
    )
    try:
        for mode, duration in phases:
            # Bound the state-request interval from the moment we begin the
            # write, including any time spent polling 4035 for verification.
            phase_deadline = time.monotonic() + duration
            try:
                client.set_operation(mode, verify_seconds=verify_seconds)
            except UnverifiedStateError as exc:
                if not allow_unverified:
                    raise
                _LOG.warning(
                    "Requested %s NOT VERIFIED: %s; continuing timed diagnostic "
                    "only because --allow-unverified was specified",
                    MODES[mode], exc,
                )
            remaining = max(0.0, phase_deadline - time.monotonic())
            _LOG.warning(
                "Requested %s (NOT a calibration); ambient or next phase in "
                "%.1f seconds", MODES[mode], remaining,
            )
            if remaining:
                time.sleep(remaining)
    finally:
        # Even if a phase fails or Ctrl+C interrupts time.sleep, try ambient.
        # Cannot recover from power failure, forced termination, or a rejected
        # command: manual/instrument-side fail-safe must cover those cases.
        _LOG.warning("Requesting return to ambient now (4035=0)")
        try:
            client.set_operation(0, verify_seconds=verify_seconds)
        except Exception as exc:
            _LOG.critical(
                "Ambient return NOT VERIFIED: %s; inspect instrument locally", exc
            )
            raise
        _LOG.warning(
            "4035 reports ambient after return request; physical valve position not proven"
        )


def _positive_seconds(value: str) -> float:
    result = float(value)
    if not 0 < result <= 3600:
        raise argparse.ArgumentTypeError("Duration must be > 0 and <= 3600 seconds")
    return result


def make_parser() -> argparse.ArgumentParser:
    """Build the standalone command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Experimental NE-300 ACOEM operating-state commands. "
            "Does NOT start calibrations or change calibration coefficients. "
            "Parameter 4035 is not documented as writable."
        )
    )
    parser.add_argument("-c", "--config", type=Path, required=True, help="Station YAML")
    parser.add_argument("--instrument", default="ne300", help="Instrument config key")
    parser.add_argument("--verify-seconds", type=_positive_seconds, default=5.0)
    parser.add_argument("--log-file", type=Path, help="Optional persistent operations log")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status", help="Read 4035/4036 only (safe default)")
    ambient = commands.add_parser("ambient", help="EXPERIMENTAL request for ambient state")
    ambient.add_argument("--experimental", action="store_true")
    for name in ("zero", "span"):
        command = commands.add_parser(name, help=f"EXPERIMENTAL timed {name} state (NOT calibration)")
        command.add_argument("--seconds", type=_positive_seconds, required=True)
        command.add_argument("--experimental", action="store_true")
        command.add_argument(
            "--allow-unverified", action="store_true",
            help="Continue timed diagnostic even if 4035 does not confirm the state",
        )
    sequence = commands.add_parser("zero-span", help="EXPERIMENTAL states, NOT a zero/span calibration")
    sequence.add_argument("--zero-seconds", type=_positive_seconds, required=True)
    sequence.add_argument("--span-seconds", type=_positive_seconds, required=True)
    sequence.add_argument("--experimental", action="store_true")
    sequence.add_argument("--allow-unverified", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one explicit command, returning a shell-compatible exit status."""
    parser = make_parser()
    args = parser.parse_args(argv)
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler()])
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    if args.log_file is not None:
        path = args.log_file.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(formatter)
        _LOG.addHandler(handler)
        _LOG.setLevel(logging.INFO)
    try:
        endpoint = load_endpoint(args.config, args.instrument)
        client = AcoemRemote(endpoint)
        if args.action == "status":
            values = client.status()
            mode = values[CURRENT_OPERATION]
            print(
                f"4035 current_operation={mode} ({MODES.get(mode, 'other')}), "
                f"4036 dio_state={values[DIO_STATE]}"
            )
            return 0
        if not args.experimental:
            parser.error("State writes are UNCONFIRMED for 4035; add --experimental to attempt one")
        _LOG.warning("This operation is not a calibration and does not update coefficients")
        if args.action == "ambient":
            client.set_operation(0, verify_seconds=args.verify_seconds)
        elif args.action in {"zero", "span"}:
            mode = 1 if args.action == "zero" else 2
            run_modes(
                client, [(mode, args.seconds)],
                verify_seconds=args.verify_seconds,
                allow_unverified=args.allow_unverified,
            )
        else:
            run_modes(
                client,
                [(1, args.zero_seconds), (2, args.span_seconds)],
                verify_seconds=args.verify_seconds,
                allow_unverified=args.allow_unverified,
            )
        return 0
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
        _LOG.error("NE-300 remote operation failed: %s", exc)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
