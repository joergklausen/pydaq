from __future__ import annotations

import csv
import socket
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from threading import Lock
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


# AE33 TCP/IP ``FETCH Data`` table order.
#
# This is the database-table order returned by the TCP protocol. It differs
# from the ordinary exported .dat-file order documented in the AE33 manual.
# The names below are based on the manual's TCP example (section 10.2), the
# documented exported fields (section 12.1), and the diagnostic/status value
# definitions (sections 8.1 and 8.2).
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

# Minimal acquisition-time normalization for the AE33 Log table.
AE33_LOG_HEADERS: List[str] = ["dtm", "raw"]


def _utc_now_string() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_ae33_datetime(value: str) -> str | None:
    """Normalize an AE33 database timestamp to pydaq's storage format.

    The AE33 TCP database commonly returns US-style timestamps such as
    ``7/29/2026 6:06:00 AM``. ``HourlyCsvWriter`` expects an ISO-like
    timestamp, so normalize the representation without changing the clock
    time or assigning a timezone that the instrument did not provide.

    Args:
        value: Timestamp text returned by the AE33.

    Returns:
        ``YYYY-MM-DD HH:MM:SS`` when recognized, otherwise ``None``.
    """
    text = value.strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass

    for timestamp_format in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(text, timestamp_format)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    return None


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

    The AE31 streams one full data line at its configured reporting interval.
    The important acquisition rule is therefore to keep the serial port open:
    closing and reopening the port for every scheduled read can miss a line that
    arrived just before the port was opened.  This driver opens the port during
    initialization, leaves it open, and protects reads with a shared per-port
    lock.
    """

    HEADERS = AE31_HEADERS

    _registry_lock = Lock()
    _serial_by_port: Dict[str, Any] = {}
    _serial_lock_by_port: Dict[str, Lock] = {}
    _owner_by_port: Dict[str, str] = {}

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
        self._serial_lock = Lock()
        self._serial_handle: Any | None = None

    def initialize(self) -> None:
        if serial is None:  # pragma: no cover
            raise RuntimeError("pyserial is not available but AE31 requires serial communication.")
        if not self.serial_port:
            raise ValueError("AE31 requires io.device or io.port in configuration.")

        self._ensure_serial_open()
        self.logger.info(
            "AE31 ready port=%s baudrate=%s timeout_seconds=%s serial_number=%s persistent_serial=true",
            self.serial_port,
            self.baudrate,
            self.timeout_seconds,
            self.serial_number or "unknown",
        )

    def stop(self) -> None:
        """Close the persistent AE31 serial handle when the driver stops."""
        self._close_serial_handle()
        super().stop()

    def get_record(self) -> Dict[str, Any]:
        if serial is None:  # pragma: no cover
            raise RuntimeError("pyserial is not available but AE31 requires serial communication.")
        if not self.serial_port:
            raise ValueError("AE31 requires io.device or io.port in configuration.")

        try:
            self._ensure_serial_open()
            assert self._serial_handle is not None
            with self._serial_lock:
                raw = self._serial_handle.readline().decode("ascii", errors="ignore").strip()
        except Exception as exc:
            self._set_last_error(str(exc))
            self._close_serial_handle()
            raise

        if not raw:
            self._set_last_error(
                f"AE31 returned an empty line after timeout_seconds={self.timeout_seconds}."
            )
            return {}

        self._set_last_error("")
        return self._parse_data_line(raw)

    def _ensure_serial_open(self) -> None:
        """Open the AE31 serial port once and keep a shared handle per port.

        The method intentionally does **not** reset or flush input buffers.  For
        a streaming instrument, bytes that arrived between scheduled reads are
        the data we want to preserve.
        """
        if serial is None:  # pragma: no cover
            raise RuntimeError("pyserial is not available but AE31 requires serial communication.")

        with self._registry_lock:
            if self.serial_port not in self._serial_lock_by_port:
                self._serial_lock_by_port[self.serial_port] = Lock()
            self._serial_lock = self._serial_lock_by_port[self.serial_port]

        with self._serial_lock:
            existing = self._serial_by_port.get(self.serial_port)
            if existing is not None and getattr(existing, "is_open", False):
                self._serial_handle = existing
                owner = self._owner_by_port.get(self.serial_port, "unknown")
                if owner != self.name:
                    self.logger.warning(
                        "AE31 serial port %s is already owned by %s; sharing handle with %s",
                        self.serial_port,
                        owner,
                        self.name,
                    )
                return

            handle = serial.Serial(
                port=self.serial_port,
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=self.timeout_seconds,
            )
            self._serial_by_port[self.serial_port] = handle
            self._owner_by_port[self.serial_port] = self.name
            self._serial_handle = handle

    def _close_serial_handle(self) -> None:
        """Close this driver's shared serial handle, best effort."""
        if not self.serial_port:
            return

        lock = self._serial_lock_by_port.get(self.serial_port, self._serial_lock)
        with lock:
            handle = self._serial_by_port.get(self.serial_port)
            if handle is None:
                self._serial_handle = None
                return
            try:
                if getattr(handle, "is_open", False):
                    handle.close()
            except Exception as exc:
                self.logger.warning("AE31 failed to close serial port %s: %s", self.serial_port, exc)
            finally:
                self._serial_by_port.pop(self.serial_port, None)
                self._owner_by_port.pop(self.serial_port, None)
                self._serial_handle = None

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

    The driver fetches rows from the instrument's ``Data`` and ``Log`` tables.
    The ``Data`` table uses the verified AE33 TCP database-field mapping. Its
    ``dtm`` timestamp is normalized to the ISO-like form required by pydaq
    storage; the remaining values are retained as reported by the instrument.
    The ``Log`` table is stored as raw lines with a PC acquisition timestamp.

    ``get_record()`` is deliberately non-destructive: it peeks at the current
    Data-table row without advancing the persistence cursor. Only
    ``append_record()`` advances the Data/Log cursors after successful parsing,
    so a status/display read cannot consume a row before it is stored.
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
        """Return the latest Data-table row without advancing storage cursors."""
        try:
            maxid_text = self._tcpip_comm("MAXID Data", tidy=True)
            if not maxid_text:
                self._set_last_error("AE33 MAXID Data returned an empty response.")
                return {}
            maxid = int(maxid_text.strip())
            rows = self._fetch_table_rows(table="Data", first=maxid, last=maxid)
            if not rows:
                self._set_last_error(f"AE33 FETCH Data {maxid} returned no rows.")
                return {}
            record = self._parse_data_row(rows[-1])
        except Exception as exc:
            self._set_last_error(f"AE33 latest Data read failed: {exc}")
            self.logger.error("AE33 latest Data read failed: %s", exc)
            return {}

        self._set_last_error("")
        return record

    def append_record(self) -> None:
        with self._state_lock:
            if not self.state.enabled:
                return

        data_records = self._fetch_new_table_records(table="Data", table_kind="data")
        log_records = self._fetch_new_table_records(table="Log", table_kind="log")

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
    ) -> List[Dict[str, Any]]:
        """Fetch unseen table rows and advance the cursor only after parsing.

        On startup the driver retrieves up to roughly one day of retained rows,
        as before. A failed/empty FETCH or an invalid Data row does not advance
        the corresponding cursor, which allows the next cycle to retry rather
        than silently losing data.
        """
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
            first = last_id + 1

        try:
            rows = self._fetch_table_rows(table=table, first=first, last=maxid)
            if not rows:
                self._set_last_error(
                    f"AE33 FETCH {table} {first} {maxid} returned no rows."
                )
                return []

            if table_kind == "data":
                records = [self._parse_data_row(row) for row in rows]
            else:
                records = [self._parse_log_row(row) for row in rows]
        except Exception as exc:
            self._set_last_error(f"AE33 FETCH {table} parse error: {exc}")
            self.logger.error("AE33 FETCH %s parse error: %s", table, exc)
            return []

        setattr(self, last_id_attr, maxid)
        self._set_last_error("")
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
        """Parse one AE33 TCP Data-table row without silently shifting fields."""
        values = [item.strip() for item in row.split("|")]
        if len(values) != len(self.HEADERS):
            raise ValueError(
                "AE33 Data-table field count mismatch: "
                f"expected={len(self.HEADERS)} received={len(values)}"
            )

        record = dict(zip(self.HEADERS, values))
        normalized_dtm = _normalize_ae33_datetime(str(record.get("dtm", "")))
        if normalized_dtm is None:
            raise ValueError(
                f"AE33 Data-table timestamp is not recognized: {record.get('dtm')!r}"
            )
        record["dtm"] = normalized_dtm
        return record

    @staticmethod
    def _parse_log_row(row: str) -> Dict[str, Any]:
        return {"dtm": _utc_now_string(), "raw": row.strip()}

    def tape_advances_remaining(self) -> str:
        response = self._tcpip_comm("$AE33:A", tidy=True)
        return response.replace("\n", "").strip()
