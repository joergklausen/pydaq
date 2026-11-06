"""Read-only Eaton UPS monitoring via RFC 1628 SNMPv3.

Each successful poll is a regular pydaq CSV record. State transitions are also
persisted locally as individual JSON events and staged atomically to the
existing instrument outbox. No SNMP SET operations are performed.

Requires pysnmp >= 7.1. Battery voltage/frequencies are scaled per RFC 1628.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydaq.instruments.instrument import Instrument

UPS = "1.3.6.1.2.1.33.1"
# These exact objects were present in the MKN inventory. Table entries use line 1.
OIDS = {
    "battery_status_code": f"{UPS}.2.1.0",
    "seconds_on_battery": f"{UPS}.2.2.0",
    "battery_runtime_min": f"{UPS}.2.3.0",
    "battery_capacity_pct": f"{UPS}.2.4.0",
    "battery_voltage": f"{UPS}.2.5.0",
    "input_frequency": f"{UPS}.3.3.1.2.1",
    "input_voltage": f"{UPS}.3.3.1.3.1",
    "output_source_code": f"{UPS}.4.1.0",
    "output_frequency": f"{UPS}.4.2.0",
    "output_voltage": f"{UPS}.4.4.1.2.1",
    "output_load": f"{UPS}.4.4.1.5.1",
    "active_alarms": f"{UPS}.6.1.0",
}
OUTPUT_SOURCES = {1: "other", 2: "none", 3: "normal", 4: "bypass", 5: "battery", 6: "booster", 7: "reducer"}
BATTERY_STATUSES = {1: "unknown", 2: "normal", 3: "low", 4: "depleted"}
HEADERS = ["dtm", "output_source_code", "output_source", "on_battery",
           "battery_status_code", "battery_status", "battery_voltage",
           "battery_capacity_pct", "battery_runtime_min", "seconds_on_battery",
           "input_voltage", "input_frequency", "output_voltage", "output_frequency",
           "output_load", "active_alarms"]


def decode_values(raw: Mapping[str, str]) -> dict[str, Any]:
    """Convert RFC 1628 raw OID values to engineering units; unknowns stay missing."""
    result: dict[str, Any] = {}
    for key, oid in OIDS.items():
        value = raw.get(oid)
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        result[key] = number / 10 if key in {"battery_voltage", "input_frequency", "output_frequency"} else number
    if "output_source_code" in result:
        code = result["output_source_code"]
        result["output_source"] = OUTPUT_SOURCES.get(code, "unknown")
        result["on_battery"] = 1 if code == 5 else 0 if code in OUTPUT_SOURCES else None
    if "battery_status_code" in result:
        result["battery_status"] = BATTERY_STATUSES.get(result["battery_status_code"], "unknown")
    return result


def _read_secret(value: object) -> str:
    """Read secrets from explicit file paths; never log their contents."""
    if value is None or value == "":
        return ""
    path = Path(str(value)).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"SNMP secret file missing: {path}")
    return path.read_text(encoding="utf-8").strip()


async def poll_snmp(io: Mapping[str, Any]) -> dict[str, str]:
    """Read all configured OIDs in one GET, using SNMPv3 read-only access."""
    try:
        from pysnmp.hlapi.v3arch.asyncio import (
            ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget,
            UsmUserData, get_cmd, usmAesCfb128Protocol, usmHMACSHAAuthProtocol,
        )
    except ImportError as exc:
        raise RuntimeError('Install pysnmp>=7.1 to enable Eaton SNMP driver') from exc
    host = str(io.get("host", "")).strip()
    username = str(io.get("user", "")).strip()
    if not host or not username:
        raise ValueError("Eaton io.host and io.user are required")
    level = str(io.get("security_level", "authPriv"))
    if level == "noAuthNoPriv":
        credentials = UsmUserData(username)
    elif level in {"authNoPriv", "authPriv"}:
        auth = _read_secret(io.get("auth_password_file"))
        if not auth:
            raise ValueError("SNMPv3 authentication password file is required")
        kwargs: dict[str, Any] = {"authKey": auth, "authProtocol": usmHMACSHAAuthProtocol}
        if level == "authPriv":
            privacy = _read_secret(io.get("priv_password_file"))
            if not privacy:
                raise ValueError("SNMPv3 privacy password file is required")
            kwargs.update(privKey=privacy, privProtocol=usmAesCfb128Protocol)
        credentials = UsmUserData(username, **kwargs)
    else:
        raise ValueError(f"Unsupported SNMP security level: {level}")
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create(
            (host, int(io.get("port", 161))), timeout=float(io.get("timeout_seconds", 3)),
            retries=int(io.get("retries", 1)),
        )
        error, status, _index, binds = await get_cmd(
            engine, credentials, target, ContextData(),
            *(ObjectType(ObjectIdentity(oid)) for oid in OIDS.values()),
            lookupMib=False,
        )
        if error or status:
            raise RuntimeError(f"SNMP GET failed: {error or status}")
        return {oid.prettyPrint(): value.prettyPrint() for oid, value in binds}
    finally:
        engine.close_dispatcher()


def event_changes(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Generate meaningful transitions, never invent an event on first observation."""
    if previous is None:
        return []
    changes: list[tuple[str, str]] = []
    old, new = previous.get("output_source_code"), current.get("output_source_code")
    if old is not None and new is not None and old != new:
        if new == 5:
            changes.append(("ups_on_battery", "critical"))
        elif old == 5 and new == 3:
            changes.append(("ups_mains_restored", "info"))
        else:
            changes.append(("ups_output_source_changed", "warning"))
    old_b, new_b = previous.get("battery_status_code"), current.get("battery_status_code")
    if old_b is not None and new_b is not None and old_b != new_b:
        if new_b in {3, 4}:
            changes.append(("ups_battery_low" if new_b == 3 else "ups_battery_depleted", "critical"))
        elif old_b in {3, 4} and new_b == 2:
            changes.append(("ups_battery_normal", "info"))
    old_a, new_a = previous.get("active_alarms"), current.get("active_alarms")
    if old_a is not None and new_a is not None and old_a != new_a:
        changes.append(("ups_alarm_count_changed", "warning" if new_a > 0 else "info"))
    return changes


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class EATON(Instrument):
    """Driver scheduled normally by pydaq; only SNMP reads are performed."""
    HEADERS = HEADERS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("headers", self.HEADERS)
        super().__init__(*args, **kwargs)
        self.io = dict(self.parameters.get("io", {}))
        self._state_file = self.data_dir / "eaton_last_state.json"
        self._event_dir = self.data_dir / "events"
        self._previous: dict[str, Any] | None = None

    def initialize(self) -> None:
        if self._state_file.exists():
            try:
                value = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    self._previous = value
            except (OSError, ValueError) as exc:
                self.logger.warning("Could not read previous UPS state: %s", exc)

    def get_record(self) -> dict[str, Any]:
        raw = asyncio.run(poll_snmp(self.io))
        values = decode_values(raw)
        if "output_source_code" not in values or "battery_capacity_pct" not in values:
            raise RuntimeError("Incomplete UPS SNMP response: source or charge missing")
        now = datetime.now(timezone.utc)
        record = {"dtm": now.strftime("%Y-%m-%d %H:%M:%S"), **values}
        for event_type, severity in event_changes(self._previous, values):
            event_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"
            payload = {
                "event_id": event_id, "timestamp": now.isoformat().replace("+00:00", "Z"),
                "instrument": self.name, "event": event_type, "severity": severity,
                "source": "snmp_poll", "previous_output_source":
                None if self._previous is None else self._previous.get("output_source"),
                "output_source": values.get("output_source"),
                "battery_status": values.get("battery_status"),
                "battery_capacity_pct": values.get("battery_capacity_pct"),
                "battery_runtime_min": values.get("battery_runtime_min"),
                "active_alarms": values.get("active_alarms"),
            }
            filename = f"{self.name}-event-{event_id}.json"
            _atomic_json(self._event_dir / filename, payload)
            _atomic_json(self.outbox_dir / filename, payload)
            self.logger.warning("UPS event %s severity=%s", event_type, severity)
        _atomic_json(self._state_file, values)
        self._previous = values
        return record
