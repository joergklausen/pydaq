"""Focused read-only Eaton 9PX low-battery and shutdown diagnostics.

Run in pydaq repository root:
 python -m pydaq.utils.eaton_shutdown_diagnostics -c pydaq/configs/mkn.yml

Uses the existing Eaton station SNMPv3 configuration and secret-file convention.
ONLY issues GET (never SET). It does not start tests, shut down the UPS,
or alter timers. Missing / unsupported objects are reported explicitly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from pydaq.instruments.eaton import _read_secret

UPS = '1.3.6.1.2.1.33.1'
EATON = '1.3.6.1.4.1.534.1'

# RFC 1628: explicitly request configuration subtree absent from earlier WALK.
# UPS control objects are queried read-only, regardless of their MIB write access.
OBJECTS: dict[str, tuple[str, str]] = {
    'battery_status': (f'{UPS}.2.1.0', 'enum: 1 unknown, 2 normal, 3 low, 4 depleted'),
    'seconds_on_battery': (f'{UPS}.2.2.0', 'seconds'),
    'battery_runtime_min': (f'{UPS}.2.3.0', 'minutes'),
    'battery_capacity_pct': (f'{UPS}.2.4.0', '%'),
    'battery_voltage_deci_v': (f'{UPS}.2.5.0', '0.1 V'),
    'output_source': (f'{UPS}.4.1.0', 'enum: 2 none, 3 normal, 4 bypass, 5 battery'),
    'active_alarm_count': (f'{UPS}.6.1.0', 'count'),
    'low_battery_warning_min': (f'{UPS}.9.7.0', 'minutes; configured threshold'),
    'audible_alarm_setting': (f'{UPS}.9.8.0', 'enum per RFC 1628'),
    'shutdown_type': (f'{UPS}.8.1.0', 'enum; READ ONLY'),
    'shutdown_after_delay': (f'{UPS}.8.2.0', 'seconds; READ ONLY'),
    'startup_after_delay': (f'{UPS}.8.3.0', 'seconds; READ ONLY'),
    'reboot_with_duration': (f'{UPS}.8.4.0', 'seconds; READ ONLY'),
    'auto_restart': (f'{UPS}.8.5.0', 'enum; READ ONLY'),
    'test_results_summary': (f'{UPS}.7.3.0', 'test results status'),
    'eaton_battery_runtime_s': (f'{EATON}.2.1.0', 'seconds'),
    'eaton_battery_capacity_pct': (f'{EATON}.2.4.0', '%'),
    'eaton_battery_abm_state': (f'{EATON}.2.5.0', 'manufacturer enum'),
    'eaton_battery_last_replaced': (f'{EATON}.2.6.0', 'text as reported'),
    'eaton_battery_failure': (f'{EATON}.2.7.0', 'manufacturer enum'),
    'eaton_battery_not_present': (f'{EATON}.2.8.0', 'manufacturer enum'),
    'eaton_battery_aged': (f'{EATON}.2.9.0', 'manufacturer enum'),
    'eaton_output_load_pct': (f'{EATON}.4.1.0', '%'),
}

# Split queries so one unsupported value or a large response cannot hide all others.
GROUPS: tuple[tuple[str, ...], ...] = (
    ('battery_status','seconds_on_battery','battery_runtime_min','battery_capacity_pct','battery_voltage_deci_v'),
    ('output_source','active_alarm_count','low_battery_warning_min','audible_alarm_setting'),
    ('shutdown_type','shutdown_after_delay','startup_after_delay','reboot_with_duration','auto_restart'),
    ('test_results_summary','eaton_battery_runtime_s','eaton_battery_capacity_pct','eaton_battery_abm_state'),
    ('eaton_battery_last_replaced','eaton_battery_failure','eaton_battery_not_present','eaton_battery_aged','eaton_output_load_pct'),
)


def read_io(path: Path, instrument: str = 'eaton') -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('Station config must be a mapping')
    item = config.get('instruments', {}).get(instrument)
    if not isinstance(item, dict) or not isinstance(item.get('io'), dict):
        raise ValueError(f'Missing instruments.{instrument}.io')
    io = dict(item['io'])
    if io.get('kind') != 'snmp' or not io.get('host') or not io.get('user'):
        raise ValueError('Expected configured Eaton SNMP host, user and kind=snmp')
    return io


def make_user(io: Mapping[str, Any]):
    from pysnmp.hlapi.v3arch.asyncio import (
        UsmUserData, usmAesCfb128Protocol, usmHMACSHAAuthProtocol,
    )
    level = str(io.get('security_level', 'authPriv'))
    name = str(io['user'])
    if level == 'noAuthNoPriv':
        return UsmUserData(name)
    if level not in ('authNoPriv','authPriv'):
        raise ValueError(f'Unsupported security level: {level}')
    auth = _read_secret(io.get('auth_password_file'))
    if not auth:
        raise ValueError('Missing authentication password file')
    opts: dict[str, Any] = {'authKey': auth, 'authProtocol': usmHMACSHAAuthProtocol}
    if level == 'authPriv':
        priv = _read_secret(io.get('priv_password_file'))
        if not priv:
            raise ValueError('Missing privacy password file')
        opts.update(privKey=priv, privProtocol=usmAesCfb128Protocol)
    return UsmUserData(name, **opts)


def decode_response(group: tuple[str, ...], response: Mapping[str, str]) -> dict[str, str | None]:
    """Match by numeric OID; never mistake unsupported SNMP values for measurements."""
    result = {}
    for name in group:
        oid = OBJECTS[name][0]
        raw = response.get(oid)
        if raw is None or any(tok in raw.lower() for tok in ('nosuchobject','nosuchinstance','no such object','no such instance','endofmibview')):
            result[name] = None
        else:
            result[name] = raw
    return result


async def query(io: Mapping[str, Any]) -> tuple[dict[str, str | None], dict[str, str]]:
    from pysnmp.hlapi.v3arch.asyncio import (
        ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget, get_cmd,
    )
    user = make_user(io)
    engine = SnmpEngine()
    results: dict[str, str | None] = {}
    problems: dict[str, str] = {}
    try:
        target = await UdpTransportTarget.create(
            (str(io['host']), int(io.get('port',161))),
            timeout=float(io.get('timeout_seconds',3)), retries=int(io.get('retries',1)),
        )
        for idx, group in enumerate(GROUPS, start=1):
            try:
                err, status, _index, binds = await get_cmd(
                    engine, user, target, ContextData(),
                    *(ObjectType(ObjectIdentity(OBJECTS[k][0])) for k in group),
                    lookupMib=False,
                )
                if err or status:
                    problems[f'group_{idx}'] = str(err or status)
                    results.update({key: None for key in group})
                    continue
                values = {oid.prettyPrint(): val.prettyPrint() for oid,val in binds}
                results.update(decode_response(group,values))
            except Exception as exc:
                problems[f'group_{idx}'] = f'{type(exc).__name__}: {exc}'
                results.update({key: None for key in group})
    finally:
        engine.close_dispatcher()
    return results, problems


def format_report(values: Mapping[str, str | None], problems: Mapping[str,str], stamp: str) -> str:
    lines = [f'Eaton targeted shutdown diagnostic | UTC {stamp}',
             'Read-only SNMP GET only; no SET, tests or changes',
             'NOTE: reported runtime is an estimate, not measured battery capacity.', '']
    for name,(oid,unit) in OBJECTS.items():
        value = values.get(name)
        lines.append(f'{name}: {value if value is not None else "NOT SUPPORTED / NO RESPONSE"} [{unit}]  ({oid})')
    if problems:
        lines.extend(['', 'Query problems:'])
        lines.extend(f'{name}: {message}' for name,message in problems.items())
    lines.extend(['', 'Interpretation warning: even PASS of a brief self-test does not verify EBM runtime.',
                  'No EBM count is inferred from unidentified numeric objects.'])
    return '\n'.join(lines)+'\n'


def main(argv: list[str] | None = None) -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('-c','--config',type=Path,default=Path('pydaq/configs/mkn.yml'))
    ap.add_argument('--instrument',default='eaton')
    ap.add_argument('--output',type=Path,default=Path('C:/Users/mkn/Documents/pydaq/mkn/outbox/eaton/eaton_shutdown_diagnostic.txt'))
    args=ap.parse_args(argv)
    io=read_io(args.config,args.instrument)
    stamp=datetime.now(timezone.utc).isoformat()
    values,problems=asyncio.run(query(io))
    report=format_report(values,problems,stamp)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(report,encoding='utf-8')
    args.output.with_suffix('.json').write_text(json.dumps({
        'utc':stamp,'host':io['host'],
        'results':{key:{'oid':OBJECTS[key][0],'raw':raw} for key,raw in values.items()},
        'problems':problems},indent=2)+'\n',encoding='utf-8')
    print(report)
    return 0 if not problems else 2


if __name__ == '__main__':
    raise SystemExit(main())
