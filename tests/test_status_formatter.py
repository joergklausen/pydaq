from __future__ import annotations

from copy import deepcopy

import pytest

from pydaq.utils.status_formatter import format_latest_record, format_number


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (38.8512, "38.85"),
        (5.8947, "5.895"),
        (54.6996, "54.7"),
        (66.2043, "66.2"),
        (462.5377, "462.5"),
        (0.0138, "0.0138"),
        (0.0000123456, "1.235e-05"),
        (None, "-"),
        (float("nan"), "-"),
    ],
)
def test_format_number_limits_significant_digits(
    value: object,
    expected: str,
) -> None:
    assert format_number(value) == expected


def test_format_number_preserves_existing_coarser_display() -> None:
    assert format_number(18.344, decimal_places=1) == "18.3"
    assert format_number(462.5377, decimal_places=1) == "462.5"
    assert format_number(3.019, decimal_places=2) == "3.02"


def test_format_number_caps_fixed_decimal_representation() -> None:
    assert format_number(1234.56, decimal_places=1) == "1235"
    assert format_number(12345, decimal_places=0) == "1.234e+04"


def test_thermo_summary_retains_compact_precision() -> None:
    assert format_latest_record(
        {
            "dtm": "2026-08-01 13:30:00",
            "o3": 18.344,
            "flags": "0C105000",
        }
    ) == "O3=18.3 ppb flags=0C105000"


def test_hmp_summary_retains_compact_precision() -> None:
    assert format_latest_record(
        {
            "dtm": "2026-08-01 13:30:00",
            "T": 13.3483333333,
            "RH": 51.395,
            "Td": 3.5425,
        }
    ) == "T=13.3 °C RH=51.4 % Td=3.5 °C"


def test_fidas_summary_uses_no_more_than_four_significant_digits() -> None:
    assert format_latest_record(
        {
            "dtm": "2026-08-01 13:30:00",
            "61": 0.01471234,
            "62": 0.01609876,
            "64": 0.01774567,
        }
    ) == "PM1=14.7 PM2.5=16.1 PM10=17.7 µg/m³"


def test_ae33_measurements_are_limited_but_counts_and_status_remain_exact() -> None:
    summary = format_latest_record(
        {
            "dtm": "2026-08-01 13:30:00",
            "BC6": "12345",
            "FlowC": "3019",
            "unclear_3": "200",
            "Temp_3": "26",
        }
    )

    assert summary.startswith(
        "BC880=1.234e+04 ng/m³ flow=3.02 L/min tape=200 left"
    )
    assert "status=26" in summary


def test_generic_summary_limits_integer_and_float_measurements() -> None:
    assert format_latest_record(
        {
            "dtm": "2026-08-01 13:30:00",
            "count": 123456,
            "ratio": 0.00123456,
        }
    ) == "count=1.235e+05 ratio=0.001235"


def test_formatting_does_not_modify_source_record() -> None:
    record = {
        "dtm": "2026-08-01 13:30:00",
        "o3": 18.344,
        "flags": "0C105000",
    }
    original = deepcopy(record)

    format_latest_record(record)

    assert record == original
