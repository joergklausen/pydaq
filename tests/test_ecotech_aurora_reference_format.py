from __future__ import annotations

from datetime import datetime
import inspect
import logging
from typing import Any, Iterator, cast

from pydaq.instruments.ecotech import NEPH
from pydaq.instruments.instrument import TimeBucketAggregator


AURORA_FIELDS = [field for field in NEPH.HEADERS if field != "dtm"]


def _bare_aurora_driver() -> NEPH:
    """Create a NEPH instance without running Instrument.__init__ or opening IO."""
    driver = cast(NEPH, NEPH.__new__(NEPH))
    driver.__dict__.update(
        {
            "name": "aurora3000",
            "logger": logging.getLogger("test.ecotech.aurora3000"),
        }
    )
    return driver


def _aurora_sample(dtm: datetime, *, ssp1: float, dio_state: int = 7) -> dict[str, Any]:
    """Return one complete instantaneous Aurora sample in pydaq field order."""
    return {
        "dtm": dtm,
        "ssp1": ssp1,
        "ssp2": 1.0,
        "ssp3": 1.0,
        "sbsp1": 1.0,
        "sbsp2": 1.0,
        "sbsp3": 1.0,
        "sample_temp": 1.0,
        "enclosure_temp": 1.0,
        "RH": 1.0,
        "pressure": 1.0,
        "major_state": 0.0,
        "DIO_state": dio_state,
    }


class _FakeAuroraNEPH(NEPH):
    """NEPH test double that feeds predefined samples and never touches IO."""

    def __init__(self, samples: list[dict[str, Any]]) -> None:
        logger = logging.getLogger("test.ecotech.fake_aurora")
        self.__dict__.update({"name": "aurora3000", "logger": logger})
        aggregator = self._build_aggregator(
            schedule_cfg={"aggregation_period_minutes": 1, "aggregation_timestamp": "end"},
            processing_cfg={"aggregation_method": "mean"},
            aggregate_cfg={},
        )
        self.__dict__.update(
            {
                "aggregator": aggregator,
                "empty_record_is_ok": True,
                "_samples": iter(samples),
            }
        )

    def get_current_sample(self) -> dict[str, Any]:
        samples = cast(Iterator[dict[str, Any]], self.__dict__["_samples"])
        return next(samples)


def test_ecotech_driver_uses_shared_time_bucket_aggregator() -> None:
    """Guard against reintroducing the old local aggregation block."""
    source = inspect.getsource(NEPH)

    assert "TimeBucketAggregator" in source
    assert "self.aggregator.add(sample)" in source
    assert "def _append_to_aggregate" not in source
    assert "def _aggregate" + "_records" not in source
    assert "numeric = [float(v)" not in source
    assert "float(v)" not in source
    assert "type:" + " ignore" not in source


def test_aurora_format_matches_reference_row() -> None:
    driver = _bare_aurora_driver()
    row: dict[str, Any] = {
        "dtm": datetime(2026, 4, 10, 9, 2, 0),
        "ssp1": 21.788,
        "ssp2": 26.580,
        "ssp3": 33.602,
        "sbsp1": 3.927,
        "sbsp2": 42490956.000,
        "sbsp3": 6.238,
        "sample_temp": 30.547,
        "enclosure_temp": 30.745,
        "RH": 38.181,
        "pressure": 815.167,
        "major_state": 0.0,
        "DIO_state": 7.0,
    }

    formatted = driver._format_record(row)
    line = ",".join(str(formatted[field]) for field in NEPH.HEADERS)

    assert line == (
        "2026-04-10T09:02:00,21.788,26.580,33.602,3.927,"
        "42490956.000,6.238,30.547,30.745,38.181,815.167,0.000,7.000"
    )


def test_aurora_builds_time_bucket_aggregator_from_pydaq_schedule_config() -> None:
    driver = _bare_aurora_driver()

    aggregator = driver._build_aggregator(
        schedule_cfg={"aggregation_period_minutes": 1, "aggregation_timestamp": "end"},
        processing_cfg={"aggregation_method": "mean"},
        aggregate_cfg={},
    )

    assert isinstance(aggregator, TimeBucketAggregator)
    assert aggregator.period_seconds == 60
    assert aggregator.datetime_field == "dtm"
    assert aggregator.timestamp == "end"
    assert aggregator.default_method == "mean"


def test_aurora_get_record_emits_legacy_1_minute_mean_after_bucket_closes() -> None:
    driver = _FakeAuroraNEPH(
        [
            _aurora_sample(datetime(2026, 4, 10, 9, 10, 0), ssp1=1.0, dio_state=7),
            _aurora_sample(datetime(2026, 4, 10, 9, 10, 5), ssp1=2.0, dio_state=7),
            _aurora_sample(datetime(2026, 4, 10, 9, 10, 10), ssp1=3.0, dio_state=6),
            _aurora_sample(datetime(2026, 4, 10, 9, 11, 0), ssp1=99.0, dio_state=7),
        ]
    )

    assert driver.get_record() == {}
    assert driver.get_record() == {}
    assert driver.get_record() == {}

    formatted = driver.get_record()

    assert formatted["dtm"] == "2026-04-10T09:11:00"
    assert formatted["ssp1"] == "2.000"
    # Legacy Aurora reference files average the state columns numerically too.
    assert formatted["DIO_state"] == "6.667"


def test_aurora_dio_state_is_decoded_as_hex_word() -> None:
    assert NEPH._parse_hex_or_int("0007") == 7
    assert NEPH._parse_hex_or_int("0010") == 16
    assert NEPH._parse_hex_or_int("0x10") == 16
