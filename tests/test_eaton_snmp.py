"""Offline Eaton UPS MIB mapping unit tests; safe in every pytest run."""
from __future__ import annotations

from eaton_snmp_support import OIDS, decode_output_source, parse_ups_values


def test_output_source_rfc1628_enumerations() -> None:
    expected = {
        1: 'other', 2: 'none', 3: 'normal', 4: 'bypass',
        5: 'battery', 6: 'booster', 7: 'reducer',
    }
    for code, label in expected.items():
        assert decode_output_source(code) == label
    assert decode_output_source('invalid') == 'unknown'
    assert decode_output_source(99) == 'unknown'


def test_normal_mains_sample_from_mkn_inventory() -> None:
    sample = {
        OIDS['output_source']: '3',
        OIDS['battery_status']: '2',
        OIDS['battery_voltage']: '840',
        OIDS['battery_capacity_pct']: '54',
        OIDS['battery_runtime_min']: '68',
        OIDS['active_alarms']: '0',
    }
    values = parse_ups_values(sample)
    assert values == {
        'battery_status_code': 2, 'battery_status': 'normal',
        'battery_runtime_min': 68, 'battery_capacity_pct': 54,
        'battery_voltage': 84.0, 'output_source_code': 3,
        'output_source': 'normal', 'on_battery': False, 'active_alarms': 0,
    }


def test_battery_supply_and_unknown_state() -> None:
    battery = parse_ups_values({OIDS['output_source']: '5'})
    assert battery['output_source'] == 'battery'
    assert battery['on_battery'] is True
    unknown = parse_ups_values({OIDS['output_source']: '99'})
    assert unknown['output_source'] == 'unknown'
    assert unknown['on_battery'] is None
