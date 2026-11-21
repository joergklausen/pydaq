"""Read-only Eaton UPS battery and EBM discovery using pydaq's station YAML.

Run from the pydaq repository root:
    python -m pydaq.utils.eaton_battery_diagnostics -c pydaq/configs/mkn.yml

Requires PyYAML and pysnmp >= 7.1. Never performs SNMP SET or battery tests.
Outputs a human-readable report and a machine-readable inventory of numeric OIDs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from pydaq.instruments.eaton import OIDS, decode_values, poll_snmp, _read_secret

UPS_BATTERY = "1.3.6.1.2.1.33.1.2"
UPS_TEST = "1.3.6.1.2.1.33.1.7"
UPS_ALARMS = "1.3.6.1.2.1.33.1.6"
EATON = "1.3.6.1.4.1.534"
# Specific root inventories remain bounded to keep queries low-impact.
TREES = (
    ("Battery (UPS-MIB)", UPS_BATTERY),
    ("Tests (UPS-MIB)", UPS_TEST),
    ("Alarms (UPS-MIB)", UPS_ALARMS),
    ("Eaton enterprise", EATON),
)


def read_io(config_path: Path, instrument: str = "eaton") -> dict[str, Any]:
    """Resolve the actual pydaq configuration; do not accept embedded secrets."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Station YAML must be a mapping")
    instruments = config.get("instruments")
    if not isinstance(instruments, dict) or instrument not in instruments:
        raise ValueError(f"Instrument {instrument!r} not found under instruments")
    entry = instruments[instrument]
    if not isinstance(entry, dict) or not isinstance(entry.get("io"), dict):
        raise ValueError(f"Missing instruments.{instrument}.io configuration")
    io = dict(entry["io"])
    if io.get("kind") != "snmp":
        raise ValueError(f"Expected io.kind=snmp, got {io.get('kind')!r}")
    if not io.get("host") or not io.get("user"):
        raise ValueError("SNMP host and user must be configured")
    return io


def make_user(io: Mapping[str, Any]):
    """Use exactly the SNMPv3 algorithms and secret-file conventions of eaton.py."""
    from pysnmp.hlapi.v3arch.asyncio import (
        UsmUserData, usmAesCfb128Protocol, usmHMACSHAAuthProtocol,
    )
    name = str(io["user"])
    level = str(io.get("security_level", "authPriv"))
    if level == "noAuthNoPriv":
        return UsmUserData(name)
    if level not in ("authNoPriv", "authPriv"):
        raise ValueError(f"Unsupported security_level: {level!r}")
    auth = _read_secret(io.get("auth_password_file"))
    if not auth:
        raise ValueError("SNMPv3 authentication password file required")
    kwargs: dict[str, Any] = {"authKey": auth, "authProtocol": usmHMACSHAAuthProtocol}
    if level == "authPriv":
        priv = _read_secret(io.get("priv_password_file"))
        if not priv:
            raise ValueError("SNMPv3 privacy password file required")
        kwargs.update(privKey=priv, privProtocol=usmAesCfb128Protocol)
    return UsmUserData(name, **kwargs)


def report_lines(scalars: Mapping[str, str], trees: Mapping[str, Mapping[str, str]],
                 problems: Mapping[str, str], *, timestamp: str) -> list[str]:
    """Create a conservative report: never interpret unknown Eaton OIDs as EBM count."""
    values = decode_values(scalars)
    lines = [f"Eaton battery diagnostic (UTC): {timestamp}",
             "Read-only SNMPv3 GET/WALK; no battery test or control commands", "",
             "=== UPS status (RFC 1628) ==="]
    labels = (
        ("output_source", "Output source"), ("on_battery", "On battery (1=yes)"),
        ("battery_status", "Battery status"), ("battery_capacity_pct", "Charge (%)"),
        ("battery_runtime_min", "Estimated runtime (minutes)"),
        ("battery_voltage", "Terminal voltage (V)"),
        ("seconds_on_battery", "Seconds on battery"),
        ("output_load", "Output load (%)"), ("active_alarms", "Active alarm count"),
    )
    for key, label in labels:
        lines.append(f"{label}: {values.get(key, 'not available')}")
    lines.extend(["", "=== Battery configuration and EBM interpretation ===",
                  "The standard UPS-MIB does not specify detected EBM count or battery-bank Ah.",
                  "Eaton-specific objects appear below as raw OID/value pairs.",
                  "A reported nominal capacity (e.g. 45 Ah) is configuration, not proof of usable capacity.",
                  "Do NOT infer EBM health or count from an unidentified numeric OID.",
                  "A passing short battery self-test is NOT a full-runtime capacity test."])
    for label, root in TREES:
        data = trees.get(label, {})
        lines.extend(["", f"=== {label}: {len(data)} objects ({root}) ==="])
        for oid, value in sorted(data.items(), key=lambda item: tuple(int(x) for x in item[0].split('.'))):
            lines.append(f"{oid} = {value}")
        if label in problems:
            lines.append("INCOMPLETE: " + problems[label])
    if problems:
        lines.extend(["", "WARNING: One or more walks were incomplete. Results do not imply absence of OIDs."])
    return lines


async def inventory(io: Mapping[str, Any], *, limit: int = 600) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, str]]:
    """One validated GET followed by bounded read-only walks; individual failures tolerated."""
    from pysnmp.hlapi.v3arch.asyncio import (
        ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget, walk_cmd,
    )
    # Use production driver GET: ensures status and unit scaling are identical to pydaq.
    scalars = await poll_snmp(io)
    if not scalars:
        raise RuntimeError("No UPS scalar values returned")
    user = make_user(io)
    engine = SnmpEngine()
    trees: dict[str, dict[str, str]] = {}
    problems: dict[str, str] = {}
    try:
        target = await UdpTransportTarget.create(
            (str(io["host"]), int(io.get("port", 161))),
            timeout=float(io.get("timeout_seconds", 3)),
            retries=int(io.get("retries", 1)),
        )
        for label, root in TREES:
            data: dict[str, str] = {}
            try:
                async for error, status, _index, binds in walk_cmd(
                    engine, user, target, ContextData(),
                    ObjectType(ObjectIdentity(root)),
                    lexicographicMode=False, lookupMib=False,
                    maxRows=limit, maxCalls=limit,
                ):
                    if error or status:
                        problems[label] = f"SNMP error: {error or status}"
                        break
                    for oid, value in binds:
                        ident = oid.prettyPrint()
                        if ident == root or ident.startswith(root + "."):
                            data[ident] = value.prettyPrint()
                            if len(data) >= limit:
                                problems[label] = f"Walk truncated at {limit} objects"
                                break
                    if len(data) >= limit:
                        break
            except Exception as exc:
                problems[label] = f"{type(exc).__name__}: {exc}"
            trees[label] = data
    finally:
        engine.close_dispatcher()
    return scalars, trees, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", type=Path, default=Path("pydaq/configs/mkn.yml"))
    parser.add_argument("--instrument", default="eaton")
    parser.add_argument("--output", type=Path, default=Path("C:/Users/mkn/Documents/pydaq/mkn/outbox/eaton/eaton_battery_diagnostic.txt"))
    parser.add_argument("--max-objects", type=int, default=600,
                        help="Maximum OIDs per subtree; default 600")
    args = parser.parse_args(argv)
    if not 1 <= args.max_objects <= 3000:
        parser.error("--max-objects must be between 1 and 3000")
    try:
        io = read_io(args.config, args.instrument)
        stamp = datetime.now(timezone.utc).isoformat()
        scalars, trees, problems = asyncio.run(inventory(io, limit=args.max_objects))
        lines = report_lines(scalars, trees, problems, timestamp=stamp)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        json_path = args.output.with_suffix(".json")
        json_path.write_text(json.dumps({"utc": stamp, "scalars": scalars,
                                          "trees": trees, "problems": problems},
                                         indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("\n".join(lines[:21]))
        print(f"\nFull report: {args.output.resolve()}\nRaw inventory: {json_path.resolve()}")
        return 0
    except Exception as exc:
        parser.exit(1, f"Eaton diagnostic failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
