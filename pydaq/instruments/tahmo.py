from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZipFile

from .instrument import Instrument


DEFAULT_SERVER = "https://tahmo.zentracloud.com"
DEFAULT_API_VERSION = "v4"
DEFAULT_TOKEN_FILE = "~/.secrets/zentra-api-token"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PER_PAGE = 2000

_METADATA_FIELDS = {
    "device_sn",
    "device_name",
    "port_number",
    "sensor_sn",
    "sensor_name",
    "units",
    "errors",
    "sensor_depth",
    "depth",
}
_READING_FIELDS = {
    "timestamp_utc",
    "datetime",
    "tz_offset",
    "value",
    "precision",
    "mrid",
    "error_flag",
    "error_description",
}
_CSV_COLUMNS = (
    "measurement",
    "device_sn",
    "device_name",
    "port_number",
    "sensor_sn",
    "sensor_name",
    "units",
    "sensor_depth",
    "timestamp_utc",
    "datetime",
    "tz_offset",
    "value",
    "precision",
    "mrid",
    "error_flag",
    "error_description",
    "metadata_errors",
    "metadata_extra_json",
    "reading_extra_json",
)


@dataclass(frozen=True)
class TahmoConfig:
    """Configuration needed for one TAHMO/ZENTRA Cloud device."""

    device_sn: str
    server: str = DEFAULT_SERVER
    api_version: str = DEFAULT_API_VERSION
    token_file: Path = Path(DEFAULT_TOKEN_FILE).expanduser()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    per_page: int = DEFAULT_PER_PAGE
    max_pages: int = 500
    request_delay_seconds: float = 65.0
    initial_start_date: str | None = None
    initial_lookback_hours: float = 12.0

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, Any]) -> "TahmoConfig":
        """Build driver configuration from the pydaq instrument parameter mapping."""
        io_cfg = _mapping(parameters.get("io"))
        device_sn = str(io_cfg.get("device_sn", "")).strip()
        if not device_sn:
            raise ValueError("TAHMO io.device_sn is required")

        server = str(io_cfg.get("server", DEFAULT_SERVER)).strip().rstrip("/")
        api_version = str(io_cfg.get("api_version", DEFAULT_API_VERSION)).strip().strip("/")
        token_file = Path(
            str(io_cfg.get("token_file", DEFAULT_TOKEN_FILE))
        ).expanduser()
        timeout_seconds = float(
            io_cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )
        per_page = int(io_cfg.get("per_page", DEFAULT_PER_PAGE))
        if not 1 <= per_page <= 2000:
            raise ValueError("TAHMO io.per_page must be between 1 and 2000")

        initial_start_date_raw = io_cfg.get("initial_start_date")
        initial_start_date = (
            str(initial_start_date_raw).strip()
            if initial_start_date_raw not in (None, "")
            else None
        )
        return cls(
            device_sn=device_sn,
            server=server,
            api_version=api_version,
            token_file=token_file,
            timeout_seconds=timeout_seconds,
            per_page=per_page,
            max_pages=int(io_cfg.get("max_pages", 500)),
            request_delay_seconds=float(io_cfg.get("request_delay_seconds", 65.0)),
            initial_start_date=initial_start_date,
            initial_lookback_hours=float(io_cfg.get("initial_lookback_hours", 12.0)),
        )


@dataclass(frozen=True)
class ReadingBatch:
    """One API response represented as a flat, self-describing row set."""

    rows: tuple[dict[str, Any], ...]
    pagination: Mapping[str, Any] | None = None

    @property
    def last_mrid(self) -> int | None:
        mrids = [
            int(row["mrid"])
            for row in self.rows
            if row.get("mrid") not in (None, "")
        ]
        return max(mrids) if mrids else None

    @property
    def first_timestamp_utc(self) -> int | None:
        values = [
            int(float(row["timestamp_utc"]))
            for row in self.rows
            if row.get("timestamp_utc") not in (None, "")
        ]
        return min(values) if values else None

    @property
    def last_timestamp_utc(self) -> int | None:
        values = [
            int(float(row["timestamp_utc"]))
            for row in self.rows
            if row.get("timestamp_utc") not in (None, "")
        ]
        return max(values) if values else None


class ZentraV4Client:
    """Minimal ZENTRA Cloud v4 client for the TAHMO server."""

    def __init__(
        self,
        *,
        server: str = DEFAULT_SERVER,
        api_version: str = DEFAULT_API_VERSION,
        token_file: Path = Path(DEFAULT_TOKEN_FILE).expanduser(),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.server = server.rstrip("/")
        self.api_version = api_version.strip("/")
        self.token_file = Path(token_file).expanduser()
        self.timeout_seconds = float(timeout_seconds)

    @property
    def readings_url(self) -> str:
        return f"{self.server}/api/{self.api_version}/get_readings/"

    def fetch_readings(
        self,
        *,
        device_sn: str,
        start_date: str | None = None,
        end_date: str | None = None,
        start_mrid: int | None = None,
        end_mrid: int | None = None,
        per_page: int = DEFAULT_PER_PAGE,
        page_num: int = 1,
    ) -> ReadingBatch:
        """Fetch one ascending page of readings.

        Exactly one API call is made. The caller handles page_num pagination.
        """
        if not 1 <= per_page <= 2000:
            raise ValueError("per_page must be between 1 and 2000")
        if start_date is not None and start_mrid is not None:
            raise ValueError("Specify start_date or start_mrid, not both")
        if end_date is not None and end_mrid is not None:
            raise ValueError("Specify end_date or end_mrid, not both")

        params: dict[str, Any] = {
            "device_sn": device_sn,
            "output_format": "json",
            "page_num": page_num,
            "per_page": per_page,
            "sort_by": "ascending",
            "device_depth": "true",
        }
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        if start_mrid is not None:
            params["start_mrid"] = int(start_mrid)
        if end_mrid is not None:
            params["end_mrid"] = int(end_mrid)

        payload = self._request_json(params)
        pagination = payload.get("pagination")
        if not isinstance(pagination, Mapping):
            raise RuntimeError("TAHMO response has no pagination metadata")
        return ReadingBatch(tuple(flatten_readings(payload)), pagination)

    def _request_json(self, params: Mapping[str, Any]) -> dict[str, Any]:
        token = self._read_token()
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self.readings_url}?{query}",
            headers={
                "Authorization": _authorization_value(token),
                "Accept": "application/json",
                "User-Agent": "pydaq-tahmo/1",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            suffix = f": {detail[:300]}" if detail else ""
            raise RuntimeError(f"ZENTRA Cloud HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"ZENTRA Cloud connection failed: {exc.reason}") from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ZENTRA Cloud returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("ZENTRA Cloud returned an unexpected response")
        return payload

    def _read_token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"Could not read ZENTRA API token file {self.token_file}"
            ) from exc
        if not token:
            raise RuntimeError(f"ZENTRA API token file is empty: {self.token_file}")
        return token


class TahmoDownloader:
    """Stateful downloader that archives and stages complete ZIP files."""

    def __init__(
        self,
        *,
        name: str,
        data_dir: Path,
        outbox_dir: Path,
        config: TahmoConfig,
        logger: logging.Logger,
        client: ZentraV4Client | None = None,
    ) -> None:
        self.name = name
        self.data_root = _instrument_root(Path(data_dir), name)
        self.outbox_root = _instrument_root(Path(outbox_dir), name)
        self.config = config
        self.logger = logger
        self.client = client or ZentraV4Client(
            server=config.server,
            api_version=config.api_version,
            token_file=config.token_file,
            timeout_seconds=config.timeout_seconds,
        )
        self.state_path = self.data_root / ".zentra_state.json"

    def fetch_once(self) -> Path | None:
        """Fetch every page for the current cursor before committing progress.

        Page 1 of the observed TAHMO response contains the NEWEST MRIDs, so
        committing a cursor after page 1 could silently discard older pages.
        We therefore gather all pages before writing any cursor. If pagination
        is interrupted, the next scheduled run repeats the entire selection.
        """
        state = self._load_state()
        device_state = _mapping(_mapping(state.get("devices")).get(self.config.device_sn))
        raw = device_state.get("last_mrid")
        last_mrid = int(raw) if raw is not None else None
        args: dict[str, Any] = {
            "device_sn": self.config.device_sn,
            "per_page": self.config.per_page,
        }
        if last_mrid is not None:
            args["start_mrid"] = last_mrid + 1
        elif self.config.initial_start_date is not None:
            args["start_date"] = self.config.initial_start_date
        else:
            if self.config.initial_lookback_hours <= 0:
                raise ValueError("TAHMO initial_lookback_hours must be positive")
            args["start_date"] = (datetime.now(UTC) - timedelta(
                hours=self.config.initial_lookback_hours
            )).strftime("%Y-%m-%d %H:%M:%S")
            self.logger.warning(
                "[%s] no saved TAHMO cursor; starting with a %.1f h lookback; older data will not be fetched automatically",
                self.name, self.config.initial_lookback_hours,
            )

        if self.config.max_pages < 1:
            raise ValueError("TAHMO max_pages must be >= 1")
        if self.config.request_delay_seconds < 0:
            raise ValueError("TAHMO request_delay_seconds must be >= 0")
        rows: list[dict[str, Any]] = []
        try:
            for page_num in range(1, self.config.max_pages + 1):
                batch = self.client.fetch_readings(**args, page_num=page_num)
                info = batch.pagination
                if info is None:
                    raise RuntimeError("TAHMO pagination metadata missing")
                rows.extend(batch.rows)
                total = info.get("page_num_readings")
                has_next = bool(info.get("next_url"))
                if total is not None and int(total) == 0:
                    has_next = False
                if not has_next:
                    break
                if page_num == self.config.max_pages:
                    raise RuntimeError(
                        f"TAHMO pagination exceeded max_pages={self.config.max_pages}; "
                        "cursor unchanged"
                    )
                time.sleep(self.config.request_delay_seconds)
        except Exception as exc:
            self.logger.warning(
                "[%s] TAHMO retrieval incomplete, cursor unchanged: %s",
                self.name, _one_line_error(exc),
            )
            return None

        if last_mrid is not None:
            rows = [row for row in rows if int(row["mrid"]) > last_mrid]
        if not rows:
            self.logger.info("[%s] TAHMO: no new data", self.name)
            return None
        # Overlapping pages are harmless but should not produce duplicate output.
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            identity = (
                row["mrid"], row["measurement"], row["port_number"],
                row["sensor_sn"], row["sensor_depth"],
            )
            unique[identity] = row
        batch = ReadingBatch(tuple(sorted(
            unique.values(),
            key=lambda r: (int(r["mrid"]), str(r["measurement"]), str(r["port_number"])),
        )))
        new_last_mrid = batch.last_mrid
        if new_last_mrid is None:
            raise RuntimeError("TAHMO readings contain no MRID")
        archive_path = self._archive_path(batch)
        stage_path = self.outbox_root / archive_path.name
        try:
            write_batch_zip(batch=batch, path=archive_path)
            _atomic_copy(archive_path, stage_path)
            self._save_cursor(
                state=state, last_mrid=new_last_mrid,
                last_timestamp_utc=batch.last_timestamp_utc,
            )
        except Exception as exc:
            self.logger.warning(
                "[%s] TAHMO local staging failed; cursor unchanged: %s",
                self.name, _one_line_error(exc),
            )
            return None
        self.logger.info(
            "[%s] TAHMO archived/staged rows=%d mrid=%s..%s file=%s",
            self.name, len(batch.rows), min(int(r["mrid"]) for r in batch.rows),
            new_last_mrid, archive_path.name,
        )
        return archive_path

    def _archive_path(self, batch: ReadingBatch) -> Path:
        first_ts = batch.first_timestamp_utc
        if first_ts is None:
            partition = datetime.now(UTC)
        else:
            partition = datetime.fromtimestamp(first_ts, tz=UTC)

        first_mrid = min(
            int(row["mrid"])
            for row in batch.rows
            if row.get("mrid") not in (None, "")
        )
        last_mrid = batch.last_mrid
        safe_sn = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.config.device_sn)
        filename = f"{self.name}-{safe_sn}-mrid{first_mrid}-{last_mrid}.zip"
        return (
            self.data_root
            / f"{partition.year:04d}"
            / f"{partition.month:02d}"
            / f"{partition.day:02d}"
            / filename
        )

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "devices": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not read TAHMO state file {self.state_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Invalid TAHMO state file {self.state_path}")
        payload.setdefault("version", 1)
        payload.setdefault("devices", {})
        return payload

    def _save_cursor(
        self,
        *,
        state: dict[str, Any],
        last_mrid: int,
        last_timestamp_utc: int | None,
    ) -> None:
        devices = state.setdefault("devices", {})
        if not isinstance(devices, dict):
            devices = {}
            state["devices"] = devices
        devices[self.config.device_sn] = {
            "last_mrid": int(last_mrid),
            "last_timestamp_utc": last_timestamp_utc,
            "updated_utc": datetime.now(UTC).isoformat(),
        }
        _atomic_write_text(
            self.state_path,
            json.dumps(state, indent=2, sort_keys=True) + "\n",
        )


class TAHMO(Instrument):
    """pydaq file-source driver for a TAHMO/ZENTRA Cloud station."""

    headers = None

    def request_reading(self) -> None:
        """Queue the cloud download instead of the normal CSV record path."""
        self._task_queue.put(self._fetch_new_data)

    def _fetch_new_data(self) -> None:
        downloader = getattr(self, "_tahmo_downloader", None)
        if downloader is None:
            parameters = _mapping(getattr(self, "parameters", {}))
            downloader = TahmoDownloader(
                name=self.name,
                data_dir=Path(self.data_dir),
                outbox_dir=Path(self.outbox_dir),
                config=TahmoConfig.from_parameters(parameters),
                logger=self.logger,
            )
            self._tahmo_downloader = downloader
        downloader.fetch_once()


def flatten_readings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten a ZENTRA v4 JSON response to one row per measurement reading."""
    if "data" not in payload:
        raise RuntimeError("TAHMO response missing data")
    data: Any = payload["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ZENTRA Cloud data field is invalid JSON") from exc
    if not isinstance(data, Mapping):
        raise RuntimeError("TAHMO response data is not a mapping")

    rows: list[dict[str, Any]] = []
    for measurement, sensor_entries in data.items():
        if measurement == "location_history" or not isinstance(sensor_entries, Sequence):
            continue
        if isinstance(sensor_entries, (str, bytes, bytearray)):
            continue

        for sensor_entry in sensor_entries:
            if not isinstance(sensor_entry, Mapping):
                continue
            metadata = _mapping(sensor_entry.get("metadata"))
            readings = sensor_entry.get("readings", ())
            if not isinstance(readings, Sequence) or isinstance(
                readings, (str, bytes, bytearray)
            ):
                continue

            metadata_extra = {
                key: value
                for key, value in metadata.items()
                if key not in _METADATA_FIELDS
            }
            errors = metadata.get("errors")
            metadata_errors = (
                json.dumps(errors, ensure_ascii=False, separators=(",", ":"))
                if errors not in (None, "", [])
                else ""
            )
            sensor_depth = metadata.get("sensor_depth", metadata.get("depth", ""))

            for reading in readings:
                if not isinstance(reading, Mapping):
                    continue
                reading_extra = {
                    key: value
                    for key, value in reading.items()
                    if key not in _READING_FIELDS
                }
                row = {
                    "measurement": measurement,
                    "device_sn": metadata.get("device_sn", ""),
                    "device_name": metadata.get("device_name", ""),
                    "port_number": metadata.get("port_number", ""),
                    "sensor_sn": metadata.get("sensor_sn", ""),
                    "sensor_name": metadata.get("sensor_name", ""),
                    "units": metadata.get("units", ""),
                    "sensor_depth": sensor_depth,
                    "timestamp_utc": reading.get("timestamp_utc", ""),
                    "datetime": reading.get("datetime", ""),
                    "tz_offset": reading.get("tz_offset", ""),
                    "value": reading.get("value", ""),
                    "precision": reading.get("precision", ""),
                    "mrid": reading.get("mrid", ""),
                    "error_flag": reading.get("error_flag", ""),
                    "error_description": reading.get("error_description", ""),
                    "metadata_errors": metadata_errors,
                    "metadata_extra_json": _compact_json(metadata_extra),
                    "reading_extra_json": _compact_json(reading_extra),
                }
                rows.append(row)

    rows.sort(
        key=lambda row: (
            _sortable_int(row.get("mrid")),
            _sortable_int(row.get("timestamp_utc")),
            str(row.get("measurement", "")),
            str(row.get("port_number", "")),
        )
    )
    return rows


def wide_tables(batch: ReadingBatch) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Build one observation row per MRID and preserve quality metadata separately.

    Duplicate variables from distinct sensors are disambiguated using their
    port and sensor serial. Conflicting duplicate cells fail loudly.
    """
    identities = {(str(r["measurement"]), str(r.get("port_number") or ""),
                   str(r.get("sensor_sn") or "")) for r in batch.rows}
    counts = {}
    for measurement, _, _ in identities:
        counts[measurement] = counts.get(measurement, 0) + 1
    columns = {}
    for measurement, port, serial in sorted(identities):
        name = measurement if counts[measurement] == 1 else f"{measurement} [port {port}, {serial}]"
        columns[(measurement, port, serial)] = name
    data_by_mrid: dict[int, dict[str, Any]] = {}
    quality: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    for r in batch.rows:
        mrid = int(r["mrid"])
        ts = int(float(r["timestamp_utc"]))
        dtm = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        key = (str(r["measurement"]), str(r.get("port_number") or ""), str(r.get("sensor_sn") or ""))
        column = columns[key]
        rec = data_by_mrid.setdefault(mrid, {"dtm": dtm, "mrid": mrid})
        if rec["dtm"] != dtm or column in rec:
            raise ValueError(f"Conflicting TAHMO reading for MRID {mrid}: {column}")
        rec[column] = r.get("value")
        metadata[column] = {"units": str(r.get("units") or "").strip(),
                            "sensor_sn": r.get("sensor_sn"), "sensor_name": r.get("sensor_name"),
                            "port_number": r.get("port_number"), "sensor_depth": r.get("sensor_depth")}
        flag = str(r.get("error_flag") or "").lower() in ("true", "1")
        if flag or r.get("error_description") or r.get("metadata_errors"):
            quality.append({"dtm": dtm, "mrid": mrid, "variable": column,
                            "error_flag": flag, "error_description": r.get("error_description") or "",
                            "sensor_errors": r.get("metadata_errors") or ""})
    cols = ["dtm", "mrid", *sorted(set(columns.values()))]
    return cols, [data_by_mrid[n] for n in sorted(data_by_mrid)], quality, metadata


def write_batch_zip(*, batch: ReadingBatch, path: Path) -> None:
    """Write a simple wide CSV plus quality and metadata sidecars into a ZIP."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns, data, quality, metadata = wide_tables(batch)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(data)
    quality_buffer = io.StringIO(newline="")
    qw = csv.DictWriter(quality_buffer, fieldnames=(
        "dtm", "mrid", "variable", "error_flag", "error_description", "sensor_errors"))
    qw.writeheader()
    qw.writerows(quality)
    temp = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with ZipFile(temp, mode="w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(f"{path.stem}.csv", buffer.getvalue().encode("utf-8"))
            archive.writestr(f"{path.stem}-quality.csv", quality_buffer.getvalue().encode("utf-8"))
            archive.writestr(f"{path.stem}-metadata.json", json.dumps(
                metadata, indent=2, ensure_ascii=False).encode("utf-8"))
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _authorization_value(token: str) -> str:
    token = token.strip()
    return token if token.lower().startswith("token ") else f"Token {token}"


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        shutil.copy2(source, temp)
        os.replace(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _compact_json(value: Mapping[str, Any]) -> str:
    if not value:
        return ""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _instrument_root(root: Path, name: str) -> Path:
    return root if root.name == name else root / name


def _sortable_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 2**63 - 1


def _one_line_error(exc: Exception) -> str:
    return " ".join(str(exc).split())
