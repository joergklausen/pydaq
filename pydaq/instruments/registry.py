"""Instrument driver registry (lazy import).

Drivers are registered by a short key (e.g. ``49c``) mapped to ``module:Class``.
Imports are performed lazily to keep start-up quick and to avoid importing optional dependencies
unless a driver is enabled.

Add new drivers by extending ``_DRIVER_REGISTRY``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Dict, Tuple

from pydaq.instruments.instrument import Instrument


_DRIVER_REGISTRY: Dict[str, Tuple[str, str]] = {
    "49c": ("instruments.thermo", "Thermo49C"),
    "49i": ("instruments.thermo", "Thermo49i"),
    "49cps": ("instruments.thermo", "Thermo49CPS"),
    "ne300": ("instruments.ecotech", "NEPH"),
    "ae33": ("instruments.magee", "AE33"),
    "hmp110": ("instruments.vaisala", "HMP110ASCII"),
    "g2401": ("instruments.picarro", "G2401"),
    "meteo": ("instruments.meteo", "METEO"),
    "tapo": ("instruments.tapo", "TapoC230"),
    "fidas": ("instruments.pallas", "FIDAS"),
}


def list_drivers() -> Dict[str, str]:
    """List available drivers.

    Returns:
        Mapping of driver key -> ``module:Class`` string.
    """
    return {key: f"{module}:{class_name}" for key, (module, class_name) in _DRIVER_REGISTRY.items()}


def get_driver_class(driver_key: str) -> type[Instrument]:
    """Resolve a driver key to a Python class.

    Args:
        driver_key: Registry key (e.g. ``thermo49c``).

    Returns:
        The driver class (a subclass of ``Instrument``).

    Raises:
        KeyError: If the driver key is not registered.
        AttributeError: If the module does not contain the expected class.
    """
    key = driver_key.strip().lower()
    if key not in _DRIVER_REGISTRY:
        raise KeyError(f"unknown driver '{driver_key}'. known: {sorted(_DRIVER_REGISTRY)}")
    module_name, class_name = _DRIVER_REGISTRY[key]
    module = import_module(module_name)
    return getattr(module, class_name)
