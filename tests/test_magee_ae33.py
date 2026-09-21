from __future__ import annotations

from pydaq.instruments.magee import AE33, AE33_DATA_HEADERS, _normalize_ae33_datetime


def _row(**updates: str) -> str:
    values = {header: "0" for header in AE33_DATA_HEADERS}
    values.update(
        {
            "Inst_SN": "AE33-S10-01394",
            "row_id": "861145",
            "DateTime_1": "7/29/2026 6:05:00 AM",
            "dtm": "7/29/2026 6:06:00 AM",
            "DateTime_2": "12/9/2025 8:23:40 AM",
            "BC6": "308",
            "FlowC": "3019",
            "ContTemp": "24.0",
            "SupplyTemp": "41.0",
            "LedTemp": "26",
            "ContStatus": "0",
            "LedStatus": "10",
            "DetectStatus": "10",
            "ValveStatus": "0",
            "Status": "0",
            "TapeAdvCount": "483",
            "TapeAdvLeft": "200",
        }
    )
    values.update(updates)
    return "|".join(values[header] for header in AE33_DATA_HEADERS)


def test_ae33_header_uses_verified_tcp_field_names() -> None:
    tail = AE33_DATA_HEADERS[-19:]
    assert tail == [
        "BB",
        "Pressure",
        "Temperature",
        "Flow1",
        "Flow2",
        "FlowC",
        "ContTemp",
        "SupplyTemp",
        "LedTemp",
        "ContStatus",
        "LedStatus",
        "DetectStatus",
        "ValveStatus",
        "Status",
        "TapeAdvCount",
        "TapeAdvLeft",
        "unclear_4",
        "unclear_5",
        "unclear_6",
    ]


def test_ae33_us_timestamp_is_normalized_for_storage() -> None:
    assert _normalize_ae33_datetime("7/29/2026 6:06:00 AM") == "2026-07-29 06:06:00"


def test_ae33_iso_timestamp_is_retained_in_normalized_form() -> None:
    assert _normalize_ae33_datetime("2026-07-29T06:06:00") == "2026-07-29 06:06:00"


def test_ae33_parse_maps_status_and_diagnostics_correctly() -> None:
    driver = object.__new__(AE33)
    record = driver._parse_data_row(_row())

    assert record["dtm"] == "2026-07-29 06:06:00"
    assert record["ContTemp"] == "24.0"
    assert record["SupplyTemp"] == "41.0"
    assert record["LedTemp"] == "26"
    assert record["ContStatus"] == "0"
    assert record["LedStatus"] == "10"
    assert record["DetectStatus"] == "10"
    assert record["ValveStatus"] == "0"
    assert record["Status"] == "0"
    assert record["TapeAdvCount"] == "483"
    assert record["TapeAdvLeft"] == "200"


def test_ae33_parse_rejects_shifted_or_incomplete_rows() -> None:
    driver = object.__new__(AE33)
    short_row = "|".join(_row().split("|")[:-1])

    try:
        driver._parse_data_row(short_row)
    except ValueError as exc:
        assert "field count mismatch" in str(exc)
    else:
        raise AssertionError("Expected field-count mismatch to raise ValueError")
