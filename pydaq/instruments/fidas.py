"""pydaq.instruments.fidas

PALAS FIDAS aerosol instrument driver for pydaq.

This driver refactors the legacy ``mkndaq.inst.fidas.FIDAS`` implementation so it fits
pydaq's worker-thread and writer-based architecture.

Behavior
--------
- Listen for UDP datagrams emitted by the FIDAS instrument.
- Parse ``<sendVal ...>`` payloads into channel/value mappings.
- Buffer high-frequency raw samples in memory.
- Emit median aggregates on the configured aggregation cadence.
- Hand aggregate rows to the shared pydaq writer for hourly rollover and outbox staging.

Notes
-----
- The driver uses its own UDP socket instead of :class:`LineComms` because FIDAS streams
  datagrams and does not follow a line-oriented request/response pattern.
- The orchestrator should pass the instrument ``schedule`` section into
  ``parameters["schedule"]``. Without that, the driver cannot see
  ``aggregation_period_minutes``.
- In station YAML, prefer ``io.kind: udp`` for FIDAS. Using ``socket`` makes the network
  monitor treat it as TCP.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import socket
from typing import Any, Dict, Iterable, List, Optional

import polars as pl

from pydaq.instruments.instrument import Instrument, utc_timestamp_string


class FIDAS(Instrument):
    """UDP-based FIDAS driver with in-memory median aggregation."""

    DEFAULT_PRINT_KEYS: tuple[str, ...] = ("60", "61", "62", "63", "64", "65")

    DEFAULT_HEADERS: List[str] = (
        ["dtm"]
        + [str(i) for i in range(0, 66)]
        + [str(i) for i in range(110, 201)]
    )

    # The orchestrator looks for HEADERS on the class when constructing the writer.
    HEADERS: List[str] = list(DEFAULT_HEADERS)

    def __init__(
        self,
        name: str,
        data_dir,
        outbox_dir,
        logger,
        *,
        headers: Optional[List[str]] = None,
        output_format: str = "csv_zip",
        writer_config=None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        params = parameters or {}
        output_cfg = self._params_section(params, "output")
        effective_output_format = str(output_cfg.get("format", output_format)).strip().lower() or "csv_zip"
        effective_headers = list(headers) if headers else list(self.DEFAULT_HEADERS)

        super().__init__(
            name=name,
            data_dir=data_dir,
            outbox_dir=outbox_dir,
            logger=logger,
            headers=effective_headers,
            output_format=effective_output_format,
            writer_config=writer_config,
            parameters=params,
        )

        io_cfg = self._params_section(self.parameters, "io")
        schedule_cfg = self._params_section(self.parameters, "schedule")

        self.host = str(io_cfg.get("host", "0.0.0.0"))
        self.port = int(io_cfg.get("port", 56790))
        self.buffer_size = int(io_cfg.get("buffer_size", 8192))
        self.timeout_seconds = float(io_cfg.get("timeout_seconds", io_cfg.get("timeout", 0.5)))

        self.aggregation_period_minutes = max(1, int(schedule_cfg.get("aggregation_period_minutes", 1)))

        self._sock: Optional[socket.socket] = None
        self._raw_records: List[Dict[str, float]] = []
        self._last_parsed: Dict[str, Any] = {}
        self._current_window_start: Optional[datetime] = None

    @staticmethod
    def _params_section(parameters: Dict[str, Any], key: str) -> Dict[str, Any]:
        value = parameters.get(key, {}) if isinstance(parameters, dict) else {}
        return value if isinstance(value, dict) else {}

    def initialize(self) -> None:
        """Open the UDP listener socket (idempotent)."""
        if self._sock is not None:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.host, self.port))
        sock.settimeout(self.timeout_seconds)
        self._sock = sock
        self.logger.info("listening udp=%s:%s", self.host, self.port)

    def stop(self) -> None:
        """Stop the worker thread and close the UDP socket."""
        super().stop()
        self.close()

    def close(self) -> None:
        """Close the UDP listener socket (best-effort)."""
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        finally:
            self._sock = None

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _floor_window_start(self, timestamp: datetime) -> datetime:
        minute = timestamp.minute - (timestamp.minute % self.aggregation_period_minutes)
        return timestamp.replace(minute=minute, second=0, microsecond=0)

    def _window_delta(self) -> timedelta:
        return timedelta(minutes=self.aggregation_period_minutes)

    def receive_udp_record(self) -> str:
        """Receive one UDP datagram and decode it as ASCII."""
        if self._sock is None:
            self.initialize()
        if self._sock is None:
            return ""

        try:
            data, _addr = self._sock.recvfrom(self.buffer_size)
        except socket.timeout:
            return ""
        except OSError as exc:
            self.logger.warning("udp receive failed: %s", exc)
            self.close()
            return ""

        return data.decode("ascii", errors="ignore").strip()

    @staticmethod
    def parse_record(record: str) -> Dict[str, Any]:
        """Parse one raw FIDAS record like ``6082<sendVal 60=1.0;61=2.0>3E``."""
        record = record.strip()
        if not record or "<" not in record or ">" not in record:
            return {}

        try:
            record_id, rest = record.split("<", 1)
            payload, checksum = rest.split(">", 1)
        except ValueError:
            return {}

        parsed: Dict[str, Any] = {
            "record_id": int(record_id.strip()) if record_id.strip().isdigit() else record_id.strip(),
            "checksum": checksum.strip(),
        }

        payload = payload.strip()
        if payload.startswith("sendVal"):
            payload = payload[len("sendVal") :].strip()

        for pair in payload.split(";"):
            if "=" not in pair:
                continue
            raw_key, raw_value = pair.split("=", 1)
            try:
                key = str(int(raw_key.strip()))
                value = float(raw_value.strip())
            except Exception:
                continue
            parsed[key] = value

        return parsed

    def get_record(self) -> Dict[str, Any]:
        raw = self.receive_udp_record()
        if not raw:
            return {}

        parsed = self.parse_record(raw)
        if not parsed:
            self.logger.debug("unparsed udp record=%s", raw[:160])
            return {}

        self._last_parsed = parsed
        return parsed

    def _extract_numeric_channels(self, record: Dict[str, Any]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for key, value in record.items():
            if not key.isdigit():
                continue
            if isinstance(value, (int, float)):
                out[key] = float(value)
        return out

    def _build_aggregate_record(self, window_start: datetime) -> Dict[str, Any]:
        if not self._raw_records:
            return {}

        frame = pl.DataFrame(self._raw_records)
        value_cols = [
            column
            for column, dtype in frame.schema.items()
            if dtype in {pl.Float32, pl.Float64, pl.Int32, pl.Int64}
        ]
        if not value_cols:
            self._raw_records.clear()
            return {}

        median_row = frame.select([pl.col(column).median().alias(column) for column in value_cols])
        row = median_row.to_dicts()[0]
        row["dtm"] = utc_timestamp_string(window_start)
        self._raw_records.clear()
        return row

    def _log_aggregate_summary(self, row: Dict[str, Any]) -> None:
        summary = {
            key: row[key]
            for key in self.DEFAULT_PRINT_KEYS
            if key in row and row[key] is not None
        }
        if summary:
            self.logger.info("aggregate %s", summary)

    def _emit_aggregate_if_due(self, now: Optional[datetime] = None, *, force: bool = False) -> None:
        current_time = now or self._now_utc()
        current_slot = self._floor_window_start(current_time)

        if self._current_window_start is None:
            self._current_window_start = current_slot

        if force:
            row = self._build_aggregate_record(self._current_window_start)
            if row and self.writer:
                self.writer.append(row)
                self.writer.finalize_if_needed()
                with self._state_lock:
                    self.state.latest = row
                self._log_aggregate_summary(row)
            return

        delta = self._window_delta()
        while self._current_window_start < current_slot:
            row = self._build_aggregate_record(self._current_window_start)
            if row and self.writer:
                self.writer.append(row)
                self.writer.finalize_if_needed()
                with self._state_lock:
                    self.state.latest = row
                self._log_aggregate_summary(row)
            self._current_window_start += delta

    def append_record(self) -> None:
        """Collect one raw sample and write only aggregated rows."""
        with self._state_lock:
            if not self.state.enabled:
                return

        parsed = self.get_record()
        now = self._now_utc()

        if parsed:
            numeric = self._extract_numeric_channels(parsed)
            if numeric:
                self._raw_records.append(numeric)

            with self._state_lock:
                # Expose latest raw reading until an aggregate is emitted.
                self.state.latest = parsed
                self.state.last_sample_ts = __import__("time").time()

        self._emit_aggregate_if_due(now)

    def rollover(self) -> None:
        """Flush any buffered aggregate and then stage the current writer file."""
        self._emit_aggregate_if_due(force=True)
        super().rollover()

    def print_readings(self, keys: Optional[Iterable[str]] = None) -> None:
        if not self._last_parsed:
            self.logger.info("no valid data retrieved")
            return

        keys = tuple(keys) if keys is not None else self.DEFAULT_PRINT_KEYS
        parts: List[str] = []
        for key in keys:
            value = self._last_parsed.get(str(key))
            if isinstance(value, (int, float)):
                parts.append(f"{key}={value:.3f}")

        if parts:
            self.logger.info("%s", "; ".join(parts))

    # Compatibility helpers during migration from mkndaq
    def collect_raw_record(self) -> Dict[str, Any]:
        return self.get_record()

    def compute_raw_data_median(self) -> Dict[str, Any]:
        if self._current_window_start is None:
            self._current_window_start = self._floor_window_start(self._now_utc())
        row = self._build_aggregate_record(self._current_window_start)
        if row:
            self._log_aggregate_summary(row)
        return row

    def save_hourly(self, stage: bool = True) -> None:
        _ = stage
        self.rollover()
