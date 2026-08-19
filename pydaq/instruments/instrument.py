"""pydaq.instruments.instrument

This module defines the :class:`~pydaq.instruments.instrument.Instrument` base class used by
all concrete instrument drivers, plus a small reusable helper for line-oriented IO.

The key design goal is **fault isolation**: each instrument runs in its own worker thread and
executes tasks taken from a queue. The scheduler thread(s) should only enqueue work and never
perform instrument IO directly.

Conventions
-----------
- Drivers implement :meth:`Instrument.get_record` to return one measurement record as a mapping.
- The platform standard timestamp field is ``dtm`` and refers to **PC acquisition time**.
  Instrument internal time/date may be included in the record as additional fields.
- If a driver forgets to include ``dtm``, the base class injects it (UTC) before writing.

Shared IO helper
----------------
Many instruments are "line oriented": send an ASCII command and read back an ASCII response
over either serial or TCP. To avoid duplicating that plumbing in each driver, this module
provides :class:`LineComms` which supports:

- serial or TCP request/response
- one request at a time (internal lock)
- retries + backoff
- optional "keep_open" for serial ports

If an instrument uses a binary protocol or streams continuously, drivers can ignore
:class:`LineComms` and implement their own IO.
"""

from __future__ import annotations

import importlib
import inspect
import socket
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Any, Callable, Dict, List, Mapping, Optional
from decimal import Decimal

from pydaq.utils.storage_handler import (HourlyCsvWriter,  # type: ignore
                                         WriterConfig)

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None  # type: ignore


def utc_timestamp_string(now: Optional[datetime] = None) -> str:
    """Return a stable UTC timestamp string (seconds resolution).

    Args:
        now: Optional datetime to format (UTC). If not provided, uses current UTC time.

    Returns:
        Timestamp formatted as ``YYYY-mm-dd HH:MM:SS`` (UTC).
    """
    dtm = now or datetime.now(timezone.utc)
    return dtm.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class InstrumentState:
    """Runtime state for dashboards and orchestration."""
    enabled: bool = True
    last_sample_ts: float = 0.0
    last_error: str = ""
    last_error_reported: str = ""
    latest: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LineCommsConfig:
    """Normalized configuration for line-oriented request/response IO."""
    kind: str  # "serial" | "tcp"
    terminator: str = "\r"
    encoding: str = "ascii"

    # Serial options
    port: str = ""
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    timeout_seconds: float = 2.0
    write_timeout_seconds: float = 2.0
    keep_open: bool = True

    # TCP options
    host: str = ""
    port_tcp: int = 0
    socket_timeout_seconds: float = 5.0

    # Reliability
    retries: int = 2
    backoff_seconds: float = 0.2
    read_max_bytes: int = 65536


class LineComms:
    """Line-oriented request/response communications over serial or TCP.

    Args:
        io_config: Mapping from YAML (typically ``instrument.io``).
        logger: Optional logger for diagnostics.
    """

    def __init__(self, io_config: Dict[str, Any], logger=None) -> None:
        self.logger = logger
        self._lock = Lock()
        self._serial = None

        kind = str(io_config.get("kind", io_config.get("type", "serial"))).strip().lower()
        terminator = str(io_config.get("terminator", "\r"))
        encoding = str(io_config.get("encoding", "ascii"))

        if kind == "serial":
            self.cfg = LineCommsConfig(
                kind="serial",
                terminator=terminator,
                encoding=encoding,
                port=str(io_config.get("port", io_config.get("device", ""))),
                baudrate=int(io_config.get("baudrate", 9600)),
                bytesize=int(io_config.get("bytesize", 8)),
                parity=str(io_config.get("parity", "N")),
                stopbits=float(io_config.get("stopbits", 1.0)),
                timeout_seconds=float(io_config.get("timeout_seconds", 2.0)),
                write_timeout_seconds=float(io_config.get("write_timeout_seconds", io_config.get("timeout_seconds", 2.0))),
                keep_open=bool(io_config.get("keep_open", True)),
                retries=int(io_config.get("retries", 2)),
                backoff_seconds=float(io_config.get("backoff_seconds", 0.2)),
                read_max_bytes=int(io_config.get("read_max_bytes", 65536)),
            )
        elif kind in {"tcp", "tcpip", "socket"}:
            self.cfg = LineCommsConfig(
                kind="tcp",
                terminator=terminator,
                encoding=encoding,
                host=str(io_config.get("host", io_config.get("ip", ""))),
                port_tcp=int(io_config.get("port", io_config.get("port_tcp", 0))),
                socket_timeout_seconds=float(io_config.get("timeout_seconds", 5.0)),
                retries=int(io_config.get("retries", 2)),
                backoff_seconds=float(io_config.get("backoff_seconds", 0.2)),
                read_max_bytes=int(io_config.get("read_max_bytes", 65536)),
            )
        else:
            raise ValueError(f"Unsupported io.kind '{kind}' (expected 'serial' or 'tcp').")

    def close(self) -> None:
        """Close any open handle (best-effort)."""
        try:
            if self._serial and getattr(self._serial, "is_open", False):
                self._serial.close()
        except Exception:
            pass
        self._serial = None

    def request(self, cmd: str, *, prefix: bytes = b"", terminator: Optional[str] = None) -> str:
        """Send one command and return decoded response (best-effort).

        Args:
            cmd: Command string (without terminator).
            prefix: Optional bytes prepended to the payload (e.g., address byte).
            terminator: Optional terminator override. Defaults to configured terminator.

        Returns:
            Decoded response string (may be empty on timeout/error).
        """
        cmd = cmd.strip()
        if not cmd:
            return ""

        last_err: Optional[Exception] = None
        max_attempts = max(1, self.cfg.retries + 1)
        endpoint = self.cfg.port if self.cfg.kind == "serial" else f"{self.cfg.host}:{self.cfg.port_tcp}"

        for attempt in range(max_attempts):
            try:
                with self._lock:
                    if self.cfg.kind == "serial":
                        return self._request_serial(cmd, prefix=prefix, terminator=terminator)
                    return self._request_tcp(cmd, prefix=prefix, terminator=terminator)
            except Exception as exc:
                last_err = exc
                if self.logger:
                    level = self.logger.error if attempt == (max_attempts - 1) else self.logger.warning
                    level(
                        "IO request failed kind=%s endpoint=%s cmd=%s attempt=%s/%s err=%s",
                        self.cfg.kind,
                        endpoint,
                        cmd,
                        attempt + 1,
                        max_attempts,
                        exc,
                    )
                # Force clean reopen on next attempt
                self.close()
                if attempt < (max_attempts - 1):
                    time.sleep(self.cfg.backoff_seconds * (attempt + 1))
                continue

        return ""

    def _ensure_serial_open(self) -> None:
        if self.cfg.kind != "serial":
            return
        if self._serial and getattr(self._serial, "is_open", False):
            return
        if serial is None:  # pragma: no cover
            raise RuntimeError("pyserial is not available but io.kind=serial was requested.")
        if not self.cfg.port:
            raise ValueError("io.port (or io.device) is required for serial instruments.")

        self._serial = serial.Serial(
            port=self.cfg.port,
            baudrate=self.cfg.baudrate,
            bytesize=self.cfg.bytesize,
            parity=self.cfg.parity,
            stopbits=self.cfg.stopbits,
            timeout=self.cfg.timeout_seconds,
            write_timeout=self.cfg.write_timeout_seconds,
        )

    def _request_serial(self, cmd: str, *, prefix: bytes, terminator: Optional[str]) -> str:
        self._ensure_serial_open()
        assert self._serial is not None

        # best-effort buffer cleanup
        try:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except Exception:
            pass

        term = terminator if terminator is not None else self.cfg.terminator
        payload = prefix + f"{cmd}{term}".encode(self.cfg.encoding, errors="ignore")

        self._serial.write(payload)
        self._serial.flush()

        buf = b""
        deadline = time.time() + max(0.5, float(self.cfg.timeout_seconds))
        while time.time() < deadline and len(buf) < self.cfg.read_max_bytes:
            chunk = self._serial.read(1024)
            if chunk:
                buf += chunk
                if b"*" in buf or buf.endswith(b"\r") or buf.endswith(b"\n"):
                    break

        text = buf.decode("utf-8", errors="ignore").strip()
        if not self.cfg.keep_open:
            self.close()
        return text

    def _request_tcp(self, cmd: str, *, prefix: bytes, terminator: Optional[str]) -> str:
        if not self.cfg.host or not self.cfg.port_tcp:
            raise ValueError("io.host and io.port are required for tcp instruments.")

        term = terminator if terminator is not None else self.cfg.terminator
        payload = prefix + f"{cmd}{term}".encode(self.cfg.encoding, errors="ignore")

        buf = b""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self.cfg.socket_timeout_seconds)
            s.connect((self.cfg.host, int(self.cfg.port_tcp)))
            s.sendall(payload)

            deadline = time.time() + max(0.5, float(self.cfg.socket_timeout_seconds))
            while time.time() < deadline and len(buf) < self.cfg.read_max_bytes:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                if b"*" in buf or buf.endswith(b"\r") or buf.endswith(b"\n"):
                    break

        return buf.decode("utf-8", errors="ignore").strip()


class TimeBucketAggregator:
    """Reusable UTC-aligned fixed-period record aggregator.

    Drivers can use this when an instrument is polled more frequently than the
    output cadence.  The aggregator buffers raw records and emits exactly one
    aggregate when a new time bucket begins.  Until then, :meth:`add` returns
    ``None``.

    The default strategy is to average numeric fields and ignore empty values.
    Per-field strategies can be supplied for state or cumulative fields:
    ``mean``, ``sum``, ``last``, ``first`` or ``mode``.
    """

    def __init__(
        self,
        *,
        period_seconds: int,
        datetime_field: str = "dtm",
        timestamp: str = "start",
        default_method: str = "mean",
        methods: Mapping[str, str] | None = None,
        datetime_format: str = "%Y-%m-%d %H:%M:%S",
        logger=None,
        name: str = "",
    ) -> None:
        if period_seconds <= 0:
            raise ValueError("period_seconds must be > 0 for TimeBucketAggregator.")
        timestamp = timestamp.strip().lower()
        if timestamp not in {"start", "end"}:
            raise ValueError("timestamp must be 'start' or 'end'.")
        default_method = default_method.strip().lower()
        if default_method not in {"mean", "sum", "last", "first", "mode"}:
            raise ValueError("default_method must be one of: mean, sum, last, first, mode.")

        self.period_seconds = int(period_seconds)
        self.datetime_field = datetime_field
        self.timestamp = timestamp
        self.default_method = default_method
        self.methods = {str(k): str(v).strip().lower() for k, v in (methods or {}).items()}
        self.datetime_format = datetime_format
        self.logger = logger
        self.name = name
        self._bucket_start: datetime | None = None
        self._records: list[dict[str, Any]] = []

    def add(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        """Buffer one record and emit a completed aggregate if available."""
        dtm = self._record_datetime(record)
        bucket_start = self._bucket_start_for(dtm)
        record_copy = dict(record)

        if self._bucket_start is None:
            self._bucket_start = bucket_start
            self._records = [record_copy]
            return None

        if bucket_start == self._bucket_start:
            self._records.append(record_copy)
            return None

        if bucket_start < self._bucket_start:
            if self.logger:
                self.logger.warning(
                    "[%s] aggregation timestamp moved backwards: new_bucket=%s current_bucket=%s; resetting buffer",
                    self.name,
                    bucket_start.isoformat(),
                    self._bucket_start.isoformat(),
                )
            self._bucket_start = bucket_start
            self._records = [record_copy]
            return None

        completed_start = self._bucket_start
        completed_records = self._records
        self._bucket_start = bucket_start
        self._records = [record_copy]
        return self._aggregate(completed_records, completed_start)

    def flush(self) -> dict[str, Any] | None:
        """Emit the currently buffered bucket, if any, and clear the buffer."""
        if self._bucket_start is None or not self._records:
            return None
        completed_start = self._bucket_start
        completed_records = self._records
        self._bucket_start = None
        self._records = []
        return self._aggregate(completed_records, completed_start)

    def _aggregate(self, records: list[dict[str, Any]], bucket_start: datetime) -> dict[str, Any]:
        if not records:
            return {}

        dtm = bucket_start if self.timestamp == "start" else bucket_start + timedelta(seconds=self.period_seconds)
        result: dict[str, Any] = {self.datetime_field: self._format_datetime(dtm)}

        fields: list[str] = []
        seen: set[str] = set()
        for record in records:
            for field in record:
                if field == self.datetime_field or field in seen:
                    continue
                seen.add(field)
                fields.append(field)

        for field in fields:
            values = [record.get(field) for record in records if record.get(field) not in (None, "")]
            if not values:
                result[field] = None
                continue
            method = self.methods.get(field, self.default_method)
            result[field] = self._aggregate_values(values, method)

        return result

    def _aggregate_values(self, values: list[Any], method: str) -> Any:
        if method == "last":
            return values[-1]
        if method == "first":
            return values[0]
        if method == "mode":
            return Counter(values).most_common(1)[0][0]

        numeric = [self._as_float(value) for value in values]
        if method == "sum":
            return sum(numeric)
        if method == "mean":
            return sum(numeric) / len(numeric)
        raise ValueError(f"Unsupported aggregation method {method!r}.")

    def _record_datetime(self, record: Mapping[str, Any]) -> datetime:
        value = record.get(self.datetime_field)
        if isinstance(value, datetime):
            dtm = value
        elif isinstance(value, str):
            text = value.strip().replace("T", " ")
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dtm = datetime.fromisoformat(text)
            except ValueError:
                dtm = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        else:
            raise ValueError(f"Record has no valid {self.datetime_field!r}: {record!r}")

        if dtm.tzinfo is None:
            return dtm.replace(tzinfo=timezone.utc)
        return dtm.astimezone(timezone.utc)

    def _bucket_start_for(self, timestamp: datetime) -> datetime:
        dtm = timestamp.astimezone(timezone.utc).replace(microsecond=0)
        epoch_seconds = int(dtm.timestamp())
        bucket_epoch = epoch_seconds - (epoch_seconds % self.period_seconds)
        return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).replace(microsecond=0)

    def _format_datetime(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).strftime(self.datetime_format)

    @staticmethod
    def _as_float(value: object) -> float:
        if value is None or value == "":
            raise ValueError("Cannot convert an empty value to float for aggregation.")
        if isinstance(value, (int, float, Decimal, str)):
            return float(value)
        raise TypeError(f"Cannot convert {type(value).__name__} to float for aggregation.")


class Instrument:
    """Abstract instrument interface with a worker thread and task queue."""

    def __init__(
        self,
        name: str,
        data_dir: Path,
        outbox_dir: Path,
        logger,
        *,
        headers: Optional[List[str]] = None,
        output_format: str = "csv_zip",
        writer_config: Optional[WriterConfig] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initializes the Instrument.

        Args:
            name: Instrument name (usually matches YAML key).
            data_dir: Directory for authoritative instrument data.
            outbox_dir: Directory for staged files to be transmitted.
            logger: Application logger; a child logger will be created.
            headers: Optional column order for CSV output. If ``None``, writer is disabled.
            output_format: ``csv``, ``csv_zip`` (default), or ``parquet`` (writer-dependent).
            writer_config: Writer settings (datetime field name, file prefix, delimiter).
            parameters: Free-form mapping containing parsed config sections for the driver.
                Typical structure: ``{"io": ..., "init": ..., "processing": ..., "output": ...}``.
        """
        self.name = name
        self.data_dir = data_dir
        self.outbox_dir = outbox_dir
        self.logger = logger.getChild(f"instrument.{name}")
        self.state = InstrumentState(enabled=True)
        self.parameters = parameters or {}

        self._task_queue: Queue[Callable[[], None]] = Queue()
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        self._state_lock = Lock()
        self._consecutive_empty_records = 0
        self.empty_record_is_ok = False
        # Per-task signature used to suppress repeated identical operator errors.
        self._last_task_error_signatures: Dict[str, str] = {}

        self.writer: Optional[HourlyCsvWriter] = None
        if headers:
            self.writer = HourlyCsvWriter(
                instrument_name=name,
                data_directory=data_dir,
                outbox_directory=outbox_dir,
                headers=headers,
                output_format=output_format,
                writer_config=writer_config,
                logger=self.logger,
            )

    def start(self) -> None:
        """Starts the instrument worker thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._worker_loop, name=f"pydaq:{self.name}", daemon=True)
        self._thread.start()
        self.logger.info("started")

    def stop(self) -> None:
        """Stops the instrument worker thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self.logger.info("stopped")

    def set_enabled(self, enabled: bool) -> None:
        """Enables/disables sampling at runtime.

        Args:
            enabled: If ``False``, scheduled sampling is skipped.
        """
        with self._state_lock:
            self.state.enabled = enabled
        self.logger.info("enabled=%s", enabled)

    # Scheduler-facing API
    def request_initialize(self) -> None:
        """Enqueue one-time initialization."""
        self._task_queue.put(self.initialize)

    def request_reading(self) -> None:
        """Enqueue one sample operation."""
        self._task_queue.put(self.append_record)

    def request_rollover(self) -> None:
        """Enqueue an explicit rollover operation."""
        self._task_queue.put(self.rollover)

    def request_transmit(self, transmit_callable: Callable[[str], None]) -> None:
        """Enqueue transmission work.

        Args:
            transmit_callable: Function that accepts ``instrument_name`` and performs transmission.
        """
        self._task_queue.put(lambda: transmit_callable(self.name))

    @staticmethod
    def _task_label(task: Callable[[], None]) -> str:
        name = getattr(task, "__name__", "task")
        return {
            "initialize": "initialization",
            "append_record": "acquisition",
            "rollover": "rollover",
        }.get(name, name.replace("_", " "))

    def _configured_endpoint(self) -> str:
        io_cfg = self.parameters.get("io", {}) if isinstance(self.parameters, dict) else {}
        if not isinstance(io_cfg, dict):
            return ""

        kind = str(io_cfg.get("kind", io_cfg.get("type", "")) or "").strip().lower()
        host = str(io_cfg.get("host", io_cfg.get("ip", "")) or "").strip()
        port = io_cfg.get("port", io_cfg.get("port_tcp"))
        device = str(io_cfg.get("device", "") or "").strip()

        if host:
            endpoint = host + (f":{port}" if port not in (None, "") else "")
            transport = "tcp" if kind in {"socket", "tcp", "tcpip"} else (kind or "tcp")
            return f"{transport} {endpoint}"
        if kind == "serial" or device or (isinstance(port, str) and port.upper().startswith("COM")):
            serial_port = str(port or device or "").strip()
            return f"serial {serial_port}".strip()
        return ""

    def _operator_error(self, task: Callable[[], None], exc: Exception) -> str:
        """Return a concise, operator-facing description of a worker failure."""
        label = self._task_label(task)
        endpoint = self._configured_endpoint()
        where = f" {endpoint}" if endpoint else ""
        text = str(exc).strip()

        if isinstance(exc, TimeoutError):
            return f"unavailable:{where} timed out during {label}"
        if isinstance(exc, ConnectionRefusedError):
            return f"unavailable:{where} connection refused during {label}"
        if isinstance(exc, PermissionError):
            detail = "access denied" if not text else text
            return f"unavailable:{where} {detail} during {label}"
        if isinstance(exc, (ConnectionError, OSError)):
            detail = text or exc.__class__.__name__
            return f"unavailable:{where} {detail} during {label}"

        detail = f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__
        return f"software/protocol error during {label}: {detail}"

    def _worker_loop(self) -> None:
        """Worker thread loop: execute queued tasks with compact operator errors."""
        while not self._stop_event.is_set():
            try:
                task = self._task_queue.get(timeout=0.5)
            except Empty:
                continue

            task_name = getattr(task, "__name__", "task")
            try:
                task()
                # A later success of the same task means an identical future error
                # should be surfaced again rather than suppressed forever.
                self._last_task_error_signatures.pop(task_name, None)
            except Exception as exc:
                operator_error = self._operator_error(task, exc)
                signature = f"{exc.__class__.__name__}:{exc}"
                repeated = self._last_task_error_signatures.get(task_name) == signature
                self._last_task_error_signatures[task_name] = signature

                with self._state_lock:
                    self.state.last_error = operator_error
                    self.state.last_error_reported = operator_error if repeated else ""

                if repeated:
                    # Preserve diagnostics for DEBUG/file logging without flooding
                    # an INFO/ERROR console every acquisition cycle.
                    self.logger.debug(
                        "[%s] %s (repeated)",
                        self.name,
                        operator_error,
                        exc_info=True,
                    )
                else:
                    # The custom console handler suppresses traceback text for this
                    # record, while the rotating file handler still gets exc_info.
                    self.logger.error(
                        "[%s] %s",
                        self.name,
                        operator_error,
                        exc_info=True,
                        extra={"console_compact": True},
                    )
                    with self._state_lock:
                        self.state.last_error_reported = operator_error
            finally:
                self._task_queue.task_done()

    # Driver hooks
    def initialize(self) -> None:
        """Optional driver initialization (override in subclasses)."""
        return

    def get_record(self) -> Dict[str, Any]:
        """Retrieves one record from the instrument.

        Returns:
            Record mapping. Should include ``dtm`` (PC acquisition time) as a string.

        Raises:
            NotImplementedError: If not overridden by subclass.
        """
        raise NotImplementedError

    # Optional alias for readability in some drivers
    def collect_record(self) -> Dict[str, Any]:
        """Alias for :meth:`get_record`."""
        return self.get_record()

    def append_record(self) -> None:
        """Collect one record and append it to the writer (if enabled)."""
        with self._state_lock:
            if not self.state.enabled:
                return

        record = self.get_record()
        if not record:
            if getattr(self, "empty_record_is_ok", False):
                self.logger.debug("no complete aggregate record ready yet")
                return

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

        # Inject dtm if missing so the writer can roll by hour.
        if self.writer:
            dt_name = getattr(getattr(self.writer, "config", None), "datetime_field", "dtm")
            if dt_name not in record:
                record[dt_name] = utc_timestamp_string()

        with self._state_lock:
            previous_empty = self._consecutive_empty_records
            self._consecutive_empty_records = 0
            self.state.latest = record
            self.state.last_sample_ts = time.time()
            self.state.last_error = ""
            self.state.last_error_reported = ""

        if previous_empty:
            self.logger.info("recovered after %s empty acquisition cycle(s)", previous_empty)

        if self.writer:
            self.writer.append(record)
            self.writer.finalize_if_needed()

    def rollover(self) -> None:
        """Force staging of the current rolling file."""
        if self.writer:
            self.writer.stage_current()

# def get_driver_class(
#     driver: str | type["Instrument"],
#     *,
#     aliases: Mapping[str, str | list[str]] | None = None,
# ) -> type["Instrument"]:
#     """
#     Resolve a driver identifier to an Instrument subclass.

#     Accepts:
#       - an Instrument subclass (returned unchanged)
#       - "package.module:ClassName"  (recommended)
#       - "package.module.ClassName"
#       - legacy short names like "49i" / "thermo49i" via an alias map

#     The import happens only when this function is called (helps avoid circular imports).
#     """
#     # Already a class?
#     if inspect.isclass(driver) and issubclass(driver, Instrument):
#         return driver

#     if not isinstance(driver, str) or not driver.strip():
#         raise TypeError(f"driver must be a non-empty str or Instrument subclass, got {type(driver)}")

#     name = driver.strip()

#     # Default legacy aliases (try multiple candidates because names may differ across refactors)
#     default_aliases: dict[str, str | list[str]] = {
#         "49i": [
#             "pydaq.instruments.thermo:Thermo49i",
#             "pydaq.instruments.thermo:Thermo",
#             "pydaq.instruments.thermo49i:Thermo49i",
#             "pydaq.instruments.thermo49i:Thermo",
#         ],
#         "thermo49i": [
#             "pydaq.instruments.thermo:Thermo49i",
#             "pydaq.instruments.thermo:Thermo",
#             "pydaq.instruments.thermo49i:Thermo49i",
#             "pydaq.instruments.thermo49i:Thermo",
#         ],
#     }

#     if aliases:
#         default_aliases.update(dict(aliases))

#     target = default_aliases.get(name, name)
#     candidates = target if isinstance(target, list) else [target]

#     last_err: Exception | None = None

#     for cand in candidates:
#         try:
#             if ":" in cand:
#                 mod_name, attr = cand.split(":", 1)
#             else:
#                 mod_name, attr = cand.rsplit(".", 1)

#             mod = importlib.import_module(mod_name)
#             cls: Any = getattr(mod, attr)

#             if not inspect.isclass(cls) or not issubclass(cls, Instrument):
#                 raise TypeError(f"{cand} resolved to {cls!r}, not an Instrument subclass")

#             return cls

#         except Exception as e:
#             last_err = e

#     raise ImportError(f"Could not resolve driver '{driver}'. Last error: {last_err}") from last_err

def get_driver_class(driver: str) -> type["Instrument"]:
    """Resolve a configured driver string to an Instrument subclass.

    Supports:
    - fully qualified class paths, e.g. ``pydaq.instruments.fidas.FIDAS``
    - short aliases, e.g. ``fidas`` or ``thermo49i``
    """

    import importlib
    import inspect
    import logging

    logger = logging.getLogger("pydaq.instrument.resolve")

    if not driver or not str(driver).strip():
        raise ImportError("Driver is empty.")

    driver = str(driver).strip()

    aliases = {
        "49i": "pydaq.instruments.thermo.Thermo49i",
        "thermo49i": "pydaq.instruments.thermo.Thermo49i",
        "49c": "pydaq.instruments.thermo.Thermo49C",
        "thermo49c": "pydaq.instruments.thermo.Thermo49C",
        "49cps": "pydaq.instruments.thermo.Thermo49CPS",
        "thermo49cps": "pydaq.instruments.thermo.Thermo49CPS",
        "fidas": "pydaq.instruments.fidas.FIDAS",
    }

    def camelize(text: str) -> str:
        return "".join(part.capitalize() for part in text.replace("-", "_").split("_") if part)

    def iter_candidates(name: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def add(value: str | None) -> None:
            if value and value not in seen:
                seen.add(value)
                candidates.append(value)

        lname = name.lower()

        add(aliases.get(lname))

        # fully qualified module.class given directly
        if "." in name:
            add(name)

        # heuristic fallbacks for short names
        add(f"pydaq.instruments.{lname}.{camelize(name)}")
        add(f"pydaq.instruments.{lname}.{name.upper()}")
        add(f"pydaq.instruments.{lname}.{name}")

        return candidates

    last_err: Exception | None = None
    candidates = iter_candidates(driver)

    logger.debug("Resolving driver %r with candidates=%s", driver, candidates)

    for cand in candidates:
        try:
            if "." not in cand:
                raise ImportError(f"Invalid driver candidate without class name: {cand!r}")

            mod_name, attr = cand.rsplit(".", 1)
            module = importlib.import_module(mod_name)
            cls = getattr(module, attr)

            if not inspect.isclass(cls):
                raise TypeError(f"{cand!r} does not resolve to a class")

            if not issubclass(cls, Instrument):
                raise TypeError(f"{cand!r} is not a subclass of Instrument")

            logger.info("Resolved driver %r -> %s.%s", driver, mod_name, attr)
            return cls

        except Exception as exc:
            last_err = exc
            logger.debug("Driver candidate failed: %s (%s)", cand, exc)

    raise ImportError(
        f"Could not resolve driver {driver!r}. Tried: {candidates}. Last error: {last_err}"
    ) from last_err