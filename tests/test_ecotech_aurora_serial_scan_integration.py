"""Hardware integration tests for locating and validating Aurora 3000 serial IO.

These tests are intended for Raspberry Pi / field-system checks, not for normal
unit-test runs.  They load the station YAML passed via ``--station-config`` and
combine configuration checks with a serial-port inventory scan.

The test module validates four things:

1. Station configuration:
   - exactly one Aurora 3000 instrument can be identified;
   - it is configured for ``io.kind: serial``;
   - all enabled serial instruments are checked for duplicate physical-port use;
   - intentional Vaisala/HMP shared serial buses are allowed.

2. Serial inventory:
   - all serial-like devices known to pyserial and common Linux device globs are
     listed;
   - configured serial devices are matched against the inventory by both their
     configured path and resolved physical path;
   - this helps catch ``/dev/ttyUSB*`` renumbering and wrong symlink targets.

3. Port ownership:
   - the configured Aurora serial device is opened with pyserial exclusive mode
     where supported;
   - if opening fails, the test reports ``lsof`` / ``fuser`` output when those
     tools are available.

4. Aurora discovery and communication:
   - candidate ports are probed with ``ID{id}`` and ``VI{id}99``;
   - ``VI{id}99`` must return the expected Aurora shape: timestamp plus twelve
     values;
   - by default the scan probes the configured Aurora port and any unassigned
     serial ports, but skips ports assigned to enabled non-Aurora instruments to
     avoid disturbing other sensors.

Run from the repository root, for example:

    pytest -vv -rs -s tests/test_ecotech_aurora_serial_integration.py \
        --station-config ./pydaq/configs/nrb.yml

Useful environment variables:

- ``PYDAQ_AURORA_SCAN_PORTS=1``
    Run the active scan even when the Aurora instrument is disabled in the YAML.
- ``PYDAQ_AURORA_SCAN_INCLUDE_CONFIGURED=1``
    Also probe ports assigned to other enabled instruments.  Use with care.
- ``PYDAQ_AURORA_SCAN_TIMEOUT=5``
    Maximum per-port timeout, in seconds, during the active discovery scan.

These tests require ``pyserial`` and are marked ``integration``.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest

pytestmark = pytest.mark.integration

AURORA_DRIVER_NAMES = {"aurora3000", "aurora", "ecotech", "neph"}
ALLOWED_SHARED_SERIAL_DRIVERS = {"hmpascii", "vaisala", "hmp110", "hmp60"}
SERIAL_GLOBS = (
    "/dev/serial/by-id/*",
    "/dev/serial/by-path/*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
    "/dev/ttyAMA*",
    "/dev/ttyS*",
)


@dataclass(frozen=True)
class SerialPort:
    """One discovered serial device."""

    device: str
    canonical: str
    description: str = ""
    hwid: str = ""


@dataclass(frozen=True)
class ConfiguredSerialInstrument:
    """One serial instrument found in the station configuration."""

    name: str
    driver: str
    enabled: bool
    device: str
    canonical: str


@dataclass(frozen=True)
class ProbeResult:
    """Result of probing one serial port with Aurora commands."""

    port: SerialPort
    ok: bool
    id_response: str
    vi099_response: str
    error: str = ""
    skipped_reason: str = ""


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
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


def _canonical_device(device: str) -> str:
    if not device:
        return ""
    try:
        return str(Path(device).expanduser().resolve(strict=False))
    except OSError:
        return str(Path(device).expanduser())


def _is_aurora(name: str, instrument: Mapping[str, Any]) -> bool:
    driver = _driver_name(instrument)
    protocol = str(instrument.get("protocol", "")).strip().lower()
    return name.lower() == "aurora3000" or driver in AURORA_DRIVER_NAMES or protocol == "aurora"


def _aurora_instrument(config: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    matches: list[tuple[str, dict[str, Any]]] = []
    for name, instrument in _instruments(config).items():
        if _is_aurora(name, instrument):
            matches.append((name, instrument))

    if not matches:
        raise pytest.skip("No Aurora 3000 instrument found in station config.")
    if len(matches) > 1:
        names = ", ".join(name for name, _ in matches)
        raise AssertionError(f"Expected exactly one Aurora 3000 instrument, found: {names}")
    return matches[0]


def _configured_serial_instruments(config: Mapping[str, Any]) -> list[ConfiguredSerialInstrument]:
    configured: list[ConfiguredSerialInstrument] = []
    for name, instrument in _instruments(config).items():
        if _io_kind(instrument) != "serial":
            continue
        device = _serial_device(instrument)
        configured.append(
            ConfiguredSerialInstrument(
                name=name,
                driver=_driver_name(instrument),
                enabled=_boolish(instrument.get("enabled"), default=True),
                device=device,
                canonical=_canonical_device(device),
            )
        )
    return configured


def _configured_by_canonical(config: Mapping[str, Any]) -> dict[str, list[ConfiguredSerialInstrument]]:
    grouped: dict[str, list[ConfiguredSerialInstrument]] = {}
    for item in _configured_serial_instruments(config):
        grouped.setdefault(item.canonical, []).append(item)
    return grouped


# ---------------------------------------------------------------------------
# Serial discovery / probing helpers
# ---------------------------------------------------------------------------
def _scan_serial_ports() -> list[SerialPort]:
    """Return serial devices discovered through pyserial plus Linux globs."""
    ports: dict[str, SerialPort] = {}

    try:
        list_ports = importlib.import_module("serial.tools.list_ports")
        for info in list_ports.comports():
            device = str(getattr(info, "device", "") or "").strip()
            if not device:
                continue
            ports[_canonical_device(device)] = SerialPort(
                device=device,
                canonical=_canonical_device(device),
                description=str(getattr(info, "description", "") or ""),
                hwid=str(getattr(info, "hwid", "") or ""),
            )
    except Exception:
        pass

    for pattern in SERIAL_GLOBS:
        for raw in sorted(Path("/").glob(pattern.lstrip("/"))):
            device = str(raw)
            canonical = _canonical_device(device)
            ports.setdefault(canonical, SerialPort(device=device, canonical=canonical))

    return sorted(ports.values(), key=lambda item: item.device)


def _serial_inventory_text(ports: list[SerialPort], configured: list[ConfiguredSerialInstrument]) -> str:
    lines: list[str] = ["Discovered serial ports:"]
    if not ports:
        lines.append("  <none>")
    for port in ports:
        extra = []
        if port.description:
            extra.append(f"desc={port.description}")
        if port.hwid:
            extra.append(f"hwid={port.hwid}")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"  - {port.device} -> {port.canonical}{suffix}")

    lines.append("Configured serial instruments:")
    if not configured:
        lines.append("  <none>")
    for item in configured:
        lines.append(
            f"  - {item.name}: driver={item.driver or '<none>'} enabled={item.enabled} "
            f"device={item.device or '<missing>'} -> {item.canonical or '<missing>'}"
        )
    return "\n".join(lines)


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


def _serial_open_kwargs(instrument: Mapping[str, Any], *, device_override: str | None = None) -> dict[str, Any]:
    io = _io_config(instrument)
    device = device_override or _serial_device(instrument)
    if not device:
        raise AssertionError("Aurora 3000 serial config requires io.device or io.port.")

    timeout_seconds = float(io.get("timeout_seconds", io.get("timeout", 5)))
    scan_timeout = os.environ.get("PYDAQ_AURORA_SCAN_TIMEOUT")
    if scan_timeout is not None:
        timeout_seconds = min(timeout_seconds, float(scan_timeout))

    kwargs: dict[str, Any] = {
        "port": device,
        "baudrate": int(io.get("baudrate", 19200)),
        "bytesize": int(io.get("bytesize", 8)),
        "parity": str(io.get("parity", "N")).upper(),
        "stopbits": float(io.get("stopbits", 1)),
        "timeout": timeout_seconds,
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


def _vi099_shape_error(response: str) -> str:
    line = _last_non_empty_line(response).replace(", ", ",")
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 13:
        return f"expected timestamp + 12 values, got {len(parts)} fields; line={line!r}"

    timestamp = parts[0].replace("T", " ")
    try:
        datetime_format = "%Y-%m-%d %H:%M:%S" if "-" in timestamp else "%d/%m/%Y %H:%M:%S"
        time.strptime(timestamp[:19], datetime_format)
    except ValueError as exc:
        return f"could not parse timestamp {parts[0]!r}: {exc}"

    for value in parts[1:12]:
        try:
            float(value)
        except ValueError as exc:
            return f"could not parse numeric value {value!r}: {exc}"

    dio = parts[12]
    try:
        int(dio, 16)
    except ValueError:
        try:
            int(float(dio))
        except ValueError as exc:
            return f"could not parse DIO state {dio!r}: {exc}"
    return ""


def _assert_vi099_shape(response: str) -> None:
    error = _vi099_shape_error(response)
    assert not error, f"Aurora VI099 response has unexpected shape: {error}\nFull response: {response!r}"


def _candidate_ports_for_aurora_scan(
    *,
    config: Mapping[str, Any],
    aurora: Mapping[str, Any],
    ports: list[SerialPort],
) -> tuple[list[SerialPort], list[ProbeResult]]:
    configured_aurora_device = _serial_device(aurora)
    configured_aurora_canonical = _canonical_device(configured_aurora_device)
    include_configured_non_aurora = _boolish(os.environ.get("PYDAQ_AURORA_SCAN_INCLUDE_CONFIGURED"), default=False)
    configured_by_canonical = _configured_by_canonical(config)

    candidates_by_canonical: dict[str, SerialPort] = {port.canonical: port for port in ports}
    if configured_aurora_device and configured_aurora_canonical not in candidates_by_canonical:
        candidates_by_canonical[configured_aurora_canonical] = SerialPort(
            device=configured_aurora_device,
            canonical=configured_aurora_canonical,
            description="configured Aurora port not found by inventory",
        )

    candidates: list[SerialPort] = []
    skipped: list[ProbeResult] = []
    for port in sorted(candidates_by_canonical.values(), key=lambda item: item.device):
        configured_here = configured_by_canonical.get(port.canonical, [])
        enabled_non_aurora = [
            item
            for item in configured_here
            if item.enabled and item.canonical != configured_aurora_canonical
        ]
        if enabled_non_aurora and not include_configured_non_aurora:
            names = ", ".join(item.name for item in enabled_non_aurora)
            skipped.append(
                ProbeResult(
                    port=port,
                    ok=False,
                    id_response="",
                    vi099_response="",
                    skipped_reason=f"assigned to enabled non-Aurora instrument(s): {names}",
                )
            )
            continue
        candidates.append(port)
    return candidates, skipped


def _probe_port_for_aurora(serial_mod: Any, aurora: Mapping[str, Any], port: SerialPort) -> ProbeResult:
    kwargs = _serial_open_kwargs(aurora, device_override=port.device)
    timeout_seconds = float(kwargs.get("timeout", 5.0))
    serial_id_raw = aurora.get("id", aurora.get("serial_id", 0))
    serial_id = 0 if serial_id_raw in (None, "") else int(serial_id_raw)

    try:
        with serial_mod.Serial(**kwargs) as ser:
            instrument_id = _request(ser, f"ID{serial_id}", timeout_seconds=timeout_seconds)
            vi099 = _request(ser, f"VI{serial_id}99", timeout_seconds=timeout_seconds)
    except Exception as exc:
        holders = _processes_using_path(port.device)
        detail = f"; holders: {holders}" if holders else ""
        return ProbeResult(port=port, ok=False, id_response="", vi099_response="", error=f"{exc}{detail}")

    shape_error = _vi099_shape_error(vi099)
    return ProbeResult(
        port=port,
        ok=not shape_error,
        id_response=instrument_id,
        vi099_response=vi099,
        error=shape_error,
    )


def _probe_results_text(results: list[ProbeResult]) -> str:
    lines: list[str] = []
    for result in results:
        status = "OK" if result.ok else "SKIPPED" if result.skipped_reason else "FAILED"
        lines.append(f"[{status}] {result.port.device} -> {result.port.canonical}")
        if result.port.description:
            lines.append(f"  description: {result.port.description}")
        if result.port.hwid:
            lines.append(f"  hwid: {result.port.hwid}")
        if result.skipped_reason:
            lines.append(f"  skipped: {result.skipped_reason}")
        if result.error:
            lines.append(f"  error: {result.error}")
        if result.id_response:
            lines.append(f"  ID response: {result.id_response!r}")
        if result.vi099_response:
            lines.append(f"  VI099 response: {result.vi099_response!r}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_serial_port_inventory_matches_station_config(pytestconfig: pytest.Config) -> None:
    config = _load_yaml(_station_config_path(pytestconfig))
    ports = _scan_serial_ports()
    configured = _configured_serial_instruments(config)
    inventory = _serial_inventory_text(ports, configured)
    print("\n" + inventory)

    port_canonicals = {port.canonical for port in ports}
    missing_enabled = [
        item
        for item in configured
        if item.enabled and item.device and item.canonical not in port_canonicals and not Path(item.device).exists()
    ]
    assert not missing_enabled, (
        "Enabled serial instruments refer to devices that were not found.\n"
        + "\n".join(f"- {item.name}: {item.device} -> {item.canonical}" for item in missing_enabled)
        + "\n\n"
        + inventory
    )


def test_configured_serial_ports_are_unique_or_intentionally_shared(pytestconfig: pytest.Config) -> None:
    config = _load_yaml(_station_config_path(pytestconfig))
    configured = _configured_serial_instruments(config)

    problems: list[str] = []
    grouped: dict[str, list[ConfiguredSerialInstrument]] = {}
    for item in configured:
        if not item.enabled or not item.canonical:
            continue
        grouped.setdefault(item.canonical, []).append(item)

    for canonical, users in grouped.items():
        if len(users) <= 1:
            continue
        drivers = {user.driver for user in users}
        names = ", ".join(f"{user.name}({user.driver or '<none>'})" for user in users)
        if drivers and drivers.issubset(ALLOWED_SHARED_SERIAL_DRIVERS):
            continue
        problems.append(f"{canonical}: {names}")

    assert not problems, (
        "One or more enabled serial ports are configured for multiple non-shared instruments.\n"
        + "\n".join(f"- {problem}" for problem in problems)
    )


def test_aurora3000_serial_port_can_be_opened_exclusively(pytestconfig: pytest.Config) -> None:
    serial_mod = pytest.importorskip("serial")
    config = _load_yaml(_station_config_path(pytestconfig))
    name, aurora = _aurora_instrument(config)

    if not _boolish(aurora.get("enabled"), default=False):
        pytest.skip(f"{name} is disabled in the station config; enable it before running the hardware test.")

    assert _io_kind(aurora) == "serial", f"{name} must use io.kind: serial for this integration test."
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


def test_aurora3000_scan_serial_ports_for_vi099(pytestconfig: pytest.Config) -> None:
    serial_mod = pytest.importorskip("serial")
    config = _load_yaml(_station_config_path(pytestconfig))
    name, aurora = _aurora_instrument(config)

    if _io_kind(aurora) != "serial":
        pytest.skip(f"{name} is not configured as a serial instrument.")

    force_scan = _boolish(os.environ.get("PYDAQ_AURORA_SCAN_PORTS"), default=False)
    if not _boolish(aurora.get("enabled"), default=False) and not force_scan:
        pytest.skip(
            f"{name} is disabled in the station config. Enable it or set PYDAQ_AURORA_SCAN_PORTS=1 "
            "to actively scan serial ports."
        )

    ports = _scan_serial_ports()
    candidates, skipped = _candidate_ports_for_aurora_scan(config=config, aurora=aurora, ports=ports)
    assert candidates, "No serial candidate ports found for Aurora scan.\n" + _serial_inventory_text(
        ports, _configured_serial_instruments(config)
    )

    results: list[ProbeResult] = []
    for port in candidates:
        results.append(_probe_port_for_aurora(serial_mod, aurora, port))
    results.extend(skipped)

    configured_device = _serial_device(aurora)
    configured_canonical = _canonical_device(configured_device)
    successes = [result for result in results if result.ok]
    configured_success = [result for result in successes if result.port.canonical == configured_canonical]

    if configured_success:
        _assert_vi099_shape(configured_success[0].vi099_response)
        return

    if successes:
        found = ", ".join(f"{result.port.device} -> {result.port.canonical}" for result in successes)
        pytest.fail(
            f"Aurora responded on a serial port, but not on the configured port {configured_device!r}.\n"
            f"Detected Aurora candidate(s): {found}\n\n"
            + _probe_results_text(results)
        )

    pytest.fail(
        f"No scanned serial port returned a parseable Aurora VI099 response for configured port {configured_device!r}.\n\n"
        + _probe_results_text(results)
    )
