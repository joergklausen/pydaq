"""Hourly CSV writer with rollover and outbox staging.

Desired layout:
- ``data/<instrument>/<YYYY>/<MM>/...`` holds the authoritative local record.
- ``outbox/<instrument>/...`` holds files to be transmitted.
  After successful transmission, files are removed from outbox.
  The data directory remains intact.

The writer rolls over by hour based on the record's datetime field.
"""

from __future__ import annotations

import csv
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydaq.utils.datetime_handler import parse_isoish


@dataclass(frozen=True)
class WriterConfig:
    """Configuration for CSV writing behavior."""

    datetime_field: str = "dtm"
    file_prefix: str = ""
    csv_delimiter: str = ","


class HourlyCsvWriter:
    """Append record dictionaries to a rolling hourly CSV and stage to outbox."""

    def __init__(
        self,
        instrument_name: str,
        data_directory: Path,
        outbox_directory: Path,
        headers: List[str],
        *,
        output_format: str = "csv_zip",
        writer_config: Optional[WriterConfig] = None,
        logger=None,
    ) -> None:
        """Create an hourly CSV writer.

        Args:
            instrument_name: Instrument identifier used for file naming.
            data_directory: Base directory for the authoritative local record. Files are stored under
                ``<data_directory>/<YYYY>/<MM>/...``.
            outbox_directory: Directory for staged files ready for transmission.
                Outbox is kept flat per instrument (no year/month folders).
            headers: CSV header fields, in order.
            output_format: ``csv`` or ``csv_zip`` (default). ``parquet`` is intentionally not implemented here.
            writer_config: Optional writer configuration.
            logger: Optional logger for messages.
        """
        self.instrument_name = instrument_name
        self.data_directory = data_directory
        self.outbox_directory = outbox_directory
        self.headers = headers
        self.output_format = output_format
        self.config = writer_config or WriterConfig()
        self.logger = logger

        self._current_hour_key: str = ""
        self._open_path: Optional[Path] = None
        self._file_handle = None
        self._csv_writer: Optional[csv.DictWriter] = None
        self._rows_written: int = 0

        self.data_directory.mkdir(parents=True, exist_ok=True)
        self.outbox_directory.mkdir(parents=True, exist_ok=True)

    def _as_utc(self, dtm: datetime) -> datetime:
        """Return a timezone-aware datetime in UTC."""
        if dtm.tzinfo is None:
            return dtm.replace(tzinfo=timezone.utc)
        return dtm.astimezone(timezone.utc)

    def _hour_key(self, dtm: datetime) -> str:
        """Compute hour key used for rollover decisions."""
        return dtm.strftime("%Y%m%d%H")

    def _data_path_for_hour(self, dtm: datetime, hour_key: str) -> Path:
        """Return the authoritative data CSV path for a given timestamp/hour."""
        year = dtm.strftime("%Y")
        month = dtm.strftime("%m")
        prefix = (self.config.file_prefix or self.instrument_name).strip()
        directory = self.data_directory / year / month
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{prefix}-{hour_key}.csv"

    def _ensure_open_for_timestamp(self, dtm: datetime) -> None:
        """Ensure the correct hourly CSV file is open for ``dtm``."""
        hour_key = self._hour_key(dtm)
        if hour_key == self._current_hour_key and self._file_handle:
            return

        # Rollover previous hour (if any)
        if self._file_handle and self._open_path:
            self._close_and_stage()

        self._current_hour_key = hour_key
        self._open_path = self._data_path_for_hour(dtm, hour_key)
        is_new = not self._open_path.exists()

        self._file_handle = self._open_path.open("a", encoding="utf-8", newline="")
        self._csv_writer = csv.DictWriter(
            self._file_handle,
            fieldnames=self.headers,
            delimiter=self.config.csv_delimiter,
        )
        self._rows_written = 0

        if is_new:
            self._csv_writer.writeheader()
            self._file_handle.flush()

    def append(self, record: Dict[str, Any]) -> None:
        """Append one record dictionary.

        Args:
            record: Field mapping. Must contain the configured ``datetime_field`` (default ``dtm``).

        Notes:
            If the datetime cannot be parsed, the record is skipped.
        """
        dt_value = record.get(self.config.datetime_field)
        if isinstance(dt_value, datetime):
            dtm = dt_value
        elif isinstance(dt_value, str):
            parsed = parse_isoish(dt_value)
            if parsed is None:
                return
            dtm = parsed
        else:
            return

        dtm = self._as_utc(dtm)
        self._ensure_open_for_timestamp(dtm)

        assert self._csv_writer is not None
        row = {h: record.get(h) for h in self.headers}
        self._csv_writer.writerow(row)
        assert self._file_handle is not None
        self._file_handle.flush()
        self._rows_written += 1

    def finalize_if_needed(self, now: Optional[datetime] = None) -> None:
        """Finalize and stage the current file if the hour has advanced.

        Args:
            now: Optional current timestamp. If omitted, uses current UTC time.
        """
        if not self._file_handle or not self._open_path or not self._current_hour_key:
            return
        now_dt = self._as_utc(now or datetime.now(timezone.utc))
        now_key = self._hour_key(now_dt)
        if self._current_hour_key >= now_key:
            return
        self._close_and_stage()

    def stage_current(self) -> None:
        """Force staging of the currently open file (if any)."""
        if not self._file_handle or not self._open_path:
            return
        self._close_and_stage()

    def _close_and_stage(self) -> None:
        """Close the rolling file handle and stage a payload into the outbox.

        The authoritative data CSV stays in ``data/<instrument>/<YYYY>/<MM>/...``.

        Empty files (no data rows) are deleted and **not** staged.
        """
        try:
            if self._file_handle:
                self._file_handle.close()
        finally:
            self._file_handle = None
            self._csv_writer = None

        assert self._open_path is not None
        source_path = self._open_path

        # Reset current-file state early to avoid double staging.
        self._open_path = None
        self._current_hour_key = ""

        # Delete header-only / empty files (no data rows).
        if self._rows_written <= 0:
            try:
                source_path.unlink(missing_ok=True)
            except Exception:
                pass
            return

        # Stage to outbox while keeping the data file in place.
        self.outbox_directory.mkdir(parents=True, exist_ok=True)

        if self.output_format == "csv":
            staged = self.outbox_directory / source_path.name
            shutil.copy2(source_path, staged)
            return

        if self.output_format == "csv_zip":
            staged_zip = self.outbox_directory / source_path.with_suffix(".zip").name
            if staged_zip.exists():
                staged_zip.unlink(missing_ok=True)
            with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.write(source_path, arcname=source_path.name)
            return

        if self.output_format == "parquet":
            raise NotImplementedError("parquet staging is not implemented in this minimal writer")

        # Fallback: keep data only.
        return
