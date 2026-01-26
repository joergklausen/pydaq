"""Configuration schema and loader.

The platform uses YAML for station configuration because it is:
- easy to read and edit
- supports comments
- naturally represents nested mappings for instrument configuration

The config content is conventionally all lower-case, with a few exceptions. 
Secrets should be stored in secret files in special folders (e.g. ``~/.ssh`` or ``~/.secrets``).
The YAML config can reference these files, and the platform will read their contents at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import yaml


class ConfigError(ValueError):
    """Raised when the station configuration cannot be parsed or validated."""
    pass


def _expand_user_path(text: str) -> Path:
    return Path(text).expanduser()


def _require_mapping_value(mapping: Dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"missing required key '{key}' in {context}")
    return mapping[key]


@dataclass(frozen=True)
class StationConfig:
    """Station identity and timezone configuration."""
    wsi: str
    id: str
    name: str = ""
    timezone: str = "UTC"


@dataclass(frozen=True)
class PathsConfig:
    """Filesystem layout for one station instance."""
    root: Path
    data: Path
    outbox: Path
    logs: Path


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration for console and file outputs."""
    level_console: str = "info"
    level_file: str = "info"
    file: str = "pydaq.log"


@dataclass(frozen=True)
class DashboardConfig:
    """Tiny HTTP dashboard config."""
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8088


@dataclass(frozen=True)
class MainConfig:
    """Main-loop parameters for the schedule-driven orchestrator."""
    loop_sleep_seconds: float = 1.0
    config_reload_seconds: int = 60
    dashboard: DashboardConfig = DashboardConfig()


@dataclass(frozen=True)
class TransferTargetConfig:
    """Configuration for one transfer target (e.g. s3, sftp)."""
    kind: str
    enabled: bool
    parameters: Dict[str, Any]


@dataclass(frozen=True)
class TransferConfig:
    """Transfer manager configuration."""
    enabled: bool
    require_all_targets: bool = False
    scan_every_seconds: int = 300
    retries: int = 3
    backoff_seconds: float = 2.0
    max_backoff_seconds: float = 30.0
    targets: List[TransferTargetConfig] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class InstrumentScheduleConfig:
    """Scheduling settings for an instrument."""
    sample_every_seconds: int
    rollover: str = "hourly"
    rollover_at: str = ":00"
    transmit_every_seconds: int = 600
    transmit_delay_seconds: int = 15
    print_every_seconds: int = 0


@dataclass(frozen=True)
class InstrumentOutputConfig:
    """How instrument data are staged and transmitted."""
    format: str = "csv_zip"
    remote_path: str = ""
    remove_on_success: bool = True


@dataclass(frozen=True)
class InstrumentConfig:
    """Configuration for one instrument entry."""
    name: str
    enabled: bool
    driver: str
    io: Dict[str, Any]
    schedule: InstrumentScheduleConfig
    output: InstrumentOutputConfig
    init: Dict[str, Any]
    processing: Dict[str, Any]


@dataclass(frozen=True)
class ApplicationConfig:
    """Parsed station configuration used by the orchestrator."""
    station: StationConfig
    paths: PathsConfig
    logging: LoggingConfig
    main: MainConfig
    transfer: TransferConfig
    io: Dict[str, Any]
    instruments: Dict[str, InstrumentConfig]


def _parse_station_config(raw: Dict[str, Any]) -> StationConfig:
    station_id = str(_require_mapping_value(raw, "id", "station")).strip().lower()
    if len(station_id) != 3:
        raise ConfigError("station.id must be exactly 3 characters (e.g. 'mkn', 'nrb')")
    return StationConfig(
        wsi=str(_require_mapping_value(raw, "wsi", "station")),
        id=station_id,
        name=str(raw.get("name", "")),
        timezone=str(raw.get("timezone", "UTC")),
    )


def _parse_paths_config(raw: Dict[str, Any]) -> PathsConfig:
    root = _expand_user_path(str(_require_mapping_value(raw, "root", "paths"))).resolve()
    data = root / str(raw.get("data", "data"))
    outbox = root / str(raw.get("outbox", "outbox"))
    logs = root / str(raw.get("logs", "logs"))
    return PathsConfig(root=root, data=data, outbox=outbox, logs=logs)


def _parse_logging_config(raw: Dict[str, Any]) -> LoggingConfig:
    return LoggingConfig(
        level_console=str(raw.get("level_console", "info")).lower(),
        level_file=str(raw.get("level_file", "info")).lower(),
        file=str(raw.get("file", "pydaq.log")),
    )


def _parse_main_config(raw: Dict[str, Any]) -> MainConfig:
    dashboard_raw = raw.get("dashboard", {}) or {}
    dashboard = DashboardConfig(
        enabled=bool(dashboard_raw.get("enabled", True)),
        host=str(dashboard_raw.get("host", "0.0.0.0")),
        port=int(dashboard_raw.get("port", 8088)),
    )
    return MainConfig(
        loop_sleep_seconds=float(raw.get("loop_sleep_seconds", raw.get("loop_sleep_s", 1.0))),
        config_reload_seconds=int(raw.get("config_reload_seconds", raw.get("config_reload_s", 60.0))),
        dashboard=dashboard,
    )


def _parse_transfer_config(raw: Dict[str, Any]) -> TransferConfig:
    """Parse transfer configuration.

    Supports two YAML shapes for ``transfer.targets``:

    **Mapping style (recommended for readability)**

    .. code-block:: yaml

        transfer:
          targets:
            s3:
              enabled: true
              parameters: {...}
            sftp:
              enabled: true
              parameters: {...}

    **List style (allows multiple targets of the same kind)**

    .. code-block:: yaml

        transfer:
          targets:
            - kind: s3
              enabled: true
              parameters: {...}

    Args:
        raw: Parsed ``transfer`` mapping.

    Returns:
        TransferConfig instance.

    Raises:
        ConfigError: If the structure is invalid.
    """
    enabled = bool(raw.get("enabled", False))
    targets_raw = raw.get("targets", None)

    targets: List[TransferTargetConfig] = []

    if targets_raw is None:
        targets_raw = {}

    # Style 1: list of {kind, enabled, parameters}
    if isinstance(targets_raw, list):
        for idx, entry in enumerate(targets_raw):
            if not isinstance(entry, dict):
                raise ConfigError(f"transfer.targets[{idx}] must be a mapping")
            kind = str(_require_mapping_value(entry, "kind", f"transfer.targets[{idx}]")).lower()
            targets.append(
                TransferTargetConfig(
                    kind=kind,
                    enabled=bool(entry.get("enabled", True)),
                    parameters=dict(entry.get("parameters", {}) or {}),
                )
            )

    # Style 2: mapping of kind -> {enabled, parameters}
    elif isinstance(targets_raw, dict):
        for kind_key, entry in targets_raw.items():
            if not isinstance(entry, dict):
                raise ConfigError(f"transfer.targets.{kind_key} must be a mapping")
            kind = str(kind_key).lower()
            targets.append(
                TransferTargetConfig(
                    kind=kind,
                    enabled=bool(entry.get("enabled", True)),
                    parameters=dict(entry.get("parameters", {}) or {}),
                )
            )
    else:
        raise ConfigError("'transfer.targets' must be a list or mapping")

    return TransferConfig(
        enabled=enabled,
        require_all_targets=bool(raw.get("require_all_targets", False)),
        scan_every_seconds=int(raw.get("scan_every_seconds", 300)),
        retries=int(raw.get("retries", 3)),
        backoff_seconds=float(raw.get("backoff_seconds", 2.0)),
        max_backoff_seconds=float(raw.get("max_backoff_seconds", 30.0)),
        targets=targets,
    )

def _parse_instrument_schedule_config(raw: Dict[str, Any]) -> InstrumentScheduleConfig:
    return InstrumentScheduleConfig(
        sample_every_seconds=int(_require_mapping_value(raw, "sample_every_seconds", "instruments.<name>.schedule")),
        rollover=str(raw.get("rollover", "hourly")).lower(),
        rollover_at=str(raw.get("rollover_at", ":00")),
        transmit_every_seconds=int(raw.get("transmit_every_seconds", 600)),
        transmit_delay_seconds=int(raw.get("transmit_delay_seconds", 15)),
        print_every_seconds=int(raw.get("print_every_seconds", 0)),
    )


def _parse_instrument_output_config(raw: Dict[str, Any]) -> InstrumentOutputConfig:
    return InstrumentOutputConfig(
        format=str(raw.get("format", "csv_zip")).lower(),
        remote_path=str(raw.get("remote_path", "")),
        remove_on_success=bool(raw.get("remove_on_success", True)),
    )


def load_config(config_path: Path) -> ApplicationConfig:
    """Load and validate a station YAML configuration."""
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("top-level YAML must be a mapping/dictionary")

    station = _parse_station_config(_require_mapping_value(raw, "station", "root"))
    paths = _parse_paths_config(_require_mapping_value(raw, "paths", "root"))
    logging = _parse_logging_config(raw.get("logging", {}) or {})
    main = _parse_main_config(raw.get("main", {}) or {})
    transfer = _parse_transfer_config(raw.get("transfer", {}) or {})
    io = dict(raw.get("io", {}) or {})

    instruments_raw = _require_mapping_value(raw, "instruments", "root")
    if not isinstance(instruments_raw, dict):
        raise ConfigError("'instruments' must be a mapping of instrument_name -> config")

    instruments: Dict[str, InstrumentConfig] = {}
    for instrument_name, instrument_raw in instruments_raw.items():
        if not isinstance(instrument_raw, dict):
            raise ConfigError(f"instruments.{instrument_name} must be a mapping")

        enabled = bool(instrument_raw.get("enabled", True))
        driver = str(_require_mapping_value(instrument_raw, "driver", f"instruments.{instrument_name}")).lower()
        io_mapping = dict(_require_mapping_value(instrument_raw, "io", f"instruments.{instrument_name}"))

        schedule_raw = dict(_require_mapping_value(instrument_raw, "schedule", f"instruments.{instrument_name}"))
        schedule = _parse_instrument_schedule_config(schedule_raw)

        output_raw = dict(instrument_raw.get("output", {}) or {})
        output = _parse_instrument_output_config(output_raw)

        init_mapping = dict(instrument_raw.get("init", {}) or {})
        processing_mapping = dict(instrument_raw.get("processing", {}) or {})

        instruments[str(instrument_name).lower()] = InstrumentConfig(
            name=str(instrument_name).lower(),
            enabled=enabled,
            driver=driver,
            io=io_mapping,
            schedule=schedule,
            output=output,
            init=init_mapping,
            processing=processing_mapping,
        )

    return ApplicationConfig(
        station=station,
        paths=paths,
        logging=logging,
        main=main,
        transfer=transfer,
        io=io,
        instruments=instruments,
    )
