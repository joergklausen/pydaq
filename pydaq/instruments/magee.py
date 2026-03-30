from __future__ import annotations

import csv
import socket
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydaq.instruments.instrument import Instrument
from pydaq.utils.storage_handler import HourlyCsvWriter, WriterConfig  # type: ignore

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None  # type: ignore


AE31_HEADERS: List[str] = [
    "dtm",
    "id",
    "date",
    "time",
    "UV370",
    "B470",
    "G520",
    "Y590",
    "R660",
    "IR880",
    "IR950",
    "flow",
    "_370",
    "sens_zero_370",
    "sens_beam_370",
    "ref_zero_370",
    "ref_beam_370",
    "att_370",
    "_470",
    "sens_zero_470",
    "sens_beam_470",
    "ref_zero_470",
    "ref_beam_470",
    "att_470",
    "_520",
    "sens_zero_520",
    "sens_beam_520",
    "ref_zero_520",
    "ref_beam_520",
    "att_520",
    "_590",
    "sens_zero_590",
    "sens_beam_590",
    "ref_zero_590",
    "ref_beam_590",
    "att_590",
    "_660",
    "sens_zero_660",
    "sens_beam_660",
    "ref_zero_660",
    "ref_beam_660",
    "att_660",
    "_880",
    "sens_zero_880",
    "sens_beam_880",
    "ref_zero_880",
    "ref_beam_880",
    "att_880",
    "_950",
    "sens_zero_950",
    "sens_beam_950",
    "ref_zero_950",
    "ref_beam_950",
    "att_950",
]


AE33_DATA_HEADERS: List[str] = [
    "Inst_SN",
    "row_id",
    "DateTime_1",
    "dtm",
    "unclear",
    "DateTime_2",
    "RefCh1",
    "Sen1Ch1",
    "Sen2Ch1",
    "RefCh2",
    "Sen1Ch2",
    "Sen2Ch2",
    "RefCh3",
    "Sen1Ch3",
    "Sen2Ch3",
    "RefCh4",
    "Sen1Ch4",
    "Sen2Ch4",
    "RefCh5",
    "Sen1Ch5",
    "Sen2Ch5",
    "RefCh6",
    "Sen1Ch6",
    "Sen2Ch6",
    "RefCh7",
    "Sen1Ch7",
    "Sen2Ch7",
    "BC11",
    "BC12",
    "BC1",
    "BC21",
    "BC22",
    "BC2",
    "BC31",
    "BC32",
    "BC3",
    "BC41",
    "BC42",
    "BC4",
    "BC51",
    "BC52",
    "BC5",
    "BC61",
    "BC62",
    "BC6",
    "BC71",
    "BC72",
    "BC7",
    "K1",
    "K2",
    "K3",
    "K4",
    "K5",
    "K6",
    "K7",
    "unclear_2",
    "Pres",
    "Temp",
    "Flow1",
    "Flow2",
    "FlowC",
    "Temp_1",
    "Temp_2",
    "Temp_3",
    "Stat_1",
    "Stat_2",
    "Stat_3",
    "Stat_4",
    "Stat_5",
    "TapeAdvCount",
    "unclear_3",
    "unclear_4",
    "unclear_5",
    "unclear_6",
]

# Minimal acquisition-time normalization for the AE33 Log table.
AE33_LOG_HEADERS: List[str] = ["dtm", "raw"]


def _utc_now_string() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clone_writer_config(base: Any, **overrides: Any) -> Any:
    """Best-effort clone of WriterConfig with selected overrides.

    This keeps the Magee driver loosely coupled to storage_handler implementation details.
    """
    if base is None:
        return WriterConfig(**overrides)

    values: Dict[str, Any] = {}
    for key in ("datetime_field", "file_prefix", "delimiter"):
        if hasattr(base, key):
            values[key] = getattr(base, key)
    values.update(overrides)
    return WriterConfig(**values)


class MageeBase(Instrument):
    """Shared helpers for Magee Aethalometer drivers."""

    def __init__(
        self,
        name: str,
        data_dir: Path,
        outbox_dir: Path,
        logger,
        *,
        headers: Optional[List[str]] = None,
        output_format: str = "csv_zip",
        writer_config=None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            name,
            data_dir,
            outbox_dir,
            logger,
            headers=headers,
            output_format=output_format,
            writer_config=writer_config,
            parameters=parameters,
        )
        self.io: Dict[str, Any] = dict(self.parameters.get("io", {}))
        self.init_parameters: Dict[str, Any] = dict(self.parameters.get("init", {}))
        self.processing_parameters: Dict[str, Any] = dict(self.parameters.get("processing", {}))
        self.output_parameters: Dict[str, Any] = dict(self.parameters.get("output", {}))
        self.instrument_id = str(self.parameters.get("id", self.init_parameters.get("id", ""))).strip()
        self.serial_number = str(self.parameters.get("serial_number", "")).strip()

    def _set_last_error(self, message: str) -> None:
        with self._state_lock:
            self.state.last_error = message


class AE31(MageeBase):
    """Magee AE31 serial driver.

    The driver follows the original AE31 approach: read one raw instrument line, prepend PC
    acquisition time and configured instrument id, and assign the result positionally to a
    stable header. No scientific remapping is done here.
    """

    HEADERS = AE31_HEADERS

    def __init__(
        self,
        name: str,
        data_dir: Path,
        outbox_dir: Path,
        logger,
        *,
        headers: Optional[List[str]] = None,
        output_format: str = "csv_zip",
        writer_config=None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            name,
            data_dir,
            outbox_dir,
            logger,
            headers=headers or self.HEADERS,
            output_format=output_format,
            writer_config=writer_config,
            parameters=parameters,
        )
        self.serial_port = str(self.io.get("device", self.io.get("port", ""))).strip()
        self.baudrate = int(self.io.get("baudrate", 9600))
        self.bytesize = int(self.io.get("bytesize", 8))
        self.parity = str(self.io.get("parity", "N")).upper()
        self.stopbits = float(self.io.get("stopbits", 1))
        self.timeout_seconds = float(
            self.io.get(
                "timeout_seconds",
                self.io.get("serial_timeout_seconds", self.parameters.get("serial_timeout", 2)),
            )
        )

    def initialize(self) -> None:
        if serial is None:  # pragma: no cover
            raise RuntimeError("pyserial is not available but AE31 requires serial communication.")
        self.logger.info(
            "AE31 ready port=%s baudrate=%s timeout_seconds=%s serial_number=%s",
            self.serial_port,
            self.baudrate,
            self.timeout_seconds,
            self.serial_number or "unknown",
        )

    def get_record(self) -> Dict[str, Any]:
        if serial is None:  # pragma: no cover
            raise RuntimeError("pyserial is not available but AE31 requires serial communication.")
        if not self.serial_port:
            raise ValueError("AE31 requires io.device or io.port in configuration.")

        try:
            with serial.Serial(
                port=self.serial_port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.timeout_seconds,
            ) as ser:
                raw = ser.readline().decode("ascii", errors="ignore").strip()
        except Exception as exc:
            self._set_last_error(str(exc))
            raise

        if not raw:
            self._set_last_error("AE31 returned an empty line.")
            return {}

        self._set_last_error("")
        return self._parse_data_line(raw)

    def _parse_data_line(self, raw: str) -> Dict[str, Any]:
        values = [_utc_now_string(), self.instrument_id]
        values.extend(self._split_csv_like_row(raw))

        if len(values) < len(self.HEADERS):
            values.extend([""] * (len(self.HEADERS) - len(values)))
        else:
            values = values[: len(self.HEADERS)]

        return dict(zip(self.HEADERS, values))

    @staticmethod
    def _split_csv_like_row(raw: str) -> List[str]:
        cleaned = raw.replace("\x00", "").strip()
        delimiter = ","
        if ";" in cleaned and "," not in cleaned:
            delimiter = ";"

        try:
            parsed = next(csv.reader(StringIO(cleaned), delimiter=delimiter, skipinitialspace=True))
            values = [item.strip() for item in parsed]
            if values:
                return values
        except Exception:
            pass

        return [item.strip() for item in cleaned.split(delimiter)]


class AE33(MageeBase):
    """Magee AE33 TCP/IP driver.

    The driver mirrors the original acquisition idea: fetch raw rows from the instrument's
    ``Data`` and ``Log`` tables and add only stable headers needed by pydaq storage. The
    ``Data`` table uses the requested acquisition header, while the ``Log`` table is stored as
    raw lines with a PC acquisition timestamp.
    """

    HEADERS = AE33_DATA_HEADERS

    def __init__(
        self,
        name: str,
        data_dir: Path,
        outbox_dir: Path,
        logger,
        *,
        headers: Optional[List[str]] = None,
        output_format: str = "csv_zip",
        writer_config=None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            name,
            data_dir,
            outbox_dir,
            logger,
            headers=headers or self.HEADERS,
            output_format=output_format,
            writer_config=writer_config,
            parameters=parameters,
        )
        self.host = str(self.io.get("host", "")).strip()
        self.port = int(self.io.get("port", 0))
        self.socket_timeout_seconds = float(self.io.get("timeout_seconds", self.io.get("timeout", 5.0)))
        self.socket_sleep_seconds = float(self.io.get("sleep_seconds", self.io.get("sleep", 0.2)))
        self.get_config_commands: List[str] = list(
            self.init_parameters.get("get_config", self.parameters.get("get_config", []))
        )
        self.set_datetime_enabled = bool(
            self.init_parameters.get("set_datetime", self.parameters.get("set_datetime", False))
        )
        self._last_data_id: Optional[int] = None
        self._last_log_id: Optional[int] = None

        log_writer_config = _clone_writer_config(
            writer_config,
            datetime_field="dtm",
            file_prefix=f"{name}-log",
        )
        self.log_writer = HourlyCsvWriter(
            instrument_name=name,
            data_directory=data_dir,
            outbox_directory=outbox_dir,
            headers=AE33_LOG_HEADERS,
            output_format=output_format,
            writer_config=log_writer_config,
            logger=self.logger,
        )

    def initialize(self) -> None:
        if not self.host or not self.port:
            raise ValueError("AE33 requires io.host and io.port in configuration.")

        self.logger.info(
            "AE33 ready host=%s port=%s timeout_seconds=%s serial_number=%s",
            self.host,
            self.port,
            self.socket_timeout_seconds,
            self.serial_number or "unknown",
        )

        for cmd in self.get_config_commands:
            response = self._tcpip_comm(cmd)
            if response:
                self.logger.info("AE33 init %s -> %s", cmd, response.replace("\n", " | ")[:300])

        if self.set_datetime_enabled:
            self._set_datetime()

    def get_record(self) -> Dict[str, Any]:
        records = self._fetch_new_table_records(table="Data", table_kind="data", latest_only=True)
        return records[-1] if records else {}

    def append_record(self) -> None:
        with self._state_lock:
            if not self.state.enabled:
                return

        data_records = self._fetch_new_table_records(table="Data", table_kind="data", latest_only=False)
        log_records = self._fetch_new_table_records(table="Log", table_kind="log", latest_only=False)

        if not data_records and not log_records:
            with self._state_lock:
                self._consecutive_empty_records += 1
                count = self._consecutive_empty_records
                last_error = self.state.last_error
            if count == 1 or (count % 10) == 0:
                self.logger.error(
                    "no record produced consecutive=%s%s",
                    count,
                    f" last_error={last_error}" if last_error else "",
                )
            return

        for record in data_records:
            if self.writer:
                self.writer.append(record)

        for record in log_records:
            self.log_writer.append(record)

        latest_record = data_records[-1] if data_records else (log_records[-1] if log_records else {})
        with self._state_lock:
            previous_empty = self._consecutive_empty_records
            self._consecutive_empty_records = 0
            self.state.latest = latest_record
            self.state.last_sample_ts = time.time()
            self.state.last_error = ""

        if previous_empty:
            self.logger.info("recovered after %s empty acquisition cycle(s)", previous_empty)

        if self.writer:
            self.writer.finalize_if_needed()
        self.log_writer.finalize_if_needed()

    def rollover(self) -> None:
        if self.writer:
            self.writer.stage_current()
        self.log_writer.stage_current()

    def _fetch_new_table_records(
        self,
        *,
        table: str,
        table_kind: str,
        latest_only: bool,
    ) -> List[Dict[str, Any]]:
        try:
            maxid_text = self._tcpip_comm(f"MAXID {table}", tidy=True)
            if not maxid_text:
                self._set_last_error(f"AE33 MAXID {table} returned an empty response.")
                return []
            maxid = int(maxid_text.strip())
        except Exception as exc:
            self._set_last_error(f"AE33 MAXID {table} parse error: {exc}")
            self.logger.error("AE33 MAXID %s parse error: %s", table, exc)
            return []

        last_id_attr = "_last_data_id" if table_kind == "data" else "_last_log_id"
        last_id = getattr(self, last_id_attr)

        if last_id is None:
            try:
                minid_text = self._tcpip_comm(f"MINID {table}", tidy=True)
                minid = int(minid_text.strip()) if minid_text else maxid
            except Exception:
                minid = maxid
            first = max(minid, maxid - 1440)
        else:
            if maxid <= last_id:
                return []
            first = maxid if latest_only else (last_id + 1)

        rows = self._fetch_table_rows(table=table, first=first, last=maxid)
        setattr(self, last_id_attr, maxid)

        if table_kind == "data":
            records = [self._parse_data_row(row) for row in rows]
        else:
            records = [self._parse_log_row(row) for row in rows]

        records = [record for record in records if record]
        if latest_only and records:
            return [records[-1]]
        return records

    def _fetch_table_rows(self, *, table: str, first: int, last: int) -> List[str]:
        rows: List[str] = []
        chunk_size = 1000
        current = first

        while current <= last:
            end = min(last, current + chunk_size - 1)
            if current == end:
                response = self._tcpip_comm(f"FETCH {table} {current}", tidy=True)
            else:
                response = self._tcpip_comm(f"FETCH {table} {current} {end}", tidy=True)
            rows.extend(self._extract_rows(response))
            current = end + 1

        return rows

    def _set_datetime(self) -> None:
        cmd = time.strftime("$AE33:T%Y%m%d%H%M%S")
        response = self._tcpip_comm(cmd)
        self.logger.info("AE33 datetime sync -> %s | response=%s", cmd, response.replace("\n", " | ")[:200])

    def _tcpip_comm(self, cmd: str, *, tidy: bool = True) -> str:
        payload = (cmd + "\r\n").encode("ascii", errors="ignore")
        received = b""

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.socket_timeout_seconds)
            sock.connect((self.host, self.port))
            sock.sendall(payload)
            time.sleep(self.socket_sleep_seconds)

            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                received += chunk

        text = received.decode("utf-8", errors="ignore")
        if tidy:
            text = text.replace("AE33>", "")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            while "\n\n" in text:
                text = text.replace("\n\n", "\n")
            text = text.strip()
        return text

    @staticmethod
    def _extract_rows(response: str) -> List[str]:
        if not response:
            return []
        return [row.strip() for row in response.splitlines() if row.strip()]

    def _parse_data_row(self, row: str) -> Dict[str, Any]:
        values = [item.strip() for item in row.split("|")]

        if len(values) < len(self.HEADERS):
            values.extend([""] * (len(self.HEADERS) - len(values)))
        else:
            values = values[: len(self.HEADERS)]

        return dict(zip(self.HEADERS, values))

    @staticmethod
    def _parse_log_row(row: str) -> Dict[str, Any]:
        return {"dtm": _utc_now_string(), "raw": row.strip()}

    def tape_advances_remaining(self) -> str:
        response = self._tcpip_comm("$AE33:A", tidy=True)
        return response.replace("\n", "").strip()
