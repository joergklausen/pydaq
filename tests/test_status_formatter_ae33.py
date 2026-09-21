from __future__ import annotations

from pydaq.utils.status_formatter import format_latest_record


def test_ae33_console_uses_overall_status_not_ledtemp() -> None:
    record = {
        "BC6": "476",
        "FlowC": "3019",
        "TapeAdvLeft": "141",
        "LedTemp": "27",
        "Status": "0",
    }

    text = format_latest_record(record)

    assert text == (
        "BC880=476 ng/m³ flow=3.02 L/min tape=141 left status=0 (OK)"
    )
    assert "status=27" not in text


def test_ae33_console_does_not_decode_legacy_temp3_as_status() -> None:
    record = {
        "BC6": "476",
        "FlowC": "3019",
        "TapeAdvLeft": "141",
        "Temp_3": "27",
    }

    text = format_latest_record(record)

    assert text == "BC880=476 ng/m³ flow=3.02 L/min tape=141 left"
    assert "status=" not in text


def test_ae33_status_289_matches_manual_example() -> None:
    record = {
        "BC6": "476",
        "Status": "289",
    }

    text = format_latest_record(record)

    assert "status=289" in text
    assert "tape advance" in text
    assert "LED warning" in text
    assert "tape nearly exhausted" in text
