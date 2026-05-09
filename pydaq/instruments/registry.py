from __future__ import annotations

"""Simple driver registry for pydaq instruments.

Use one canonical driver string per instrument family. Keep the mapping explicit and
fail with a clear error when a driver name is unknown.
"""

import importlib
from typing import Type

from pydaq.instruments.instrument import Instrument


# Canonical config values. Keep this list intentionally small.
_DRIVER_MAP: dict[str, str] = {
    "49i": "pydaq.instruments.thermo:Thermo49i",
    "49c": "pydaq.instruments.thermo:Thermo49C",
    "49cps": "pydaq.instruments.thermo:Thermo49CPS",
    "fidas": "pydaq.instruments.fidas:FIDAS",
    "ae31": "pydaq.instruments.magee:AE31",
    "hmpascii": "pydaq.instruments.vaisala:HMPASCII",
    "aurora3000": "pydaq.instruments.ecotech:NEPH",
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
            f"Invalid driver registry entry {spec!r}; expected 'module:Class'."
        ) from exc

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ImportError(
            f"Could not import driver module {module_name!r} for spec {spec!r}: {exc}"
        ) from exc

    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise ImportError(
            f"Driver class {class_name!r} not found in module {module_name!r}."
        ) from exc

    if not isinstance(cls, type) or not issubclass(cls, Instrument):
        raise TypeError(
            f"Resolved driver {spec!r} to {cls!r}, but it is not an Instrument subclass."
        )

    return cls


def get_driver_class(driver: str) -> Type[Instrument]:
    """Resolve a configured driver name to an instrument class.

    Accepted config values are intentionally limited to the canonical names returned by
    :func:`list_drivers`. Resolution is case-insensitive, so ``HMPASCII`` also works,
    but the preferred YAML value is ``hmpascii``.
    """
    if not isinstance(driver, str) or not driver.strip():
        supported = ", ".join(list_drivers())
        raise ValueError(
            f"Instrument driver must be a non-empty string. Supported drivers: {supported}."
        )

    key = driver.strip().lower()
    spec = _DRIVER_MAP.get(key)
    if spec is None:
        supported = ", ".join(list_drivers())
        raise ValueError(
            f"Unknown instrument driver {driver!r}. Supported drivers: {supported}. "
            f"For Vaisala HMP sensors in ASCII mode, use driver: hmpascii"
        )

    return _load_class(spec)
