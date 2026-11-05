"""Tests for compact Eaton UPS console status. No UPS hardware required."""
from pydaq.utils.status_formatter import format_latest_record


def test_eaton_mains_console_summary():
    record = {
        'dtm': '2026-11-05 17:00:00',
        'battery_status_code': 2,
        'seconds_on_battery': 0,
        'battery_runtime_min': 68,
        'battery_capacity_pct': 59,
        'output_source_code': 3,
        'output_source': 'normal',
        'on_battery': 0,
    }
    assert format_latest_record(record) == (
        "source=normal on_battery=0 battery_runtime=68' battery_capacity=59 %"
    )


def test_eaton_battery_console_summary():
    record = {
        'output_source_code': 5,
        'output_source': 'battery',
        'on_battery': 1,
        'battery_runtime_min': 29,
        'battery_capacity_pct': 45,
        'battery_status_code': 2,
    }
    assert format_latest_record(record) == (
        "source=battery on_battery=1 battery_runtime=29' battery_capacity=45 %"
    )


def test_eaton_missing_optional_measurements():
    record = {'output_source_code': 3, 'battery_capacity_pct': None, 'output_source': 'normal'}
    assert format_latest_record(record) == 'source=normal'
