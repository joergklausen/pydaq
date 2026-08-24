from __future__ import annotations

"""Simple driver registry for pydaq instruments.

Use one canonical driver string per instrument family. Keep the mapping explicit
and fail with a clear error when a driver name is unknown.
"""

import importlib
from typing import Type

from pydaq.instruments.instrument import Instrument
# Canonical config values. Keep this list intentionally small.
_DRIVER_MAP: dict[str, str] = {
    "49i": "pydaq.instruments.thermo:Thermo49i",
    "tei49i": "pydaq.instruments.thermo:Thermo49i",
    "thermo49i": "pydaq.instruments.thermo:Thermo49i",
    "49c": "pydaq.instruments.thermo:Thermo49C",
    "tei49c": "pydaq.instruments.thermo:Thermo49C",
    "thermo49c": "pydaq.instruments.thermo:Thermo49C",
    "49cps": "pydaq.instruments.thermo:Thermo49CPS",
    "tei49cps": "pydaq.instruments.thermo:Thermo49CPS",
    "thermo49cps": "pydaq.instruments.thermo:Thermo49CPS",
    "fidas": "pydaq.instruments.fidas:FIDAS",
    "ae31": "pydaq.instruments.magee:AE31",
    "ae33": "pydaq.instruments.magee:AE33",
    "hmpascii": "pydaq.instruments.vaisala:HMPASCII",
    "aurora3000": "pydaq.instruments.ecotech:NEPH",
    "ne300": "pydaq.instruments.ecotech:NE300",
    "avo": "pydaq.instruments.avo:AVO",
    "meteo": "pydaq.instruments.meteo:METEO",
    "g2401": "pydaq.instruments.picarro:G2401",
    "picarro": "pydaq.instruments.picarro:G2401",
}

def list_drivers() -> list[str]:
    """Return supported canonical driver names."""
    return sorted(_DRIVER_MAP)


def _load_class(spec: str) -> Type[Instrument]:
    """Load ``module:Class`` and validate it is an Instrument subclass."""
    try:
        module_name, class_name = spec.split(":", 1)
    except ValueError as exc:
        raise ImportError(
            f"Invalid driver registry entry {spec!r}; "
            "expected 'module:Class'."
        ) from exc
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ImportError(
            f"Could not import driver module {module_name!r} "
            f"for spec {spec!r}: {exc}"
        ) from exc

    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise ImportError(
            f"Driver class {class_name!r} not found "
            f"in module {module_name!r}."
        ) from exc
    if not isinstance(cls, type) or not issubclass(cls, Instrument):
        raise TypeError(
            f"Resolved driver {spec!r} to {cls!r}, "
            "but it is not an Instrument subclass."
        )

    return cls

def get_driver_class(driver: str) -> Type[Instrument]:
    """Resolve a configured driver name to an instrument class."""
    if not isinstance(driver, str) or not driver.strip():
        supported = ", ".join(list_drivers())
        raise ValueError(
            "Instrument driver must be a non-empty string. "
            f"Supported drivers: {supported}."
        )
    key = driver.strip().lower()
    spec = _DRIVER_MAP.get(key)
    if spec is None:
        supported = ", ".join(list_drivers())
        raise ValueError(
            f"Unknown instrument driver {driver!r}. "
            f"Supported drivers: {supported}. "
            "For Vaisala HMP sensors in ASCII mode, use driver: hmpascii"
        )

    return _load_class(spec)
