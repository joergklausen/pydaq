from __future__ import annotations

import logging
import sys
from types import ModuleType
from typing import cast

try:
    import polars  # noqa: F401
except ModuleNotFoundError:
    # These tests exercise logging only; no dataframe operations are needed.
    sys.modules["polars"] = ModuleType("polars")

from pydaq.instruments.fidas import FIDAS


def _bare_fidas() -> FIDAS:
    driver = cast(FIDAS, FIDAS.__new__(FIDAS))
    driver.__dict__.update(
        {
            "name": "fidas",
            "logger": logging.getLogger("test.fidas.stdout"),
            "_last_parsed": {},
        }
    )
    return driver


def test_aggregate_stdout_uses_at_most_four_significant_digits(caplog) -> None:
    driver = _bare_fidas()
    caplog.set_level(logging.INFO, logger="test.fidas.stdout")

    driver._log_aggregate_summary(
        {
            "60": 462.5377,
            "61": 0.01381234,
            "62": 0.01519876,
            "63": 0.01654321,
            "64": 0.01734567,
            "65": 0.01861234,
        }
    )

    assert caplog.messages == [
        "60=462.5 61=0.01381 62=0.0152 "
        "63=0.01654 64=0.01735 65=0.01861"
    ]


def test_print_readings_uses_same_significant_digit_limit(caplog) -> None:
    driver = _bare_fidas()
    driver._last_parsed = {
        "60": 462.5377,
        "61": 0.01381234,
    }
    caplog.set_level(logging.INFO, logger="test.fidas.stdout")

    driver.print_readings(keys=("60", "61"))

    assert caplog.messages == ["60=462.5; 61=0.01381"]


def test_fidas_declares_driver_owned_status_reporting() -> None:
    assert FIDAS.EMITS_OWN_STATUS is True
