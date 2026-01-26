"""PYDAQ orchestrator (APScheduler-based).

Goals
- Robust scheduling with bounded concurrency (no unbounded "thread-per-job-fire").
- Stable job IDs per instrument (easy pause/resume/remove).
- Hot reload of YAML configuration (add/remove/disable instruments without stopping others).
- Fault containment: an error in one instrument job should not stall the whole app.

Notes
- This module expects instrument drivers to provide (some or all of):
    - get_data()
    - display_data()
    - save_data_file()
    - stage_data_file()
    - transfer_files()
  Missing methods are tolerated (jobs are simply not scheduled).
- Drivers are instantiated via import path in config (e.g., "instr.thermo.Thermo").
  By default we call cls(name=<name>, config_path=<path>), but we also try to pass
  config/params/logger if the constructor supports them.

Dependency
- APScheduler is required: `pip install apscheduler`
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import logging
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from utils.config import load_config
from utils.logging import setup_logging  # type: ignore

try:
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except Exception:  # pragma: no cover
    ThreadPoolExecutor = None  # type: ignore[assignment]
    BackgroundScheduler = None  # type: ignore[assignment]
    CronTrigger = None  # type: ignore[assignment]
    IntervalTrigger = None  # type: ignore[assignment]


# -----------------------------
# Config normalization helpers
# -----------------------------

def _normalize_instrument_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize instrument entry schemas.

    Supports:
      1) {"name": "...", "driver": "...", "params": {...}, ...}
      2) {"<name>": {"driver": "...", "params": {...}, ...}}   # legacy/alternate
    """
    if "name" in raw and "driver" in raw:
        entry = dict(raw)
        entry.setdefault("enabled", True)
        entry.setdefault("params", {})
        return entry

    # Legacy/alternate schema: single-key mapping
    if len(raw) == 1:
        name = next(iter(raw.keys()))
        inner = raw[name] or {}
        if not isinstance(inner, dict):
            raise ValueError(f"Instrument '{name}' must map to a dict, got {type(inner)}")
        entry = dict(inner)
        entry["name"] = name
        entry.setdefault("enabled", True)
        entry.setdefault("params", {})
        # Many configs used "type" instead of "model"; keep both.
        if "model" not in entry and "type" in entry:
            entry["model"] = entry["type"]
        return entry

    raise ValueError(f"Unsupported instrument entry schema: {raw!r}")


def _instrument_fingerprint(entry: dict[str, Any]) -> str:
    """Create a stable fingerprint for change detection (driver + params + model + enabled)."""
    payload = {
        "name": entry.get("name"),
        "driver": entry.get("driver"),
        "model": entry.get("model"),
        "params": entry.get("params", {}),
        "enabled": bool(entry.get("enabled", True)),
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _safe_getattr(obj: Any, attr: str) -> Optional[Callable[..., Any]]:
    fn = getattr(obj, attr, None)
    return fn if callable(fn) else None


def _safe_close(obj: Any, logger: logging.Logger, name: str) -> None:
    """Best-effort shutdown hook for drivers."""
    for meth in ("close", "shutdown", "stop", "__exit__"):
        fn = _safe_getattr(obj, meth)
        if fn is None:
            continue
        try:
            # __exit__ signature: (exc_type, exc, tb)
            if meth == "__exit__":
                fn(None, None, None)
            else:
                fn()
            return
        except Exception as err:  # pragma: no cover
            logger.warning("[%s] %s() failed during shutdown: %s", name, meth, err)
            return


# -----------------------------
# Runtime models
# -----------------------------

@dataclass
class InstrumentRuntime:
    name: str
    entry: dict[str, Any]
    fingerprint: str
    instr: Any
    jobs: dict[str, str] = field(default_factory=dict)  # key -> job_id
    failures: dict[str, int] = field(default_factory=dict)  # key -> count
    last_success: dict[str, float] = field(default_factory=dict)  # key -> epoch seconds
    restart_pending: bool = False


# -----------------------------
# Instrument manager
# -----------------------------

class InstrumentManager:
    """Owns instrument lifecycles + APScheduler jobs."""

    def __init__(
        self,
        *,
        scheduler: "BackgroundScheduler",
        logger: logging.Logger,
        config_path: Path,
        reload_seconds: int = 30,
        restart_failures: int = 5,
        tz: Optional[str] = None,
        simulate: bool = False,
    ) -> None:
        self.scheduler = scheduler
        self.logger = logger
        self.config_path = config_path
        self.reload_seconds = max(5, int(reload_seconds))
        self.restart_failures = max(1, int(restart_failures))
        self.tz = tz
        self.simulate = bool(simulate)

        # Latest successfully applied config (used for restarts triggered from job threads)
        self._cfg: dict[str, Any] = {}

        self._runtimes: dict[str, InstrumentRuntime] = {}
        self._config_mtime: float = 0.0
        self._last_reload_check: float = 0.0

        # Actions are processed in the main loop (avoids doing lifecycle ops inside worker threads)
        self._actions: "queue.SimpleQueue[tuple[str, str, str]]" = queue.SimpleQueue()

        # De-duplicate restart requests (many failures may enqueue in a short time)
        self._restart_scheduled: set[str] = set()

    # ---- lifecycle public API ----

    def bootstrap(self, cfg: dict[str, Any]) -> None:
        """Initial load + schedule all enabled instruments."""
        self._config_mtime = self._get_mtime()
        self.apply_config(cfg)

    def tick(self) -> None:
        """Main-loop tick: process actions + optionally reload config."""
        self._process_actions()
        self._reload_if_changed()

    def shutdown(self) -> None:
        """Remove all jobs and close all instruments."""
        for name in list(self._runtimes.keys()):
            self._remove_instrument(name, reason="shutdown")

    # ---- config reload ----

    def _get_mtime(self) -> float:
        try:
            return self.config_path.stat().st_mtime
        except FileNotFoundError:  # pragma: no cover
            return 0.0

    def _reload_if_changed(self) -> None:
        now = time.time()
        if (now - self._last_reload_check) < self.reload_seconds:
            return
        self._last_reload_check = now

        mtime = self._get_mtime()
        if mtime <= self._config_mtime:
            return

        self.logger.info("Config file changed; reloading %s", self.config_path)
        try:
            cfg = load_config(self.config_path)
        except Exception as err:  # pragma: no cover
            self.logger.exception("Failed to reload config (%s). Keeping current runtime.", err)
            self._config_mtime = mtime
            return

        try:
            self.apply_config(cfg)
            self._config_mtime = mtime
        except Exception as err:  # pragma: no cover
            self.logger.exception("Failed to apply new config (%s). Keeping current runtime.", err)
            self._config_mtime = mtime

    # ---- diff/apply ----

    def apply_config(self, cfg: dict[str, Any]) -> None:
        """Diff desired instruments vs current, then add/remove/restart as needed."""
        # Keep a reference to the latest config for restarts triggered by job failures
        self._cfg = cfg
        raw_entries = cfg.get("instruments", []) or []
        desired: dict[str, dict[str, Any]] = {}

        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise ValueError(f"Instrument entries must be dicts; got {type(raw)}: {raw!r}")
            entry = _normalize_instrument_entry(raw)
            name = entry["name"]
            desired[name] = entry

        # Remove instruments not in desired
        for name in list(self._runtimes.keys()):
            if name not in desired:
                self._remove_instrument(name, reason="removed from config")

        # Add/restart/disable desired instruments
        for name, entry in desired.items():
            enabled = bool(entry.get("enabled", True))
            fp = _instrument_fingerprint(entry)

            if not enabled:
                if name in self._runtimes:
                    self._remove_instrument(name, reason="disabled in config")
                continue

            if name not in self._runtimes:
                self._add_instrument(name, entry, fp, cfg)
                continue

            # existing: restart if fingerprint changed
            rt = self._runtimes[name]
            if rt.fingerprint != fp:
                self._restart_instrument(name, entry, fp, cfg, reason="config changed")

    # ---- add/remove/restart ----

    def _add_instrument(self, name: str, entry: dict[str, Any], fp: str, cfg: dict[str, Any]) -> None:
        self.logger.info("[%s] Adding instrument (%s)", name, entry.get("driver"))
        instr = self._instantiate_driver(name=name, entry=entry, cfg=cfg)

        rt = InstrumentRuntime(name=name, entry=entry, fingerprint=fp, instr=instr)
        self._runtimes[name] = rt

        self._schedule_jobs(rt)

        # Prime buffer (non-fatal if fails)
        fn = _safe_getattr(instr, "get_data")
        if fn:
            try:
                fn()
            except Exception as err:  # pragma: no cover
                self._log_for(rt, level="warning", msg="Initial get_data() failed (will retry): %s", err=err)

    def _remove_instrument(self, name: str, reason: str) -> None:
        rt = self._runtimes.get(name)
        if not rt:
            return
        self.logger.info("[%s] Removing instrument (%s)", name, reason)

        # Remove scheduled jobs
        for _, job_id in list(rt.jobs.items()):
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                # job may already be gone
                pass
        rt.jobs.clear()

        # Close driver
        try:
            _safe_close(rt.instr, self.logger, name)
        finally:
            self._runtimes.pop(name, None)

    def _restart_instrument(self, name: str, entry: dict[str, Any], fp: str, cfg: dict[str, Any], reason: str) -> None:
        self.logger.warning("[%s] Restarting instrument (%s)", name, reason)
        self._remove_instrument(name, reason="restart")
        self._add_instrument(name, entry, fp, cfg)

    # ---- action processing ----

    def request_restart(self, name: str, reason: str) -> None:
        rt = self._runtimes.get(name)
        if rt is None:
            return
        if name in self._restart_scheduled:
            return
        self._restart_scheduled.add(name)
        self._actions.put(("restart", name, reason))

    def _process_actions(self) -> None:
        while True:
            try:
                action, name, reason = self._actions.get_nowait()
            except Exception:
                break

            if action != "restart":
                continue

            rt = self._runtimes.get(name)
            try:
                if rt is not None:
                    self._restart_instrument(name, rt.entry, rt.fingerprint, self._cfg, reason=reason)
            finally:
                # Ensure we clear dedup state even if restart fails
                self._restart_scheduled.discard(name)

    # ---- scheduling ----

    def _schedule_jobs(self, rt: InstrumentRuntime) -> None:
        """Register APScheduler jobs for one instrument runtime."""
        instr = rt.instr
        name = rt.name
        params = rt.entry.get("params", {}) or {}

        # intervals: use driver attributes if present, else fall back to params defaults
        display_s = int(getattr(instr, "console_display_interval_seconds", params.get("console_display_interval_seconds", 20)))
        sample_s = int(getattr(instr, "sampling_interval_seconds", params.get("sampling_interval_seconds", 60)))
        report_min = int(getattr(instr, "reporting_interval_minutes", params.get("reporting_interval_minutes", 60)))

        # JOB: display
        display_fn = _safe_getattr(instr, "display_data")
        if display_fn and display_s > 0:
            job_id = f"{name}:display"
            self._add_interval_job(rt, "display", display_fn, seconds=display_s, job_id=job_id)

        # JOB: sample
        sample_fn = _safe_getattr(instr, "get_data")
        if sample_fn and sample_s > 0:
            job_id = f"{name}:sample"
            self._add_interval_job(rt, "sample", sample_fn, seconds=sample_s, job_id=job_id)

        # JOB: save (aligned every 10 minutes by default)
        save_fn = _safe_getattr(instr, "save_data_file") or _safe_getattr(instr, "save_data")
        if save_fn:
            job_id = f"{name}:save"
            trigger = CronTrigger(minute="0,10,20,30,40,50", second=0)
            self._add_job(rt, "save", save_fn, trigger=trigger, job_id=job_id)

        # JOB: stage + transfer (composite)
        stage_fn = _safe_getattr(instr, "stage_data_file")
        xfer_fn = _safe_getattr(instr, "transfer_files")
        if stage_fn and xfer_fn:
            job_id = f"{name}:stage_transfer"
            trigger = self._report_trigger(report_min)
            self._add_job(rt, "stage_transfer", lambda: self._stage_then_transfer(rt), trigger=trigger, job_id=job_id)

    def _report_trigger(self, report_min: int) -> Any:
        """Create an aligned trigger for stage/transfer."""
        # Default: hourly at :00:30
        if report_min >= 1440:
            return CronTrigger(hour=0, minute=0, second=50)
        if report_min == 10:
            return CronTrigger(minute="0,10,20,30,40,50", second=50)
        if report_min in (30, 60, 120, 180, 360, 720):
            # Align to top-of-hour, but still run at +30s so it follows the save job.
            return CronTrigger(minute=0, second=50)
        # Fallback: simple interval (may drift, but safe)
        return IntervalTrigger(minutes=max(1, report_min))

    def _add_interval_job(self, rt: InstrumentRuntime, key: str, fn: Callable[[], Any], *, seconds: int, job_id: str) -> None:
        trigger = IntervalTrigger(seconds=max(1, seconds))
        self._add_job(rt, key, fn, trigger=trigger, job_id=job_id)

    def _add_job(self, rt: InstrumentRuntime, key: str, fn: Callable[[], Any], *, trigger: Any, job_id: str) -> None:
        """Add a guarded job with stable ID and overlap protection."""
        wrapped = self._guarded_job(rt, key, fn)

        # Replace existing (idempotent for reload/restart)
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

        self.scheduler.add_job(
            wrapped,
            trigger=trigger,
            id=job_id,
            max_instances=1,      # don't overlap the same job
            coalesce=True,        # if missed, coalesce to one run
            misfire_grace_time=60,
            replace_existing=True,
        )
        rt.jobs[key] = job_id

    def _guarded_job(self, rt: InstrumentRuntime, key: str, fn: Callable[[], Any]) -> Callable[[], None]:
        """Wrap a job to (a) log context, (b) keep failure counters, (c) request restart."""
        def _run() -> None:
            instr_logger = getattr(rt.instr, "logger", self.logger)
            try:
                fn()
                rt.failures[key] = 0
                rt.last_success[key] = time.time()
            except Exception as err:  # pragma: no cover
                rt.failures[key] = rt.failures.get(key, 0) + 1
                # Use .exception to capture traceback
                try:
                    instr_logger.exception("[%s] job '%s' failed (%d): %s", rt.name, key, rt.failures[key], err)
                except Exception:
                    self.logger.exception("[%s] job '%s' failed (%d): %s", rt.name, key, rt.failures[key], err)

                # restart policy: only for sampling job by default
                if key == "sample" and rt.failures[key] >= self.restart_failures:
                    self.request_restart(rt.name, f"sample failed {rt.failures[key]} times")
        return _run

    def _stage_then_transfer(self, rt: InstrumentRuntime) -> None:
        """Composite action: stage then transfer (keeps order inside one job)."""
        instr = rt.instr
        log = getattr(instr, "logger", self.logger)

        try:
            instr.stage_data_file()
        except Exception as err:  # pragma: no cover
            log.error("[%s] stage_data_file() failed: %s", rt.name, err)
            return

        try:
            instr.transfer_files()
        except Exception as err:  # pragma: no cover
            log.error("[%s] transfer_files() failed: %s", rt.name, err)

    # ---- driver instantiation ----

    def _instantiate_driver(self, *, name: str, entry: dict[str, Any], cfg: dict[str, Any]) -> Any:
        driver_path = entry["driver"]
        module_name, class_name = driver_path.rsplit(".", 1)
        cls = getattr(importlib.import_module(module_name), class_name)

        # Try to pass richer kwargs if accepted by the driver.
        params = entry.get("params", {}) or {}
        ctor = getattr(cls, "__init__", None)
        kwargs: dict[str, Any] = {}

        if ctor:
            try:
                sig = inspect.signature(ctor)
                # note: signature includes "self"
                if "name" in sig.parameters:
                    kwargs["name"] = name
                if "config_path" in sig.parameters:
                    kwargs["config_path"] = str(self.config_path)
                if "config" in sig.parameters:
                    kwargs["config"] = cfg
                if "cfg" in sig.parameters:
                    kwargs["cfg"] = cfg
                if "entry" in sig.parameters:
                    kwargs["entry"] = entry
                if "params" in sig.parameters:
                    kwargs["params"] = params
                if "logger" in sig.parameters:
                    kwargs["logger"] = self.logger
                if "simulate" in sig.parameters:
                    kwargs["simulate"] = self.simulate
            except Exception:
                kwargs = {}

        # Fallback order:
        # 1) kwargs-based construction
        # 2) legacy (name, config_path)
        # 3) just (name)
        try:
            if kwargs:
                return cls(**kwargs)
        except TypeError:
            pass

        try:
            return cls(name=name, config_path=str(self.config_path))
        except TypeError:
            return cls(name=name)

    # ---- logging ----

    def _log_for(self, rt: InstrumentRuntime, *, level: str, msg: str, err: Exception) -> None:
        log = getattr(rt.instr, "logger", self.logger)
        fn = getattr(log, level, log.info)
        fn("[%s] " + msg, rt.name, err)


# -----------------------------
# Main
# -----------------------------

def main() -> None:
    if BackgroundScheduler is None:
        raise SystemExit("APScheduler is required. Install with: pip install apscheduler")

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="pydaq.yaml", help="Path to YAML configuration file.")
    parser.add_argument("--simulate", action="store_true", help="Force simulation mode (if drivers support it).")
    parser.add_argument("--reload-seconds", type=int, default=None, help="How often to check the config for changes.")
    parser.add_argument("--max-workers", type=int, default=None, help="ThreadPool size for job execution.")
    parser.add_argument("--restart-failures", type=int, default=5, help="Restart driver after N consecutive sample failures.")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    cfg = load_config(config_path)

    simulate = bool(args.simulate or cfg.get("simulate", False))

    # setup weekly rotating logfile + console output
    log_dir = Path(cfg.get("local", {}).get("logging", ".")).expanduser()
    log_file = log_dir / cfg.get("logging", {}).get("file_name", "pydaq.log")
    logger = setup_logging(
        log_file,
        level_console=cfg.get("logging", {}).get("level_console", 20),
        level_file=cfg.get("logging", {}).get("level_file", 40),
    )

    logger.info("== PYDAQ started (config: %s) ====", config_path, extra={"to_logfile": True})

    # Scheduler with bounded thread pool
    max_workers = args.max_workers
    if max_workers is None:
        max_workers = int(cfg.get("local", {}).get("max_workers", 20))

    reload_seconds = args.reload_seconds
    if reload_seconds is None:
        reload_seconds = int(cfg.get("local", {}).get("reload_seconds", 30))

    scheduler = BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=max(1, int(max_workers)))},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )

    manager = InstrumentManager(
        scheduler=scheduler,
        logger=logger,
        config_path=config_path,
        reload_seconds=reload_seconds,
        restart_failures=args.restart_failures,
        simulate=simulate,
    )

    # Load initial instruments and start scheduler
    manager.bootstrap(cfg)
    scheduler.start()

    logger.info("Scheduler started (max_workers=%s, reload_seconds=%s). CTRL+C to exit.", max_workers, reload_seconds)

    try:
        while True:
            manager.tick()
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received; shutting down...")
    finally:
        try:
            manager.shutdown()
        finally:
            scheduler.shutdown(wait=False)
            logger.info("PYDAQ stopped.")


if __name__ == "__main__":
    main()

# """pydaq orchestrator.

# - Configures weekly rotating logging via `utils.logging.setup_logging`.
# - Loads instruments defined in the YAML and wires schedules using a thread wrapper
#   so slow serial/socket work never blocks the scheduler loop.
# - Runtime policy:
#   (1) Start accumulating immediately (prime the buffer with an initial get_data()).
#   (2) Save every 10 minutes aligned to :00,:10,:20,:30,:40,:50.
#   (3) Log init-time instrument responses (drivers implement .initialize()).
# """

# from __future__ import annotations

# import argparse
# import importlib
# import threading
# import time
# from pathlib import Path
# from typing import Any, Dict

# import schedule

# from utils.config import load_config
# from utils.logging import setup_logging  # type: ignore


# def run_threaded(job_func, *args, **kwargs):
#     """Submit a job to a daemon thread so schedule.run_pending() stays non-blocking."""
#     t = threading.Thread(target=job_func, args=args, kwargs=kwargs, daemon=True)
#     t.start()
#     return t


# def setup_schedules(instr) -> None:
#     """Setup schedule(s) for specified instrument."""
#     # Console display every 20' (threaded so the loop never blocks)
#     schedule.every(instr.console_display_interval_seconds).seconds.do(run_threaded, instr.display_data)
    
#     # Sample every sampling interval (threaded so the loop never blocks)
#     schedule.every(instr.sampling_interval_seconds).seconds.do(run_threaded, instr.get_data)

#     # Save every 10 minutes, aligned (reduces loss on power cuts)
#     _schedule_aligned_every_10_minutes(instr.save_data_file)

#     # Stage+Transfer as a single composite job to enforce ordering
#     if instr.reporting_interval_minutes == 10:
#         # align to wall-clock :00,:10,:20,:30,:40,:50
#         for m in (0, 10, 20, 30, 40, 50):
#             schedule.every().hour.at(f":{m:02}").do(run_threaded, _stage_then_transfer, instr)
#     elif instr.reporting_interval_minutes >= 1440:
#         # daily at 00:00
#         schedule.every().day.at("00:00").do(run_threaded, _stage_then_transfer, instr)
#     else:
#         # hourly at :00
#         schedule.every().hour.at(":00").do(run_threaded, _stage_then_transfer, instr)

#     # Start accumulating right now (first file may be partial)
#     try:
#         instr.get_data()
#     except Exception as err:  # pragma: no cover
#         instr.logger.warning("Initial get_data() failed (will retry on schedule): %s", err)


# def load_instrument(entry: Dict[str, Any], config_path: str):
#     """
#     Instantiate an instrument `<module>.<Class>` with (name, config_path).
    
#     Args:
#         entry (Dict[str, Any]): ...
#         path (str | Path): Expanded path to the YAML configuration file.

#     Returns:
#         driver: instrument driver (class).

#     Raises:
#         ...

#     """
#     name = entry["name"]
#     module_name, driver_name = entry["driver"].rsplit(".", 1)
#     try:
#         cls = getattr(importlib.import_module(module_name), driver_name)
#         return cls(name=name, config_path=config_path)
#     except Exception as err:
#         pass


# def _stage_then_transfer(instr):
#     """
#     Composite job that guarantees ordering:
#     1) stage the most recently saved file, then
#     2) transfer it (S3 preferred, SFTP fallback).
#     Runs inside a single worker thread via run_threaded().
#     """
#     try:
#         instr.stage_data_file()
#     except Exception as err:  # pragma: no cover
#         instr.logger.error("stage_data_file() failed: %s", err)
#         return
#     try:
#         instr.transfer_files()
#     except Exception as err:  # pragma: no cover
#         instr.logger.error("transfer_files() failed: %s", err)


# def _schedule_aligned_every_10_minutes(job):
#     """Align save jobs to the wall clock every :00,:10,:20,:30,:40,:50."""
#     for m in (0, 10, 20, 30, 40, 50):
#         schedule.every().hour.at(f":{m:02}").do(run_threaded, job)


# def main() -> None:
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--config", default="pydaq.yaml")
#     parser.add_argument("--simulate", action="store_true")
#     args = parser.parse_args()

#     path = Path(args.config).expanduser()
#     cfg = load_config(path)

#     # setup weekly rotating logfile + console output
#     log_dir = Path(cfg.get("local", {}).get("logging", ".")).expanduser()
#     log_file = log_dir / cfg.get("logging", {}).get("file_name", "pydaq.log")
#     logger = setup_logging(
#         log_file,
#         level_console=cfg.get("logging", {}).get("level_console", 20),
#         level_file=cfg.get("logging", {}).get("level_file", 40),
#     )
#     print(f"logging to {logger.handlers} ...")

#     logger.info(f"== PYDAQ (with config file {path}) started (CTRL+C to exit) ====", extra={"to_logfile": True})

#     # instantiate and wire instruments, setup schedules
#     instruments = []
#     for entry in cfg.get("instruments", []):
#         instr = load_instrument(entry, config_path=args.config)
#         instruments.append(instr)
#         setup_schedules(instr)

#     while True:
#         schedule.run_pending()
#         time.sleep(1)


# if __name__ == "__main__":
#     main()

