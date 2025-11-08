"""Instrument factory and schedule wiring.

Loads drivers based on a config entry and attaches runtime policy:
- driver.initialize() (logs init get/set responses)
- driver.setup_schedules() (sampling + staging)
- aligned 10-minute saves (:00,:10,:20,:30,:40,:50)
- immediate first get_data() so accumulation starts right away
"""

from __future__ import annotations

import importlib
from typing import Dict, Any, Callable

import schedule


def _schedule_aligned_every_10_minutes(job: Callable[[], Any]) -> None:
    """Schedule `job` at aligned :00, :10, :20, :30, :40, :50 each hour."""
    for m in (0, 10, 20, 30, 40, 50):
        schedule.every().hour.at(f":{m:02}").do(job)


def load_instrument(instr_config: Dict[str, Any], config_path: str, simulate: bool = False):
    """Instantiate an instrument driver and attach schedules.

    Parameters
    ----------
    instr_config : dict
        One entry from config['instruments'] (expects 'name' and 'class').
    config_path : str
        Path to the YAML configuration file.
    simulate : bool, optional
        If True, drivers may choose to simulate I/O.

    Returns
    -------
    Instrument
        An initialized driver instance with schedules wired.
    """
    name = instr_config["name"]
    module_name, class_name = instr_config["class"].rsplit(".", 1)
    cls = getattr(importlib.import_module(module_name), class_name)

    instr = cls(name=name, config_path=config_path)

    if hasattr(instr, "initialize") and callable(instr.initialize):
        instr.initialize()

    instr.setup_schedules()
    _schedule_aligned_every_10_minutes(instr.save_data_file)

    # Start accumulating immediately (first file may be partial by design)
    try:
        instr.get_data()
    except Exception:
        # Don't block startup on a transient comms failure
        pass

    return instr


# import importlib
# import logging
# from pathlib import Path

# def load_instrument(instr_config: dict, data_dir: Path|str, staging_dir: Path|str, simulate: bool=False):
#     name = instr_config["name"]
#     class_path = instr_config["class"]
#     model = instr_config.get("model", "")
#     params = instr_config.get("params", {})
#     params["data_dir"] = data_dir
#     params["staging_dir"] = staging_dir

#     if simulate:
#         return MockInstrument(name, **params)

#     module_name, class_name = class_path.rsplit(".", 1)
#     module = importlib.import_module(module_name)
#     cls = getattr(module, class_name)
#     return cls(name=name, **params)


# class MockInstrument:
#     def __init__(self, name, **kwargs):
#         self.name = name
#         self.logger = logging.getLogger(f"Mock:{self.name}")

#     def acquire_data(self):
#         from datetime import datetime
#         self.logger.info(f"[MOCK] {self.name} acquiring fake data at {datetime.now()}")

#     def set_config(self):
#         self.logger.info(f"[MOCK] Configuring {self.name}")