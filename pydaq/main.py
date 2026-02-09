from __future__ import annotations

"""Main orchestrator for one station instance.

The orchestrator is intentionally built around the lightweight ``schedule`` library.

Responsibilities:
- Load YAML configuration into a validated config model.
- Start/stop instruments and allow runtime enable/disable by **hot-reloading** the config file.
- Attach schedule jobs for sampling, rollover, and transmission.
- Provide a minimal JSON dashboard for current readings.

Threading model:
- The scheduler loop runs in the main thread.
- Each instrument has a dedicated worker thread and task queue.
- The scheduler *enqueues* work; it does not perform IO directly.

This separation prevents one stuck instrument from blocking the entire application.
"""

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional
import logging

import schedule

from utils.config_handler import ApplicationConfig, InstrumentConfig, load_config
from pydaq.dashboard import start_dashboard
from pydaq.instruments.instrument import Instrument, get_driver_class
from pydaq.utils.transfer_handler import S3Target, SftpTarget, TransferHandler, TransferTarget
from pydaq.utils.logging_handler import setup_logging
from pydaq.utils.network_monitor import NetworkMonitor, ReachabilityTarget


def _fingerprint_configuration(value) -> str:
    """Create a stable fingerprint for a config structure.

    Args:
        value: Any JSON-serializable object.

    Returns:
        A SHA-256 hex digest.
    """
    blob = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Orchestrator:
    """Run and supervise a station's instrument park.

    Args:
        config_path: Path to the station YAML configuration file.

    Notes:
        The orchestrator watches the config file's mtime and reloads it periodically.
        Changes to an instrument's config cause that instrument to be recreated and rescheduled.
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self._config_mtime_seconds: float = 0.0
        self.application_config: Optional[ApplicationConfig] = None

        self.logger: logging.Logger = logging.getLogger("pydaq.orchestrator")
        self.instruments: Dict[str, Instrument] = {}
        self._instrument_config_fingerprints: Dict[str, str] = {}

        self.transfer_handler: Optional[TransferHandler] = None

        self._dashboard_server = None
        self._dashboard_thread = None

        self._network_monitor: Optional[NetworkMonitor] = None

        self._load_initial_configuration()

    def _load_initial_configuration(self) -> None:
        """Load config, initialize logging, and schedule main-level jobs."""
        self.application_config = load_config(self.config_path)
        self._config_mtime_seconds = self.config_path.stat().st_mtime

        self.logger = setup_logging(
            log_directory=self.application_config.paths.logs,
            file_name=self.application_config.logging.file,
            level_console=self.application_config.logging.level_console,
            level_file=self.application_config.logging.level_file,
        )
        self.logger.info("pydaq started station=%s config=%s", self.application_config.station.id, self.config_path)

        for directory in (
            self.application_config.paths.data,
            self.application_config.paths.outbox,
            self.application_config.paths.logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.transfer_handler = self._build_transfer_handler(self.application_config)
        self._apply_configuration(self.application_config)

        if self.application_config.main.dashboard.enabled:
            self._start_dashboard()


        # Reachability monitoring for LAN-connected instruments (derived from config).
        self._refresh_network_monitor(self.application_config)
        schedule.every(60).seconds.do(self._network_monitor_tick).tag("main:net_monitor")

        schedule.every(self.application_config.main.config_reload_seconds).seconds.do(self._check_for_config_reload).tag(
            "main:reload"
        )

        if self.transfer_handler and self.application_config.transfer.enabled:
            schedule.every(self.application_config.transfer.scan_every_seconds).seconds.do(self._transfer_scan_all).tag(
                "main:transfer_scan"
            )

    def _start_dashboard(self) -> None:
        """Start the dashboard server in a background thread."""
        assert self.application_config is not None
        server = start_dashboard(
            host=self.application_config.main.dashboard.host,
            port=self.application_config.main.dashboard.port,
            state_reference={"instruments": self.instruments},
        )
        self._dashboard_server = server

        import threading

        self._dashboard_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._dashboard_thread.start()
        self.logger.info(
            "dashboard listening on http://%s:%d",
            self.application_config.main.dashboard.host,
            self.application_config.main.dashboard.port,
        )


    # -------------------------------------------------------------------------
    # Network reachability monitoring
    # -------------------------------------------------------------------------

    @staticmethod
    def _io_get(io_obj, key: str, default=None):
        """Best-effort accessor for InstrumentConfig.io which may be dict-like or an object."""
        if io_obj is None:
            return default
        if isinstance(io_obj, dict):
            return io_obj.get(key, default)
        return getattr(io_obj, key, default)

    def _build_network_monitor_targets(self, config: ApplicationConfig) -> list[ReachabilityTarget]:
        """
        Build reachability targets from the current YAML config.

        Strategy
        --------
        - Include enabled instruments that declare an IP/hostname under ``io.host``.
        - If an instrument uses TCP (io.kind in {"socket", "tcp"}), include ``io.port`` and let
          the monitor do a TCP connect probe (preferred).
        - If an instrument is UDP-based (io.kind == "udp") or port is missing, fall back to ICMP
          ping (best-effort; may be blocked depending on device/network).

        Returns
        -------
        list[ReachabilityTarget]
            Targets to be probed by :class:`~pydaq.utils.network_monitor.NetworkMonitor`.
        """
        targets: list[ReachabilityTarget] = []

        for name, instrument_cfg in config.instruments.items():
            if not getattr(instrument_cfg, "enabled", False):
                continue

            io_cfg = getattr(instrument_cfg, "io", None)
            host = self._io_get(io_cfg, "host")
            if not host:
                continue

            kind = str(self._io_get(io_cfg, "kind", "") or "").lower()
            port = self._io_get(io_cfg, "port")

            tcpish = kind in {"socket", "tcp"}
            udpish = kind in {"udp", "udp_socket", "datagram"}

            use_port = int(port) if (tcpish and port is not None) else None
            method = "auto" if use_port is not None else "icmp"

            # For UDP-based devices, avoid misleading TCP probes.
            if udpish:
                use_port = None
                method = "icmp"

            targets.append(
                ReachabilityTarget(
                    name=name,
                    host=str(host),
                    port=use_port,
                    method=method,
                )
            )

        return targets

    def _refresh_network_monitor(self, config: ApplicationConfig) -> None:
        """Create or update the NetworkMonitor based on the latest config (hot-reload safe)."""
        targets = self._build_network_monitor_targets(config)

        if self._network_monitor is None:
            self._network_monitor = NetworkMonitor(
                logger=self.logger.getChild("net"),
                targets=targets,
                timeout_s=1.0,
                log_unchanged_every_n=0,
            )
            self.logger.info("[net] monitor enabled targets=%d", len(targets))
            return

        self._network_monitor.set_targets(targets, prune_state=True)
        self.logger.info("[net] monitor targets updated=%d", len(targets))

    def _network_monitor_tick(self) -> None:
        """Scheduled hook: run one reachability sweep (never raises)."""
        if not self._network_monitor:
            return
        try:
            self._network_monitor.check_all()
        except Exception as exc:
            # Never let reachability monitoring break the acquisition loop.
            self.logger.exception("[net] monitor tick failed (%s)", exc)

    def _build_transfer_handler(self, config: ApplicationConfig) -> Optional[TransferHandler]:
        """Construct a TransferHandler based on config, or return ``None``."""
        if not config.transfer.enabled:
            return None
        targets: list[TransferTarget] = []
        for target_config in config.transfer.targets or []:
            if not target_config.enabled:
                continue
            if target_config.kind == "s3":
                targets.append(S3Target(**target_config.parameters))
            elif target_config.kind == "sftp":
                targets.append(SftpTarget(**target_config.parameters))
            else:
                self.logger.warning("unknown transfer target kind=%s (ignored)", target_config.kind)
        if not targets:
            return None
        return TransferHandler(
            outbox_root=config.paths.outbox,
            targets=targets,
            require_all_targets=config.transfer.require_all_targets,
            retries=config.transfer.retries,
            backoff_seconds=config.transfer.backoff_seconds,
            max_backoff_seconds=config.transfer.max_backoff_seconds,
            logger=self.logger.getChild("transfer"),
        )

    def _create_instrument_instance(self, instrument_config: InstrumentConfig) -> Instrument:
        """Instantiate one instrument driver from its config."""
        assert self.application_config is not None
        instrument_class = get_driver_class(instrument_config.driver)

        data_directory = self.application_config.paths.data / instrument_config.name
        outbox_directory = self.application_config.paths.outbox / instrument_config.name

        data_directory.mkdir(parents=True, exist_ok=True)
        outbox_directory.mkdir(parents=True, exist_ok=True)

        driver_parameters = {
            "io": instrument_config.io,
            "init": instrument_config.init,
            "processing": instrument_config.processing,
            "output": asdict(instrument_config.output),
        }

        headers = getattr(instrument_class, "HEADERS", None)
        instrument = instrument_class(
            name=instrument_config.name,
            data_dir=data_directory,
            outbox_dir=outbox_directory,
            logger=self.logger,
            headers=headers if headers else None,
            output_format=instrument_config.output.format,
            parameters=driver_parameters,
        )
        return instrument

    def _schedule_instrument_jobs(self, instrument_config: InstrumentConfig, instrument: Instrument) -> None:
        """Attach schedule jobs for one instrument and clear any prior jobs."""
        tag = f"instrument:{instrument_config.name}"
        schedule.clear(tag)

        def initialize_once():
            instrument.request_initialize()
            return schedule.CancelJob

        schedule.every(1).seconds.do(initialize_once).tag(tag)
        schedule.every(instrument_config.schedule.sample_every_seconds).seconds.do(instrument.request_reading).tag(tag)

        if instrument_config.schedule.rollover == "hourly":
            schedule.every().hour.at(instrument_config.schedule.rollover_at).do(instrument.request_rollover).tag(tag)
        elif instrument_config.schedule.rollover == "daily":
            schedule.every().day.at(instrument_config.schedule.rollover_at).do(instrument.request_rollover).tag(tag)

        if instrument_config.schedule.print_every_seconds > 0:
            schedule.every(instrument_config.schedule.print_every_seconds).seconds.do(
                self._log_latest_for_instrument, instrument_config.name
            ).tag(tag)

        if self.transfer_handler and self.application_config and self.application_config.transfer.enabled:

            def transmit_job():
                time.sleep(max(0, instrument_config.schedule.transmit_delay_seconds))
                instrument.request_transmit(self._transmit_one_instrument)

            schedule.every(instrument_config.schedule.transmit_every_seconds).seconds.do(transmit_job).tag(tag)

    def _log_latest_for_instrument(self, instrument_name: str) -> None:
        """Log the latest record for one instrument (debug/ops convenience)."""
        instrument = self.instruments.get(instrument_name)
        if not instrument:
            return
        self.logger.info("[%s] latest=%s", instrument_name, instrument.state.latest or {})

    def _transmit_one_instrument(self, instrument_name: str) -> None:
        """Transmit all outbox files for a single instrument."""
        if not self.transfer_handler or not self.application_config:
            return
        instrument_config = self.application_config.instruments.get(instrument_name)
        if not instrument_config:
            return
        remote_path = instrument_config.output.remote_path or instrument_name
        self.transfer_handler.transmit_instrument(
            instrument_name,
            remote_path,
            remove_on_success=bool(instrument_config.output.remove_on_success),
        )

    def _transfer_scan_all(self) -> None:
        """Periodic scan of all outboxes."""
        if not self.transfer_handler or not self.application_config:
            return
        instrument_remote_path_map = {
            name: (cfg.output.remote_path or name) for name, cfg in self.application_config.instruments.items()
        }
        instrument_remove_on_success_map = {
            name: bool(cfg.output.remove_on_success) for name, cfg in self.application_config.instruments.items()
        }
        self.transfer_handler.transmit_all(instrument_remote_path_map, instrument_remove_on_success_map)

    def _apply_configuration(self, config: ApplicationConfig) -> None:
        """Apply config changes: enable/disable instruments and reschedule jobs."""
        desired_instruments = config.instruments

        for instrument_name in list(self.instruments.keys()):
            if instrument_name not in desired_instruments:
                self._disable_instrument(instrument_name, reason="removed from config")

        for instrument_name, instrument_config in desired_instruments.items():
            fingerprint = _fingerprint_configuration(asdict(instrument_config))
            existing = self.instruments.get(instrument_name)

            if not instrument_config.enabled:
                if existing:
                    self._disable_instrument(instrument_name, reason="disabled in config")
                continue

            if existing is None or self._instrument_config_fingerprints.get(instrument_name) != fingerprint:
                if existing:
                    self._disable_instrument(instrument_name, reason="config changed")
                instrument = self._create_instrument_instance(instrument_config)
                self.instruments[instrument_name] = instrument
                self._instrument_config_fingerprints[instrument_name] = fingerprint
                instrument.set_enabled(True)
                instrument.start()
                self._schedule_instrument_jobs(instrument_config, instrument)
                self.logger.info("[%s] enabled driver=%s", instrument_name, instrument_config.driver)
            else:
                existing.set_enabled(True)
                self._schedule_instrument_jobs(instrument_config, existing)

    def _disable_instrument(self, instrument_name: str, reason: str) -> None:
        """Stop an instrument and remove its scheduled jobs."""
        instrument = self.instruments.pop(instrument_name, None)
        schedule.clear(f"instrument:{instrument_name}")
        self._instrument_config_fingerprints.pop(instrument_name, None)
        if instrument:
            instrument.set_enabled(False)
            instrument.stop()
        self.logger.info("[%s] disabled (%s)", instrument_name, reason)

    def _check_for_config_reload(self) -> None:
        """Reload config if the file changed on disk."""
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            self.logger.error("config disappeared: %s", self.config_path)
            return
        if mtime <= self._config_mtime_seconds:
            return
        try:
            new_config = load_config(self.config_path)
        except Exception as exc:
            self.logger.exception("config reload failed; keeping previous (%s)", exc)
            self._config_mtime_seconds = mtime
            return
        self._config_mtime_seconds = mtime
        self.application_config = new_config
        self.logger.info("config reloaded: %s", self.config_path)
        self.transfer_handler = self._build_transfer_handler(new_config)
        self._apply_configuration(new_config)
        self._refresh_network_monitor(new_config)

    def run_forever(self) -> None:
        """Run the scheduler loop forever."""
        assert self.application_config is not None
        sleep = self.application_config.main.loop_sleep_seconds
        while True:
            schedule.run_pending()
            time.sleep(sleep)
