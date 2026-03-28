"""pydaq.instruments.registry

Backward-compatible driver registry helpers.
"""

from __future__ import annotations

from pydaq.instruments.instrument import get_driver_class as _get_driver_class


def list_drivers() -> list[str]:
    """Return known short driver names."""
    return sorted(
        [
            "49i",
            "thermo49i",
            "49c",
            "thermo49c",
            "49cps",
            "thermo49cps",
            "fidas",
            "FIDAS",
        ]
    )


def get_driver_class(driver: str):
    """Resolve a driver string to its instrument class."""
    aliases = {
        "49c": "pydaq.instruments.thermo:Thermo49C",
        "thermo49c": "pydaq.instruments.thermo:Thermo49C",
        "49cps": "pydaq.instruments.thermo:Thermo49CPS",
        "thermo49cps": "pydaq.instruments.thermo:Thermo49CPS",
        "fidas": "pydaq.instruments.fidas:FIDAS",
        "FIDAS": "pydaq.instruments.fidas:FIDAS",
    }
    return _get_driver_class(driver, aliases=aliases)