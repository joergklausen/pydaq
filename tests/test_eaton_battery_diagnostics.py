"""Offline tests for the read-only Eaton diagnostic; no pysnmp needed."""
from __future__ import annotations

import pytest

from pydaq.utils.eaton_battery_diagnostics import read_io, report_lines, TREES
from pydaq.instruments.eaton import OIDS


def test_reads_existing_station_config(tmp_path):
    config = tmp_path / 'station.yml'
    config.write_text('instruments:\n  eaton:\n    io:\n      kind: snmp\n      host: 192.0.2.3\n      port: 161\n      user: operator\n      security_level: noAuthNoPriv\n')
    assert read_io(config)['host'] == '192.0.2.3'


def test_rejects_wrong_transport(tmp_path):
    config = tmp_path / 'station.yml'
    config.write_text('instruments:\n  eaton:\n    io:\n      kind: http\n      host: 192.0.2.3\n      user: operator\n')
    with pytest.raises(ValueError, match='snmp'):
        read_io(config)


def test_report_scales_voltage_and_keeps_unknown_enterprise_values_raw():
    raw = {OIDS['battery_voltage']: '838', OIDS['battery_capacity_pct']: '96',
           OIDS['battery_runtime_min']: '112', OIDS['output_source_code']: '3'}
    enterprise = {'1.3.6.1.4.1.534.1.99.1': '45'}
    text = '\n'.join(report_lines(raw, {'Eaton enterprise': enterprise}, {}, timestamp='2026-11-21T12:00:00Z'))
    assert '83.8' in text and 'Charge (%): 96' in text
    assert '1.3.6.1.4.1.534.1.99.1 = 45' in text
    assert 'Do NOT infer EBM health' in text


def test_truncation_is_explicit():
    raw = {OIDS['battery_voltage']: '838'}
    label = TREES[0][0]
    lines = report_lines(raw, {label: {}}, {label: 'Walk truncated at 5 objects'}, timestamp='now')
    assert any('INCOMPLETE: Walk truncated' in x for x in lines)
