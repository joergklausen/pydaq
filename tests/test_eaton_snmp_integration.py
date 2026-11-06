"""Opt-in live Eaton SNMP integration test and standalone diagnostic.

This test does not run on ordinary pytest invocations. Select a station
configuration explicitly using environment variables; no station is hardcoded.

PowerShell example, currently targeting MKN:

    $env:PYDAQ_EATON_TEST_STATION = 'mkn'
    $env:PYDAQ_EATON_SNMP_HOST = '192.168.1.141'
    $env:PYDAQ_EATON_SNMP_USER = 'YOUR_USER'
    $env:PYDAQ_EATON_SNMP_LEVEL = 'noAuthNoPriv'
    python -m pytest tests/test_eaton_snmp_integration.py -v

Optional full inventory (interactive passwords for authenticated SNMPv3):

    python tests/test_eaton_snmp_integration.py --station mkn --host 192.168.1.141 \
        --user YOUR_USER --level noAuthNoPriv

The station identifier is deliberately not tied to a fixed station whitelist;
when the UPS is added to a station YAML file, its SNMP parameters can be loaded
from that config by the production driver. Until then, the opt-in test uses
explicit connection environment variables.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import pytest

from eaton_snmp_support import collect_inventory, parse_ups_values, query_scalars


def test_live_ups_snmp() -> None:
    """Run only when an explicit station + host + user are provided."""
    station = os.getenv('PYDAQ_EATON_TEST_STATION')
    host = os.getenv('PYDAQ_EATON_SNMP_HOST')
    user = os.getenv('PYDAQ_EATON_SNMP_USER')
    if not all((station, host, user)):
        pytest.skip('Live UPS test requires PYDAQ_EATON_TEST_STATION, HOST and USER')
    pytest.importorskip('pysnmp')
    args = argparse.Namespace(
        host=host, user=user,
        level=os.getenv('PYDAQ_EATON_SNMP_LEVEL', 'authPriv'),
        port=int(os.getenv('PYDAQ_EATON_SNMP_PORT', '161')),
        auth_password=os.getenv('PYDAQ_EATON_SNMP_AUTH_PASSWORD'),
        priv_password=os.getenv('PYDAQ_EATON_SNMP_PRIV_PASSWORD'),
        timeout=3.0, retries=1,
    )
    values = parse_ups_values(asyncio.run(query_scalars(args)))
    assert 0 <= values['battery_capacity_pct'] <= 100
    assert values['battery_voltage'] > 0
    assert values['output_source'] != 'unknown', values
    assert isinstance(values['on_battery'], bool), values
    print(f"\nUPS [{station}]: source={values['output_source']}, "
          f"on_battery={values['on_battery']}, "
          f"charge={values['battery_capacity_pct']}%, "
          f"voltage={values['battery_voltage']} V, "
          f"runtime={values['battery_runtime_min']} min")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--station', required=True, help='Station identifier (e.g. mkn)')
    parser.add_argument('--host', required=True, help='UPS IPv4, IPv6 or hostname')
    parser.add_argument('--user', required=True, help='SNMPv3 username')
    parser.add_argument('--port', type=int, default=161)
    parser.add_argument('--level', choices=('noAuthNoPriv', 'authNoPriv', 'authPriv'),
                        default='authPriv')
    parser.add_argument('--timeout', type=float, default=3.0)
    parser.add_argument('--retries', type=int, default=1)
    parser.add_argument('--output', default='eaton_snmp_inventory.txt')
    args = parser.parse_args()
    args.auth_password = os.getenv('PYDAQ_EATON_SNMP_AUTH_PASSWORD')
    args.priv_password = os.getenv('PYDAQ_EATON_SNMP_PRIV_PASSWORD')
    try:
        lines = asyncio.run(collect_inventory(args, interactive=True))
    except Exception as exc:
        parser.exit(1, f'SNMP inventory failed: {type(exc).__name__}: {exc}\n')
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines[:14]))
    print(f'\nFull inventory saved to: {path.resolve()}')


if __name__ == '__main__':
    main()
