"""Hardware integration tests for the Ecotech Aurora 3000 serial interface.

These tests validate that the Aurora 3000 configuration in a station YAML file
is safe to deploy and that the configured serial port can be used for real
instrument communication.  They are intended for Raspberry Pi / field-system
checks, not for ordinary unit-test runs.

The tests perform three levels of validation:

1. Configuration validation:
   - locate exactly one Aurora 3000 instrument in ``--station-config``;
   - confirm that it is configured with ``io.kind: serial``;
   - check that the configured serial device is not also assigned to another
     enabled non-shared instrument.

2. Port-ownership validation:
   - verify that the configured serial device exists;
   - attempt to open it with pyserial exclusive access where supported;
   - report ``lsof`` / ``fuser`` output when another process appears to hold
     the device.

3. Instrument communication validation:
   - send ``ID{id}`` as a diagnostic command;
   - send ``VI{id}99`` and confirm that the response has the expected Aurora
     shape: timestamp plus twelve data values.

Run from the repository root, for example:

    pytest -vv -rs -s tests/test_ecotech_aurora_serial_integration.py \\
        --station-config ./pydaq/configs/nrb.yml

The hardware communication tests are skipped when the Aurora instrument is
disabled in the station config.  Enable ``instruments.aurora3000.enabled`` only
when the instrument is physically connected and the configured serial device is
correct.

These tests require ``pyserial`` and are marked ``integration``.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

pytestmark = pytest.mark.integration

AURORA_DRIVER_NAMES = {"aurora3000", "aurora", "ecotech", "neph"}
ALLOWED_SHARED_SERIAL_DRIVERS = {"hmpascii", "vaisala", "hmp110", "hmp60"}


def _load_yaml(path: Path) -> dict[str, Any]:
    yaml = importlib.import_module("yaml")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise AssertionError(f"Expected a YAML mapping in {path}, got {type(loaded).__name__}.")
    return loaded


def _station_config_path(pytestconfig: pytest.Config) -> Path:
    try:
        raw = pytestconfig.getoption("--station-config", default=None)
    except (AttributeError, ValueError):
        raw = None

    if not raw:
        pytest.skip("Pass --station-config ./pydaq/configs/nrb.yml to run the Aurora serial integration test.")

    path = Path(str(raw)).expanduser().resolve()
    if not path.exists():
        raise AssertionError(f"Station config does not exist: {path}")
    return path


def _instruments(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("instruments")
    if not isinstance(raw, Mapping):
        raise AssertionError("Station config has no 'instruments' mapping.")

    out: dict[str, dict[str, Any]] = {}
    for name, payload in raw.items():
        if isinstance(payload, Mapping):
            out[str(name)] = dict(payload)
    return out


def _boolish(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def _driver_name(instrument: Mapping[str, Any]) -> str:
    return str(instrument.get("driver", "")).strip().lower()


def _io_config(instrument: Mapping[str, Any]) -> dict[str, Any]:
    io = instrument.get("io", {})
    return dict(io) if isinstance(io, Mapping) else {}


def _io_kind(instrument: Mapping[str, Any]) -> str:
    return str(_io_config(instrument).get("kind", "")).strip().lower()


def _serial_device(instrument: Mapping[str, Any]) -> str:
    io = _io_config(instrument)
    return str(io.get("device", io.get("port", ""))).strip()


def _aurora_instrument(config: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    matches: list[tuple[str, dict[str, Any]]] = []
    for name, instrument in _instruments(config).items():
        driver = _driver_name(instrument)
        protocol = str(instrument.get("protocol", "")).strip().lower()
        if name.lower() == "aurora3000" or driver in AURORA_DRIVER_NAMES or protocol == "aurora":
            matches.append((name, instrument))

    if not matches:
        raise AssertionError("No Aurora 3000 instrument found in station config.")
    if len(matches) > 1:
        names = ", ".join(name for name, _ in matches)
        raise AssertionError(f"Expected exactly one Aurora 3000 instrument, found: {names}")
    return matches[0]


def _serial_users(config: Mapping[str, Any], device: str) -> list[tuple[str, str, bool]]:
    users: list[tuple[str, str, bool]] = []
    for name, instrument in _instruments(config).items():
        if _io_kind(instrument) != "serial":
            continue
        if _serial_device(instrument) != device:
            continue
        users.append((name, _driver_name(instrument), _boolish(instrument.get("enabled"), default=True)))
    return users


def _processes_using_path(path: str) -> str:
    """Return best-effort process information for a serial device."""
    commands: list[list[str]] = []
    if shutil.which("lsof"):
        commands.append(["lsof", "-w", path])
    if shutil.which("fuser"):
        commands.append(["fuser", "-v", path])

    output: list[str] = []
    for command in commands:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        text = "\n".join(part for part in (completed.stdout, completed.stderr) if part.strip())
        if text.strip():
            output.append(f"$ {' '.join(command)}\n{text.strip()}")
    return "\n\n".join(output)


def _serial_open_kwargs(instrument: Mapping[str, Any]) -> dict[str, Any]:
    io = _io_config(instrument)
    device = _serial_device(instrument)
    if not device:
        raise AssertionError("Aurora 3000 serial config requires io.device or io.port.")

    kwargs: dict[str, Any] = {
        "port": device,
        "baudrate": int(io.get("baudrate", 19200)),
        "bytesize": int(io.get("bytesize", 8)),
        "parity": str(io.get("parity", "N")).upper(),
        "stopbits": float(io.get("stopbits", 1)),
        "timeout": float(io.get("timeout_seconds", io.get("timeout", 5))),
        "write_timeout": float(io.get("write_timeout_seconds", io.get("timeout_seconds", io.get("timeout", 5)))),
    }

    # pyserial supports exclusive access on POSIX.  It is ignored on platforms
    # where pyserial does not implement it; the fallback is the lsof/fuser check.
    if sys.platform.startswith(("linux", "darwin", "freebsd")):
        kwargs["exclusive"] = True
    return kwargs


def _read_until_idle(ser: Any, *, total_timeout: float, idle_timeout: float = 0.25) -> str:
    deadline = time.monotonic() + max(total_timeout, 0.5)
    idle_deadline: float | None = None
    buf = bytearray()

    while time.monotonic() < deadline:
        waiting = int(getattr(ser, "in_waiting", 0) or 0)
        if waiting > 0:
            buf.extend(ser.read(waiting))
            idle_deadline = time.monotonic() + idle_timeout
            continue

        if idle_deadline is not None and time.monotonic() >= idle_deadline:
            break
        chunk = ser.read(1)
        if chunk:
            buf.extend(chunk)
            idle_deadline = time.monotonic() + idle_timeout
            continue
        if idle_deadline is not None and time.monotonic() >= idle_deadline:
            break

    return bytes(buf).decode("latin-1", errors="replace").strip()


def _request(ser: Any, command: str, *, timeout_seconds: float) -> str:
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception:
        pass
    ser.write(f"{command}\r".encode("ascii"))
    ser.flush()
    return _read_until_idle(ser, total_timeout=timeout_seconds, idle_timeout=0.35)


def _last_non_empty_line(text: str) -> str:
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]
    return lines[-1] if lines else ""


def _assert_vi099_shape(response: str) -> None:
    line = _last_non_empty_line(response).replace(", ", ",")
    parts = [part.strip() for part in line.split(",")]
    assert len(parts) >= 13, (
        "Aurora VI099 response did not contain timestamp + 12 values.\n"
        f"Last line: {line!r}\nFull response: {response!r}"
    )

    # Timestamp should parse as ISO-like date/time.
    timestamp = parts[0].replace("T", " ")
    try:
        datetime_format = "%Y-%m-%d %H:%M:%S" if "-" in timestamp else "%d/%m/%Y %H:%M:%S"
        time.strptime(timestamp[:19], datetime_format)
    except ValueError as exc:
        raise AssertionError(f"Could not parse Aurora VI099 timestamp {parts[0]!r}.") from exc

    for value in parts[1:12]:
        float(value)

    dio = parts[12]
    try:
        int(dio, 16)
    except ValueError:
        int(float(dio))


def test_aurora3000_serial_port_is_unique_in_station_config(pytestconfig: pytest.Config) -> None:
    config = _load_yaml(_station_config_path(pytestconfig))
    name, aurora = _aurora_instrument(config)

    assert _io_kind(aurora) == "serial", f"{name} must use io.kind: serial for this integration test."
    device = _serial_device(aurora)
    assert device, f"{name} requires io.device or io.port."

    users = _serial_users(config, device)
    enabled_non_shared = [
        (user_name, driver)
        for user_name, driver, enabled in users
        if enabled and user_name != name and driver not in ALLOWED_SHARED_SERIAL_DRIVERS
    ]
    assert not enabled_non_shared, (
        f"Aurora serial port {device} is also configured for enabled non-shared instruments: "
        f"{enabled_non_shared}. This can cause missing records or port contention."
    )


def test_aurora3000_serial_port_can_be_opened_exclusively(pytestconfig: pytest.Config) -> None:
    serial_mod = pytest.importorskip("serial")
    config = _load_yaml(_station_config_path(pytestconfig))
    name, aurora = _aurora_instrument(config)

    if not _boolish(aurora.get("enabled"), default=False):
        pytest.skip(f"{name} is disabled in the station config; enable it before running the hardware test.")

    kwargs = _serial_open_kwargs(aurora)
    device = str(kwargs["port"])
    assert Path(device).exists(), f"Configured Aurora serial device does not exist: {device}"

    holders = _processes_using_path(device)
    try:
        with serial_mod.Serial(**kwargs):
            pass
    except Exception as exc:
        detail = f"\nProcesses currently using {device}:\n{holders}" if holders else ""
        pytest.fail(f"Could not open Aurora serial port {device!r} exclusively: {exc}{detail}")


def test_aurora3000_vi099_returns_parseable_record(pytestconfig: pytest.Config) -> None:
    serial_mod = pytest.importorskip("serial")
    config = _load_yaml(_station_config_path(pytestconfig))
    name, aurora = _aurora_instrument(config)

    if not _boolish(aurora.get("enabled"), default=False):
        pytest.skip(f"{name} is disabled in the station config; enable it before running the hardware test.")

    kwargs = _serial_open_kwargs(aurora)
    timeout_seconds = float(kwargs.get("timeout", 5.0))
    serial_id_raw = aurora.get("id", aurora.get("serial_id", 0))
    serial_id = 0 if serial_id_raw in (None, "") else int(serial_id_raw)
    device = str(kwargs["port"])

    holders = _processes_using_path(device)
    try:
        with serial_mod.Serial(**kwargs) as ser:
            # ID is useful diagnostic output but can be empty on some serial bridges;
            # VI099 is the actual acquisition command that must work.
            instrument_id = _request(ser, f"ID{serial_id}", timeout_seconds=timeout_seconds)
            vi099 = _request(ser, f"VI{serial_id}99", timeout_seconds=timeout_seconds)
    except Exception as exc:
        detail = f"\nProcesses currently using {device}:\n{holders}" if holders else ""
        pytest.fail(f"Aurora serial communication failed on {device!r}: {exc}{detail}")

    assert vi099, f"Aurora VI{serial_id}99 returned an empty response. ID response was: {instrument_id!r}"
    _assert_vi099_shape(vi099)
