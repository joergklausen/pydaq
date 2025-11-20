"""pydaq orchestrator.

- Configures weekly rotating logging via `utils.logging.setup_logging`.
- Loads instruments defined in the YAML and wires schedules using a thread wrapper
  so slow serial/socket work never blocks the scheduler loop.
- Runtime policy:
  (1) Start accumulating immediately (prime the buffer with an initial get_data()).
  (2) Save every 10 minutes aligned to :00,:10,:20,:30,:40,:50.
  (3) Log init-time instrument responses (drivers implement .initialize()).
"""

from __future__ import annotations

import argparse
import importlib
import threading
import time
from pathlib import Path
from typing import Any, Dict

import schedule

from utils.config import load_config
from utils.logging import setup_logging  # type: ignore


def run_threaded(job_func, *args, **kwargs):
    """Submit a job to a daemon thread so schedule.run_pending() stays non-blocking."""
    t = threading.Thread(target=job_func, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def setup_schedules(instr) -> None:
    """Setup schedule(s) for specified instrument."""
    # Console display every 20' (threaded so the loop never blocks)
    schedule.every(instr.console_display_interval_seconds).seconds.do(run_threaded, instr.display_data)
    
    # Sample every sampling interval (threaded so the loop never blocks)
    schedule.every(instr.sampling_interval_seconds).seconds.do(run_threaded, instr.get_data)

    # Save every 10 minutes, aligned (reduces loss on power cuts)
    _schedule_aligned_every_10_minutes(instr.save_data_file)

    # Stage+Transfer as a single composite job to enforce ordering
    if instr.reporting_interval_minutes == 10:
        # align to wall-clock :00,:10,:20,:30,:40,:50
        for m in (0, 10, 20, 30, 40, 50):
            schedule.every().hour.at(f":{m:02}").do(run_threaded, _stage_then_transfer, instr)
    elif instr.reporting_interval_minutes >= 1440:
        # daily at 00:00
        schedule.every().day.at("00:00").do(run_threaded, _stage_then_transfer, instr)
    else:
        # hourly at :00
        schedule.every().hour.at(":00").do(run_threaded, _stage_then_transfer, instr)

    # Start accumulating right now (first file may be partial)
    try:
        instr.get_data()
    except Exception as err:  # pragma: no cover
        instr.logger.warning("Initial get_data() failed (will retry on schedule): %s", err)


def load_instrument(entry: Dict[str, Any], config_path: str):
    """
    Instantiate an instrument `<module>.<Class>` with (name, config_path).
    
    Args:
        entry (Dict[str, Any]): ...
        path (str | Path): Expanded path to the YAML configuration file.

    Returns:
        driver: instrument driver (class).

    Raises:
        ...

    """
    name = entry["name"]
    module_name, driver_name = entry["driver"].rsplit(".", 1)
    try:
        cls = getattr(importlib.import_module(module_name), driver_name)
        return cls(name=name, config_path=config_path)
    except Exception as err:
        pass


def _stage_then_transfer(instr):
    """
    Composite job that guarantees ordering:
    1) stage the most recently saved file, then
    2) transfer it (S3 preferred, SFTP fallback).
    Runs inside a single worker thread via run_threaded().
    """
    try:
        instr.stage_data_file()
    except Exception as err:  # pragma: no cover
        instr.logger.error("stage_data_file() failed: %s", err)
        return
    try:
        instr.transfer_files()
    except Exception as err:  # pragma: no cover
        instr.logger.error("transfer_files() failed: %s", err)


def _schedule_aligned_every_10_minutes(job):
    """Align save jobs to the wall clock every :00,:10,:20,:30,:40,:50."""
    for m in (0, 10, 20, 30, 40, 50):
        schedule.every().hour.at(f":{m:02}").do(run_threaded, job)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="pydaq.yaml")
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args()

    path = Path(args.config).expanduser()
    cfg = load_config(path)

    # setup weekly rotating logfile + console output
    log_dir = Path(cfg.get("local", {}).get("logging", ".")).expanduser()
    log_file = log_dir / cfg.get("logging", {}).get("file_name", "pydaq.log")
    logger = setup_logging(
        log_file,
        level_console=cfg.get("logging", {}).get("level_console", 20),
        level_file=cfg.get("logging", {}).get("level_file", 40),
    )
    print(f"logging to {logger.handlers} ...")

    logger.info(f"== PYDAQ (with config file {path}) started (CTRL+C to exit) ====", extra={"to_logfile": True})

    # instantiate and wire instruments, setup schedules
    instruments = []
    for entry in cfg.get("instruments", []):
        instr = load_instrument(entry, config_path=args.config)
        instruments.append(instr)
        setup_schedules(instr)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()


# import argparse
# import threading
# import time
# from pathlib import Path

# import schedule
# import yaml

# from utils.config import load_config
# from utils.instrument_loader import load_instrument
# from utils.logging import setup_logging


# def run_threaded(job_func):
#     """Set up threading and start job.

#     Args:
#         job_func ([type]): [description]
#     """
#     job_thread = threading.Thread(target=job_func)
#     job_thread.start()

# def main():
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--config", default="config.yaml")
#     parser.add_argument("--simulate", action="store_true")
#     args = parser.parse_args()

#     # load configuration and determine execution mode
#     config = load_config(args.config)
#     simulate = args.simulate or config.get("simulate", False)

#     # setup logging
#     log_file = Path(config.get("local", {}).get("logging", "")) / config.get("logging", {}).get("file_name", "pydaq.log")
#     logger = setup_logging(log_file)

#     # setup paths for data storage and staging
#     data_dir = Path(config.get("local", {}).get("data", "data")).expanduser()
#     staging_dir = Path(config.get("local", {}).get("staging", "staging")).expanduser()

#     # load and configure instruments
#     instruments = []
#     for instr_config in config.get("instruments", []):
#         instr = load_instrument(instr_config, data_dir, staging_dir, simulate=simulate)
#         instruments.append(instr)

#     # setup data transfer clients
#     if config.get("transfer", {}).get("s3", None):
#         from utils.s3fsc import S3FSC
#         s3fsc_config = config["transfer"]["s3fsc"]
#         s3fsc_config["default_prefix"] = staging_dir
#         s3fsc = S3FSC(config=s3fsc_config, logger=logger)
#     if config.get("transfer", {}).get("sftp", None):
#         from utils.sftp import SFTPClient
#         sftp_config = config["transfer"]["sftp"]
#         sftp_config["staging"] = staging_dir
#         sftp = SFTPClient(config=sftp_config, logger=logger)

#     # setup schedules (or include this in the instrument instantiation)
#     # ...

#     # begin data acquisition
#     print("Running pydaq... (CTRL+C to exit)")
#     while True:
#         schedule.run_pending()
#         time.sleep(1)

# if __name__ == "__main__":
#     main()
