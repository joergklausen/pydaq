from __future__ import annotations

import csv
from datetime import datetime, timezone
import logging
from pathlib import Path
import struct
from typing import Any, cast

from pydaq.instruments.ecotech import NE300, NEPH
from pydaq.instruments.registry import get_driver_class
from pydaq.utils.storage_handler import HourlyCsvWriter


MKN_20260801_LOGGED_PARAMETER_IDS = [
    2635000,
    2525000,
    2450000,
    2635090,
    2525090,
    2450090,
    5001,
    5002,
    5003,
    5004,
    5005,
    5006,
    5010,
    26635000,
    26525000,
    26450000,
    13525000,
    15635000,
    15525000,
    15450000,
    11635000,
    11525000,
    11450000,
    11635090,
    11525090,
    11450090,
    6007,
    6008,
    6001,
    6002,
    6003,
    6635000,
    6525000,
    6450000,
    6635090,
    6525090,
    6450090,
]


def _bare_ne300() -> NE300:
    driver = cast(NE300, NE300.__new__(NE300))
    driver.__dict__.update(
        {
            "name": "ne300",
            "logger": logging.getLogger("test.ecotech.ne300"),
            "writer": None,
            "logged_parameter_ids": list(
                MKN_20260801_LOGGED_PARAMETER_IDS
            ),
        }
    )
    return driver


def _record(
    record_type: int,
    current_operation: int,
    timestamp: int,
    logging_period: int,
    words: list[bytes],
) -> bytes:
    return (
        bytes([record_type, current_operation, 0, 0])
        + timestamp.to_bytes(4, byteorder="big")
        + logging_period.to_bytes(4, byteorder="big")
        + len(words).to_bytes(4, byteorder="big")
        + b"".join(words)
    )


def _command_7_frame(
    driver: NE300,
    *,
    dtm: datetime,
    current_operation: int = 0,
    logging_period: int = 60,
) -> bytes:
    header_words = [
        parameter.to_bytes(4, byteorder="big")
        for parameter in MKN_20260801_LOGGED_PARAMETER_IDS
    ]
    value_words = [
        struct.pack(">f", float(index) + 0.25)
        for index, _ in enumerate(MKN_20260801_LOGGED_PARAMETER_IDS)
    ]
    timestamp = int.from_bytes(
        driver._acoem_datetime_to_timestamp(dtm),
        byteorder="big",
    )
    body = _record(1, 0, 0, 0, header_words) + _record(
        0,
        current_operation,
        timestamp,
        logging_period,
        value_words,
    )
    prefix = (
        bytes([2, 0, 7, 3])
        + len(body).to_bytes(2, byteorder="big")
        + body
    )
    return prefix + driver._acoem_checksum(prefix) + bytes([4])


def test_registry_uses_logged_data_ne300_subclass() -> None:
    assert get_driver_class("ne300") is NE300
    assert get_driver_class("aurora3000") is NEPH


def test_current_value_status_parameter_mapping_is_not_reversed() -> None:
    assert NEPH.ACOEM_PARAMETER_TO_FIELD[4035] == "major_state"
    assert NEPH.ACOEM_PARAMETER_TO_FIELD[4036] == "DIO_state"


def test_default_logged_header_matches_mkn_files_from_20260801() -> None:
    assert (
        NE300.DEFAULT_LOGGED_PARAMETER_IDS
        == MKN_20260801_LOGGED_PARAMETER_IDS
    )
    assert len(NE300.DEFAULT_LOGGED_PARAMETER_IDS) == 37


def test_command_7_decodes_complete_40_column_record() -> None:
    driver = _bare_ne300()
    dtm = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    response = _command_7_frame(driver, dtm=dtm)

    records = driver._decode_logged_data_response(response)

    assert len(records) == 1
    record = records[0]
    assert record["dtm"] == dtm
    assert record[4035] == 0
    assert record[2002] == 60
    assert record[2635000] == 0.25
    assert record[6450090] == 36.25

    formatted = driver._format_logged_record(record)
    expected_headers = [
        "dtm",
        "4035",
        "2002",
        *[
            str(parameter)
            for parameter in MKN_20260801_LOGGED_PARAMETER_IDS
        ],
    ]
    assert list(formatted) == expected_headers
    assert len(formatted) == 40
    assert formatted["dtm"] == "2026-08-01 00:00:00"
    assert formatted["4035"] == 0
    assert formatted["2002"] == 60


def test_logged_timestamp_round_trip() -> None:
    driver = _bare_ne300()
    expected = datetime(
        2026,
        8,
        1,
        2,
        59,
        0,
        tzinfo=timezone.utc,
    )
    packed = driver._acoem_datetime_to_timestamp(expected)
    decoded = driver._acoem_timestamp_to_datetime(
        int.from_bytes(packed, byteorder="big")
    )
    assert decoded == expected


def test_command_7_rejects_data_without_header() -> None:
    driver = _bare_ne300()
    dtm = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    timestamp = int.from_bytes(
        driver._acoem_datetime_to_timestamp(dtm),
        byteorder="big",
    )
    value_words = [struct.pack(">f", 1.0)]
    body = _record(0, 0, timestamp, 60, value_words)
    prefix = (
        bytes([2, 0, 7, 3])
        + len(body).to_bytes(2, byteorder="big")
        + body
    )
    response = prefix + driver._acoem_checksum(prefix) + bytes([4])

    try:
        driver._decode_logged_data_response(response)
    except ValueError as exc:
        assert "before a parameter header" in str(exc)
    else:
        raise AssertionError("Expected command-7 data without header to fail")


def test_complete_record_is_written_with_all_40_columns(
    tmp_path: Path,
) -> None:
    driver = _bare_ne300()
    headers = [
        "dtm",
        "4035",
        "2002",
        *[str(parameter) for parameter in MKN_20260801_LOGGED_PARAMETER_IDS],
    ]
    writer = HourlyCsvWriter(
        instrument_name="ne300",
        data_directory=tmp_path / "data" / "ne300",
        outbox_directory=tmp_path / "outbox" / "ne300",
        headers=headers,
        output_format="csv",
    )
    driver.writer = writer

    dtm = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    record = driver._decode_logged_data_response(
        _command_7_frame(driver, dtm=dtm)
    )[0]
    writer.append(driver._format_logged_record(record))
    writer.stage_current()

    csv_path = (
        tmp_path
        / "data"
        / "ne300"
        / "2026"
        / "08"
        / "ne300-2026080100.csv"
    )
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))

    assert len(rows) == 2
    assert rows[0] == headers
    assert len(rows[1]) == 40
    assert rows[1][0] == "2026-08-01 00:00:00"
    assert rows[1][1:3] == ["0", "60"]
