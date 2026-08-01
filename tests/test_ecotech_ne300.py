from __future__ import annotations

import csv
from datetime import datetime, timezone
import logging
from pathlib import Path
import struct
from typing import Any, cast

import pytest

from pydaq.instruments.ecotech import NE300, NEPH
from pydaq.instruments.registry import get_driver_class
from pydaq.utils.storage_handler import HourlyCsvWriter


# Parameter columns found in the MKN NE300 files collected on 2026-08-01.
# These are used as expected output columns, not as a driver default.
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

# Regression fixture for the live failure ``header=37 record=38``.  Command 7
# may include a parameter ID that is already represented by the fixed record
# preamble.  It still occupies a value position in the packet, but must not
# become a duplicate output column.
COMMAND7_PARAMETER_IDS_WITH_METADATA = [
    4035,
    *MKN_20260801_LOGGED_PARAMETER_IDS,
]


def _bare_ne300() -> NE300:
    driver = cast(NE300, NE300.__new__(NE300))
    driver.__dict__.update(
        {
            "name": "ne300",
            "logger": logging.getLogger("test.ecotech.ne300"),
            "writer": None,
            # Deliberately provisional: every command-7 packet must be able to
            # replace this with the header transmitted by the instrument.
            "logged_parameter_ids": [999999],
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
    parameter_ids: list[int] | None = None,
    current_operation: int = 0,
    logging_period: int = 60,
) -> bytes:
    packet_parameter_ids = list(
        parameter_ids or MKN_20260801_LOGGED_PARAMETER_IDS
    )
    header_words = [
        parameter.to_bytes(4, byteorder="big")
        for parameter in packet_parameter_ids
    ]
    value_words = [
        struct.pack(">f", float(index) + 0.25)
        for index, _ in enumerate(packet_parameter_ids)
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


def test_command_7_header_generates_output_schema() -> None:
    driver = _bare_ne300()
    dtm = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)

    driver._decode_logged_data_response(
        _command_7_frame(
            driver,
            dtm=dtm,
            parameter_ids=COMMAND7_PARAMETER_IDS_WITH_METADATA,
        )
    )

    assert len(COMMAND7_PARAMETER_IDS_WITH_METADATA) == 38
    assert driver.logged_parameter_ids == MKN_20260801_LOGGED_PARAMETER_IDS
    assert driver.HEADERS == [
        "dtm",
        "4035",
        "2002",
        *[
            str(parameter)
            for parameter in MKN_20260801_LOGGED_PARAMETER_IDS
        ],
    ]
    assert len(driver.HEADERS) == 40


def test_command_7_decodes_38_packet_fields_to_40_output_columns() -> None:
    driver = _bare_ne300()
    dtm = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    response = _command_7_frame(
        driver,
        dtm=dtm,
        parameter_ids=COMMAND7_PARAMETER_IDS_WITH_METADATA,
        current_operation=7,
    )

    records = driver._decode_logged_data_response(response)

    assert len(records) == 1
    record = records[0]
    assert record["dtm"] == dtm

    # The fixed command-7 preamble is authoritative for these metadata values;
    # the value occupying the packet position for parameter 4035 is consumed
    # but must not overwrite current_operation or add another column.
    assert record[4035] == 7
    assert record[2002] == 60
    assert record[2635000] == 1.25
    assert record[6450090] == 37.25

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
    assert formatted["4035"] == 7
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

    with pytest.raises(ValueError, match="before a parameter header"):
        driver._decode_logged_data_response(response)


def test_complete_record_is_written_with_all_40_columns(
    tmp_path: Path,
) -> None:
    driver = _bare_ne300()
    headers = [
        "dtm",
        "4035",
        "2002",
        *[
            str(parameter)
            for parameter in MKN_20260801_LOGGED_PARAMETER_IDS
        ],
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
        _command_7_frame(
            driver,
            dtm=dtm,
            parameter_ids=COMMAND7_PARAMETER_IDS_WITH_METADATA,
            current_operation=7,
        )
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
    assert rows[1][1:3] == ["7", "60"]


def test_compact_logged_summary_reports_ne300_values_and_mode() -> None:
    driver = _bare_ne300()
    record = {
        "dtm": "2026-08-01 12:59:00",
        "4035": 0,
        "2002": 60,
        "2635000": 10.125,
        "2635090": 1.25,
        "2525000": 20.5,
        "2525090": 2.625,
        "2450000": 30.875,
        "2450090": 3.0,
    }

    summary = driver._format_compact_logged_summary(record)

    assert summary == (
        "ssp|bssp (Mm-1) "
        "r: 10.12|1.25 g: 20.5|2.625 "
        "b: 30.88|3 mode=ambient"
    )


def test_compact_logged_summary_handles_missing_values() -> None:
    driver = _bare_ne300()
    summary = driver._format_compact_logged_summary(
        {
            "dtm": "2026-08-01 13:00:00",
            "4035": 9,
        }
    )

    assert "r: -|-" in summary
    assert "g: -|-" in summary
    assert "b: -|-" in summary
    assert "mode=operation-9" in summary
    assert summary.endswith("mode=operation-9")


def test_command_7_packet_header_diagnostic_is_debug_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = _bare_ne300()
    dtm = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)

    with caplog.at_level(logging.DEBUG, logger="test.ecotech.ne300"):
        driver._decode_logged_data_response(
            _command_7_frame(
                driver,
                dtm=dtm,
                parameter_ids=[0, *MKN_20260801_LOGGED_PARAMETER_IDS],
            )
        )

    diagnostics = [
        record
        for record in caplog.records
        if "command-7 packet header fields=" in record.getMessage()
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0].levelno == logging.DEBUG


def test_stdout_number_uses_at_most_four_significant_digits() -> None:
    assert NE300._stdout_number(38.8512) == "38.85"
    assert NE300._stdout_number(5.8947) == "5.895"
    assert NE300._stdout_number(54.6996) == "54.7"
    assert NE300._stdout_number(0.0138) == "0.0138"
    assert NE300._stdout_number(None) == "-"
