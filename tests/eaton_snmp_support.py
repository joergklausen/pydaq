"""Read-only Eaton UPS SNMP diagnostic support (no pydaq runtime dependency).

This module is also a starting point for a production driver, but is deliberately
kept in tests until the station configuration schema is finalized.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from getpass import getpass

UPS_ROOT = '1.3.6.1.2.1.33'
OIDS = {
    'battery_status': f'{UPS_ROOT}.1.2.1.0',
    'battery_runtime_min': f'{UPS_ROOT}.1.2.3.0',
    'battery_capacity_pct': f'{UPS_ROOT}.1.2.4.0',
    'battery_voltage': f'{UPS_ROOT}.1.2.5.0',
    'output_source': f'{UPS_ROOT}.1.4.1.0',
    'active_alarms': f'{UPS_ROOT}.1.6.1.0',
}
OUTPUT_SOURCES = {
    1: 'other', 2: 'none', 3: 'normal', 4: 'bypass',
    5: 'battery', 6: 'booster', 7: 'reducer',
}
BATTERY_STATUSES = {1: 'unknown', 2: 'normal', 3: 'low', 4: 'depleted'}
TREES = (
    ('System identification', '1.3.6.1.2.1.1', 30),
    ('Standard UPS MIB (RFC 1628)', UPS_ROOT, 350),
    ('Eaton enterprise MIB', '1.3.6.1.4.1.534', 350),
)


def decode_output_source(raw: str | int) -> str:
    """RFC 1628 upsOutputSource; preserve unexpected values as unknown."""
    try:
        code = int(raw)
    except (ValueError, TypeError):
        return 'unknown'
    return OUTPUT_SOURCES.get(code, 'unknown')


def parse_ups_values(oid_values: dict[str, str]) -> dict[str, object]:
    """Interpret raw SNMP scalar values, including standard MIB scaling."""
    values: dict[str, object] = {}
    for key, oid in OIDS.items():
        raw = oid_values.get(oid)
        if raw is None:
            continue
        try:
            code = int(raw)
        except ValueError:
            continue
        if key == 'output_source':
            values['output_source_code'] = code
            values['output_source'] = decode_output_source(code)
            values['on_battery'] = code == 5 if code in OUTPUT_SOURCES else None
        elif key == 'battery_status':
            values['battery_status_code'] = code
            values['battery_status'] = BATTERY_STATUSES.get(code, 'unknown')
        elif key == 'battery_voltage':
            values[key] = code / 10  # RFC 1628: tenths of volts
        else:
            values[key] = code
    return values


def make_credentials(args: argparse.Namespace, *, interactive: bool):
    from pysnmp.hlapi.v3arch.asyncio import (
        UsmUserData, usmAesCfb128Protocol, usmHMACSHAAuthProtocol,
    )
    if args.level == 'noAuthNoPriv':
        return UsmUserData(args.user)
    auth = args.auth_password
    if not auth and interactive:
        auth = getpass('SNMPv3 authentication password: ')
    if not auth:
        raise ValueError('SNMP authentication password is required')
    if args.level == 'authNoPriv':
        return UsmUserData(args.user, authKey=auth, authProtocol=usmHMACSHAAuthProtocol)
    priv = args.priv_password
    if not priv and interactive:
        priv = getpass('SNMPv3 privacy password: ')
    if not priv:
        raise ValueError('SNMP privacy password is required')
    return UsmUserData(args.user, authKey=auth, privKey=priv,
                       authProtocol=usmHMACSHAAuthProtocol,
                       privProtocol=usmAesCfb128Protocol)


async def query_scalars(args: argparse.Namespace, *, interactive: bool = False) -> dict[str, str]:
    """GET essential UPS scalars without writing to the device."""
    from pysnmp.hlapi.v3arch.asyncio import (
        ContextData, ObjectIdentity, ObjectType, SnmpEngine,
        UdpTransportTarget, get_cmd,
    )
    credentials = make_credentials(args, interactive=interactive)
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create(
            (args.host, args.port), timeout=args.timeout, retries=args.retries,
        )
        error, status, _index, binds = await get_cmd(
            engine, credentials, target, ContextData(),
            *(ObjectType(ObjectIdentity(oid)) for oid in OIDS.values()),
            lookupMib=False,
        )
        if error or status:
            raise RuntimeError(f'SNMP GET failed: {error or status}')
        result = {oid.prettyPrint(): value.prettyPrint() for oid, value in binds}
        missing = set(OIDS.values()) - result.keys()
        if missing:
            raise RuntimeError(f'Missing UPS-MIB objects: {sorted(missing)}')
        return result
    finally:
        engine.close_dispatcher()


async def collect_inventory(args: argparse.Namespace, *, interactive: bool = False) -> list[str]:
    """Full inventory including status summary; enterprise-tree timeout is nonfatal."""
    from pysnmp.hlapi.v3arch.asyncio import (
        ContextData, ObjectIdentity, ObjectType, SnmpEngine,
        UdpTransportTarget, walk_cmd,
    )
    raw = await query_scalars(args, interactive=interactive)
    decoded = parse_ups_values(raw)
    lines = [
        f'Eaton SNMP inventory | {datetime.now(timezone.utc).isoformat()}',
        f'Host: {args.host} | Security: {args.level}',
        'SNMP GET succeeded',
        '', '--- UPS status ---',
    ]
    for key, value in decoded.items():
        lines.append(f'{key} = {value}')
    credentials = make_credentials(args, interactive=interactive)
    engine = SnmpEngine()
    try:
        target = await UdpTransportTarget.create(
            (args.host, args.port), timeout=args.timeout, retries=args.retries,
        )
        for name, root, limit in TREES:
            lines.append(f'\n--- {name} ({root}) ---')
            count = 0
            async for error, status, _index, binds in walk_cmd(
                engine, credentials, target, ContextData(),
                ObjectType(ObjectIdentity(root)),
                lexicographicMode=False, lookupMib=False,
                maxRows=limit, maxCalls=limit,
            ):
                if error or status:
                    lines.append(f'WARNING: {error or status}')
                    break
                for oid, value in binds:
                    oid_text = oid.prettyPrint()
                    if oid_text == root or oid_text.startswith(root + '.'):
                        lines.append(f'{oid_text} = {value.prettyPrint()}')
                        count += 1
            lines.append(f'Objects retrieved: {count}' +
                         (' (limit reached)' if count >= limit else ''))
    finally:
        engine.close_dispatcher()
    return lines
