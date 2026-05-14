from __future__ import annotations

"""iQAir AirVisual Outdoor (AVO) HTTP downloader for pydaq.

The AirVisual Outdoor API returns batches of historical values rather than a
single instrument sample.  Therefore this driver intentionally does not use the
base class CSV writer.  Each scheduled acquisition downloads one or more AVO
URLs, appends/deduplicates the returned historical data sets into Parquet files,
and copies the updated files to the instrument outbox so pydaq's normal transfer
machinery can upload them.

Supported YAML shapes::

    instruments:
      avo:
        enabled: true
        driver: avo
        io:
          kind: http
          urls:
            nairobi: https://device.iqair.com/v2/...
            bomet:
              url: https://device.iqair.com/v2/...
              validated: false
          timeout_seconds: 30
          retries: 3
          backoff_seconds: 2
        schedule:
          sample_every_seconds: 21600   # 6 hours
          transmit_every_seconds: 3600
        output:
          remote_path: avo
          data_path: avo
          staging_path: avo
          remove_on_success: true
        processing:
          datasets: [instant, hourly, daily, monthly]
          append: true
          remove_duplicates: true
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

try:
    import polars as pl
except Exception:  # pragma: no cover - tested in deployment environment
    pl = None  # type: ignore[assignment]

try:
    import requests
except Exception:  # pragma: no cover - tested in deployment environment
    requests = None  # type: ignore[assignment]

from pydaq.instruments.instrument import Instrument


DEFAULT_DATASETS: tuple[str, ...] = ("instant", "hourly", "daily", "monthly")
NUMERIC_DTYPES = {
    "Int8",
    "Int16",
    "Int32",
    "Int64",
    "UInt8",
    "UInt16",
    "UInt32",
    "UInt64",
    "Float64",
}


@dataclass(frozen=True)
class AVOSource:
    """One configured AirVisual Outdoor API endpoint."""

    name: str
    url: str
    validated: bool = False


class AVO(Instrument):
    """pydaq driver for iQAir AirVisual Outdoor API downloads.

    The driver is batch-oriented: one scheduled ``append_record`` call may write
    several Parquet files, for example one file per source and historical data
    set (``instant``, ``hourly``, ``daily``, ``monthly``).  The files are staged
    immediately into pydaq's outbox and then transferred by the common transfer
    scanner.
    """

    def __init__(
        self,
        name: str,
        data_dir: Path,
        outbox_dir: Path,
        logger,
        *,
        headers: Optional[list[str]] = None,
        output_format: str = "parquet",
        writer_config=None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Intentionally disable the base CSV writer.  AVO writes batch Parquet
        # files itself because each HTTP call returns many rows and data sets.
        super().__init__(
            name,
            data_dir,
            outbox_dir,
            logger,
            headers=None,
            output_format=output_format,
            writer_config=writer_config,
            parameters=parameters,
        )
        self._initialized = False
        self.sources: list[AVOSource] = []
        self.datasets: tuple[str, ...] = DEFAULT_DATASETS
        self.timeout_seconds = 30.0
        self.retries = 2
        self.backoff_seconds = 2.0
        self.verify_tls = True
        self.append_existing = True
        self.remove_duplicates = True
        self.stage_files = True
        self.data_path = self.data_dir / self.name
        self.staging_path = self.outbox_dir / self.name
        self.user_agent = "pydaq-avo/1.0"

    def initialize(self) -> None:
        """Normalize configuration and ensure local folders exist."""
        if pl is None:
            raise RuntimeError("polars is required for the AVO driver.")
        if requests is None:
            raise RuntimeError("requests is required for the AVO driver.")

        params = self._params()
        io_cfg = self._optional_mapping(params, "io")
        output_cfg = self._optional_mapping(params, "output")
        processing_cfg = self._optional_mapping(params, "processing")

        self.sources = self._normalize_sources(params=params, io_cfg=io_cfg)
        if not self.sources:
            raise ValueError(f"[{self.name}] AVO requires at least one URL under io.urls or urls.")

        raw_datasets = processing_cfg.get("datasets", params.get("datasets", DEFAULT_DATASETS))
        self.datasets = tuple(str(item).strip() for item in raw_datasets if str(item).strip())
        if not self.datasets:
            raise ValueError(f"[{self.name}] AVO processing.datasets cannot be empty.")

        self.timeout_seconds = float(io_cfg.get("timeout_seconds", io_cfg.get("timeout", 30.0)))
        self.retries = int(io_cfg.get("retries", 2))
        self.backoff_seconds = float(io_cfg.get("backoff_seconds", 2.0))
        self.verify_tls = self._as_bool(io_cfg.get("verify", io_cfg.get("verify_tls", True)), default=True)
        self.user_agent = str(io_cfg.get("user_agent", self.user_agent))

        self.append_existing = self._as_bool(processing_cfg.get("append", True), default=True)
        self.remove_duplicates = self._as_bool(processing_cfg.get("remove_duplicates", True), default=True)
        self.stage_files = self._as_bool(output_cfg.get("stage", True), default=True)

        self.data_path = self._resolve_path(
            base=self.data_dir,
            configured=output_cfg.get("data_path", params.get("data_path", self.name)),
        )
        self.staging_path = self._resolve_path(
            base=self.outbox_dir,
            configured=output_cfg.get("staging_path", params.get("staging_path", self.name)),
        )

        self.data_path.mkdir(parents=True, exist_ok=True)
        self.staging_path.mkdir(parents=True, exist_ok=True)
        self._initialized = True

        self.logger.info(
            "[%s] initialized AVO downloader sources=%s datasets=%s data_path=%s staging_path=%s",
            self.name,
            ",".join(source.name for source in self.sources),
            ",".join(self.datasets),
            self.data_path,
            self.staging_path,
        )

    def get_record(self) -> Dict[str, Any]:
        """Run one download cycle and return a summary record.

        Returns:
            Summary mapping for dashboard/state use.  The actual downloaded data
            are stored as Parquet files by :meth:`append_record` / this method.
        """
        return self._download_cycle()

    def append_record(self) -> None:
        """Download, store, stage, and update instrument state.

        This overrides the base implementation because AVO returns batches and
        writes Parquet files directly instead of appending a single CSV row.
        """
        with self._state_lock:
            if not self.state.enabled:
                return

        record = self.get_record()
        if not record or int(record.get("sources_ok", 0)) == 0:
            with self._state_lock:
                self._consecutive_empty_records += 1
                count = self._consecutive_empty_records
                last_error = self.state.last_error
            if count == 1 or (count % 10) == 0:
                self.logger.error(
                    "no AVO data downloaded consecutive=%s%s",
                    count,
                    f" last_error={last_error}" if last_error else "",
                )
            return

        with self._state_lock:
            previous_empty = self._consecutive_empty_records
            self._consecutive_empty_records = 0
            self.state.latest = record
            self.state.last_sample_ts = time.time()
            self.state.last_error = ""

        if previous_empty:
            self.logger.info("recovered after %s empty AVO download cycle(s)", previous_empty)

    def rollover(self) -> None:
        """No-op; AVO files are staged immediately after every successful download."""
        return

    def _download_cycle(self) -> Dict[str, Any]:
        if not self._initialized:
            self.initialize()

        now = datetime.now(timezone.utc).replace(microsecond=0)
        files_written: list[str] = []
        sources_ok = 0
        rows_written = 0
        errors: list[str] = []

        for source in self.sources:
            try:
                payload = self._download_data(source)
                written, rows = self._store_payload(source=source, payload=payload, now=now)
                files_written.extend(str(path) for path in written)
                rows_written += rows
                sources_ok += 1
                self.logger.info(
                    "[%s] downloaded AVO source=%s files=%s rows=%s",
                    self.name,
                    source.name,
                    len(written),
                    rows,
                )
            except Exception as exc:
                message = f"source={source.name} url={source.url}: {exc}"
                errors.append(message)
                self.logger.error("[%s] AVO download failed: %s", self.name, message, exc_info=True)

        if errors:
            self._set_last_error("; ".join(errors))

        return {
            "dtm": now.strftime("%Y-%m-%d %H:%M:%S"),
            "sources_total": len(self.sources),
            "sources_ok": sources_ok,
            "sources_failed": len(errors),
            "files_written": len(files_written),
            "rows_written": rows_written,
            "errors": " | ".join(errors),
        }

    def _download_data(self, source: AVOSource) -> Mapping[str, Any]:
        if requests is None:  # pragma: no cover
            raise RuntimeError("requests is required for the AVO driver.")

        url = source.url.rstrip("/")
        if source.validated:
            url = f"{url}/validated_data"

        headers = {"User-Agent": self.user_agent}
        last_error: Exception | None = None
        for attempt in range(max(1, self.retries + 1)):
            try:
                response = requests.get(
                    url,
                    timeout=self.timeout_seconds,
                    verify=self.verify_tls,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, Mapping):
                    raise ValueError(f"AVO response is not a JSON object: {type(data).__name__}")
                return data
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.backoff_seconds * (attempt + 1))
                    continue
                break

        raise RuntimeError(f"failed after {self.retries + 1} attempt(s): {last_error}")

    def _store_payload(
        self,
        *,
        source: AVOSource,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> tuple[list[Path], int]:
        station = self._station_slug(payload, fallback=source.name)
        historical = payload.get("historical")
        if not isinstance(historical, Mapping):
            raise ValueError("AVO payload has no 'historical' mapping.")

        written: list[Path] = []
        rows_total = 0
        for dataset in self.datasets:
            entries = historical.get(dataset, [])
            if entries is None:
                entries = []
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                raise ValueError(f"AVO historical.{dataset} is not a list.")
            if not entries:
                self.logger.warning("[%s] source=%s dataset=%s returned no rows", self.name, source.name, dataset)
                continue

            frame = self._entries_to_frame(entries)
            if frame.is_empty():
                continue
            rows_total += frame.height

            target = self._target_file(station=station, dataset=dataset, now=now)
            frame = self._merge_existing(target=target, incoming=frame)
            frame.write_parquet(target)
            written.append(target)

            if self.stage_files:
                self.staging_path.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, self.staging_path / target.name)

        return written, rows_total

    def _entries_to_frame(self, entries: Sequence[Any]):
        if pl is None:  # pragma: no cover
            raise RuntimeError("polars is required for the AVO driver.")

        rows: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            row = self.flatten_data(entry)
            if "ts" not in row:
                raise ValueError("AVO row has no 'ts' timestamp column.")
            # Parse timestamps before constructing the Polars frame.  Newer
            # Polars versions reject expression-based parsing of ISO strings
            # that contain a timezone unless a format/timezone is specified.
            # Using Python's datetime parser here keeps the driver stable across
            # Polars versions and normalizes all timestamps to naive UTC, which
            # matches the rest of pydaq's file conventions.
            row["dtm"] = self._parse_timestamp_to_utc_naive(row["ts"])
            rows.append(row)

        if not rows:
            return pl.DataFrame()

        frame = pl.DataFrame(rows)

        casts = []
        for column, dtype in frame.schema.items():
            if column in {"ts", "dtm"}:
                continue
            if dtype.__class__.__name__ in NUMERIC_DTYPES or str(dtype) in NUMERIC_DTYPES:
                casts.append(pl.col(column).cast(pl.Float32, strict=False))
        if casts:
            frame = frame.with_columns(casts)

        return frame

    @staticmethod
    def _parse_timestamp_to_utc_naive(value: Any) -> datetime:
        """Parse an AVO timestamp and return a naive UTC datetime.

        AVO API timestamps are usually ISO-8601 strings such as
        ``2026-05-11T00:00:00Z``.  The returned value deliberately has no
        ``tzinfo`` because pydaq stores UTC timestamps as timezone-naive values
        in local files.
        """
        if isinstance(value, datetime):
            dtm = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("AVO timestamp is empty.")
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                dtm = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(f"Invalid AVO timestamp {value!r}.") from exc
        else:
            raise TypeError(f"Invalid AVO timestamp type {type(value).__name__}.")

        if dtm.tzinfo is None:
            dtm = dtm.replace(tzinfo=timezone.utc)
        return dtm.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)

    def _merge_existing(self, *, target: Path, incoming):
        if pl is None:  # pragma: no cover
            raise RuntimeError("polars is required for the AVO driver.")

        frame = incoming
        if self.append_existing and target.exists():
            existing = pl.read_parquet(target)
            frame = pl.concat([existing, incoming], how="diagonal")

        if self.remove_duplicates:
            subset = ["ts"] if "ts" in frame.columns else None
            try:
                frame = frame.unique(subset=subset, maintain_order=True)
            except TypeError:
                frame = frame.unique(maintain_order=True)

        if "dtm" in frame.columns:
            frame = frame.sort("dtm")
        return frame

    def _target_file(self, *, station: str, dataset: str, now: datetime) -> Path:
        suffix = now.strftime("%Y%m") if dataset == "monthly" else now.strftime("%Y%m%d")
        return self.data_path / f"{station}_avo_{dataset}-{suffix}.parquet"

    @staticmethod
    def flatten_data(data: Mapping[str, Any], parent_key: str = "", sep: str = "_") -> dict[str, Any]:
        """Flatten a nested JSON object using underscore-separated keys."""
        items: list[tuple[str, Any]] = []
        for key, value in data.items():
            new_key = f"{parent_key}{sep}{key}" if parent_key else str(key)
            if isinstance(value, Mapping):
                items.extend(AVO.flatten_data(value, new_key, sep=sep).items())
            else:
                items.append((new_key, value))
        return dict(items)

    @staticmethod
    def _station_slug(payload: Mapping[str, Any], *, fallback: str) -> str:
        raw = str(payload.get("name", fallback)).strip() or fallback
        out = []
        last_was_sep = False
        for char in raw.lower():
            if char.isalnum():
                out.append(char)
                last_was_sep = False
            else:
                if not last_was_sep:
                    out.append("_")
                    last_was_sep = True
        return "".join(out).strip("_") or fallback.lower().replace(" ", "_")

    @staticmethod
    def _resolve_path(*, base: Path, configured: Any) -> Path:
        path = Path(str(configured)).expanduser()
        if path.is_absolute():
            return path
        return base / path

    @staticmethod
    def _as_bool(value: Any, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "y"}
        return bool(value)

    def _params(self) -> dict[str, Any]:
        params = getattr(self, "parameters", None)
        if not isinstance(params, dict):
            raise ValueError(f"[{self.name}] missing driver parameters; expected a dict.")
        return params

    def _optional_mapping(self, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        value = payload.get(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(f"[{self.name}] invalid '{key}' block; expected a mapping.")
        return dict(value)

    def _normalize_sources(self, *, params: Mapping[str, Any], io_cfg: Mapping[str, Any]) -> list[AVOSource]:
        raw_sources = io_cfg.get("sources", io_cfg.get("urls", params.get("sources", params.get("urls"))))
        global_validated = self._as_bool(io_cfg.get("validated", params.get("validated", False)), default=False)

        if raw_sources is None:
            url = io_cfg.get("url", params.get("url"))
            if url:
                return [AVOSource(name=self.name, url=str(url), validated=global_validated)]
            return []

        sources: list[AVOSource] = []
        if isinstance(raw_sources, Mapping):
            for name, value in raw_sources.items():
                source_name = str(name).replace("url_", "", 1).strip() or "avo"
                if isinstance(value, str):
                    sources.append(AVOSource(name=source_name, url=value, validated=global_validated))
                elif isinstance(value, Mapping):
                    url = value.get("url")
                    if not url:
                        raise ValueError(f"[{self.name}] AVO source {name!r} has no url.")
                    validated = self._as_bool(value.get("validated", global_validated), default=global_validated)
                    sources.append(AVOSource(name=source_name, url=str(url), validated=validated))
                else:
                    raise ValueError(f"[{self.name}] invalid AVO source {name!r}: {value!r}")
            return sources

        if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, (str, bytes)):
            for index, value in enumerate(raw_sources):
                if not isinstance(value, Mapping):
                    raise ValueError(f"[{self.name}] AVO source item {index} is not a mapping.")
                url = value.get("url")
                if not url:
                    raise ValueError(f"[{self.name}] AVO source item {index} has no url.")
                source_name = str(value.get("name", f"source_{index + 1}"))
                validated = self._as_bool(value.get("validated", global_validated), default=global_validated)
                sources.append(AVOSource(name=source_name, url=str(url), validated=validated))
            return sources

        raise ValueError(f"[{self.name}] invalid AVO urls/sources configuration: {raw_sources!r}")

    def _set_last_error(self, message: str) -> None:
        with self._state_lock:
            self.state.last_error = message
