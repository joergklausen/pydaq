"""Offline tests, including the real October 2026 TAHMO response fixture."""
from __future__ import annotations

import csv
import io
import json
import logging
import sys
import types
from pathlib import Path
from zipfile import ZipFile

# The API/downloader tests can run outside the full pydaq repository.
# When installed in pydaq, the real Instrument base is used instead.
try:
    from pydaq.instruments.instrument import Instrument
except ImportError:
    for package in ("pydaq", "pydaq.instruments"):
        if package not in sys.modules:
            module = types.ModuleType(package)
            module.__path__ = [str(Path(__file__).resolve().parents[1] / package.replace('.', '/'))]
            sys.modules[package] = module
    stub = types.ModuleType("pydaq.instruments.instrument")
    stub.Instrument = type("Instrument", (), {})
    sys.modules["pydaq.instruments.instrument"] = stub

from pydaq.instruments.tahmo import (  # noqa: E402
    ReadingBatch,
    TahmoConfig,
    TahmoDownloader,
    flatten_readings,
    write_batch_zip,
)

FIXTURE = Path(__file__).with_name("tahmo_sample.json")


def _sample() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_real_response_has_complete_records_and_quality() -> None:
    payload = _sample()
    rows = flatten_readings(payload)
    assert len(payload["data"]) == 19
    assert len(rows) == 190
    assert len({r["mrid"] for r in rows}) == 10
    assert {r["mrid"] for r in rows} == set(range(651600, 651610))
    pressure = next(r for r in rows if r["measurement"] == "Atmospheric Pressure")
    assert pressure["value"] is None
    assert pressure["error_flag"] is True
    assert pressure["error_description"] == "Sensor is reporting an error"
    assert "DEGRADED_OUTPUT" in pressure["metadata_errors"]
    assert pressure["timestamp_utc"] == 1791760500
    assert pressure["tz_offset"] == 10800


def test_zip_retains_null_values_and_errors(tmp_path: Path) -> None:
    rows = flatten_readings(_sample())
    zip_path = tmp_path / "data.zip"
    write_batch_zip(batch=ReadingBatch(tuple(rows)), path=zip_path)
    with ZipFile(zip_path) as zf:
        assert len(zf.namelist()) == 1
        reader = csv.DictReader(io.StringIO(zf.read(zf.namelist()[0]).decode()))
        output = list(reader)
    assert len(output) == 190
    pressure = next(r for r in output if r["measurement"] == "Atmospheric Pressure")
    assert pressure["value"] == ""
    assert pressure["error_flag"] == "True"


class FakeClient:
    def __init__(self, responses: dict[int, ReadingBatch], fail_page: int | None = None):
        self.responses = responses
        self.fail_page = fail_page
        self.calls: list[dict] = []

    def fetch_readings(self, **kwargs):
        self.calls.append(kwargs)
        page = kwargs["page_num"]
        if page == self.fail_page:
            raise ConnectionError("network outage")
        return self.responses[page]


def _page(mrid: int, next_page: bool) -> ReadingBatch:
    row = dict(flatten_readings(_sample())[0])
    row["mrid"] = mrid
    row["timestamp_utc"] = 1791760500 + mrid
    return ReadingBatch((row,), {"next_url": "http://127.0.0.1:8000/anything" if next_page else None,
                                 "page_num_readings": 1})


def _downloader(tmp_path: Path, client: FakeClient, **overrides):
    config = TahmoConfig(device_sn="z6-02149", per_page=10, max_pages=10,
                         request_delay_seconds=0, **overrides)
    return TahmoDownloader(name="tahmo", data_dir=tmp_path / "data",
                           outbox_dir=tmp_path / "outbox", config=config,
                           logger=logging.getLogger("test.tahmo"), client=client)


def test_pages_out_of_order_saved_before_cursor(tmp_path: Path) -> None:
    # The observed API serves newer records on page 1, older ones on page 2.
    client = FakeClient({1: _page(102, True), 2: _page(101, False)})
    dl = _downloader(tmp_path, client)
    path = dl.fetch_once()
    assert path is not None and path.exists()
    assert [c["page_num"] for c in client.calls] == [1, 2]
    state = json.loads(dl.state_path.read_text())
    assert state["devices"]["z6-02149"]["last_mrid"] == 102
    staged = list((tmp_path / "outbox").rglob("*.zip"))
    assert len(staged) == 1
    with ZipFile(staged[0]) as zf:
        output = list(csv.DictReader(io.StringIO(zf.read(zf.namelist()[0]).decode())))
    assert [int(r["mrid"]) for r in output] == [101, 102]


def test_interrupted_pagination_does_not_advance_state(tmp_path: Path) -> None:
    client = FakeClient({1: _page(102, True), 2: _page(101, False)}, fail_page=2)
    dl = _downloader(tmp_path, client)
    assert dl.fetch_once() is None
    assert not dl.state_path.exists()
    assert not list((tmp_path / "data").rglob("*.zip"))
    client.fail_page = None
    assert dl.fetch_once() is not None
    assert json.loads(dl.state_path.read_text())["devices"]["z6-02149"]["last_mrid"] == 102


def test_second_run_starts_after_committed_cursor(tmp_path: Path) -> None:
    client = FakeClient({1: _page(102, False)})
    dl = _downloader(tmp_path, client)
    assert dl.fetch_once() is not None
    client.responses = {1: ReadingBatch((), {"next_url": None, "page_num_readings": 0})}
    assert dl.fetch_once() is None
    assert client.calls[-1]["start_mrid"] == 103


def test_cap_prevents_partial_commit(tmp_path: Path) -> None:
    client = FakeClient({1: _page(102, True)})
    dl = _downloader(tmp_path, client)
    dl.config = TahmoConfig(device_sn="z6-02149", max_pages=1,
                            request_delay_seconds=0)
    assert dl.fetch_once() is None
    assert not dl.state_path.exists()
