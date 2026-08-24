from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pydaq.instruments.picarro import G2401
from pydaq.instruments.registry import get_driver_class, list_drivers


def _driver(tmp_path: Path, **io_overrides) -> G2401:
    source = tmp_path / "source"
    io = {
        "kind": "tcp",
        "host": "192.0.2.1",
        "port": 51020,
        "timeout_seconds": 0.1,
        "sleep_seconds": 0,
        "source_path": str(source),
        "file_pattern": "CFKADS2320-*-DataLog_User_Sync.dat",
        "buckets": "daily",
        "days_to_sync": 2,
        "min_file_age_seconds": 3600,
        "file_scan_every_seconds": 600,
        "zip_files": True,
    }
    io.update(io_overrides)
    return G2401(
        name="g2401",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logging.getLogger("test.pydaq"),
        parameters={"io": io, "init": {}, "processing": {}, "output": {}},
    )


def _make_source_file(
    tmp_path: Path,
    *,
    now: datetime,
    filename: str = "CFKADS2320-20260824-070000Z-DataLog_User_Sync.dat",
    age_seconds: int = 7200,
    payload: bytes = b"DATE TIME CO2 CH4 CO\n2026-08-24 07:00:00 420 1.9 0.1\n",
) -> Path:
    bucket = tmp_path / "source" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
    bucket.mkdir(parents=True, exist_ok=True)
    path = bucket / filename
    path.write_bytes(payload)
    mtime = now.timestamp() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def test_registry_contains_and_resolves_g2401() -> None:
    assert "g2401" in list_drivers()
    assert "picarro" in list_drivers()
    assert get_driver_class("g2401") is G2401
    assert get_driver_class("PICARRO") is G2401


def test_g2401_has_no_pydaq_measurement_writer(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    assert driver.writer is None


def test_parse_concentrations() -> None:
    assert G2401.parse_concentrations("421.234;1.9234;0.10987;ignored") == pytest.approx(
        (421.234, 1.9234, 0.10987)
    )


@pytest.mark.parametrize("response", ["", "1;2", "1;;3", "foo;2;3"])
def test_parse_concentrations_rejects_bad_response(response: str) -> None:
    with pytest.raises(ValueError):
        G2401.parse_concentrations(response)


def test_sync_archives_and_stages_completed_picarro_file(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    source = _make_source_file(tmp_path, now=now)
    driver = _driver(tmp_path)

    staged = driver.sync_files(now=now)

    archive = tmp_path / "data" / "2026" / "08" / "24" / source.name
    outbox = tmp_path / "outbox" / source.with_suffix(".zip").name
    assert staged == [outbox]
    assert archive.read_bytes() == source.read_bytes()
    assert outbox.exists()
    with zipfile.ZipFile(outbox) as handle:
        assert handle.namelist() == [source.name]
        assert handle.read(source.name) == source.read_bytes()


def test_sync_skips_file_that_may_still_be_written(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    _make_source_file(tmp_path, now=now, age_seconds=120)
    driver = _driver(tmp_path)

    assert driver.sync_files(now=now) == []
    assert list((tmp_path / "outbox").glob("*.zip")) == []


def test_state_prevents_restage_after_successful_transfer_removed_outbox(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    source = _make_source_file(tmp_path, now=now)
    driver = _driver(tmp_path)

    first = driver.sync_files(now=now)
    assert len(first) == 1
    first[0].unlink()  # simulate remove_on_success after transfer

    assert driver.sync_files(now=now) == []
    assert not (tmp_path / "outbox" / source.with_suffix(".zip").name).exists()

    state = json.loads((tmp_path / "data" / ".picarro_file_state.json").read_text())
    assert len(state["files"]) == 1


def test_sync_recovers_when_archive_exists_but_state_was_not_committed(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    source = _make_source_file(tmp_path, now=now)
    archive = tmp_path / "data" / "2026" / "08" / "24" / source.name
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(source.read_bytes())
    driver = _driver(tmp_path)

    staged = driver.sync_files(now=now)

    assert len(staged) == 1
    assert staged[0].exists()


def test_sync_raises_meaningful_error_for_missing_file_source(tmp_path: Path) -> None:
    driver = _driver(tmp_path)
    with pytest.raises(FileNotFoundError, match="Picarro data source is not accessible"):
        driver.sync_files()


def test_file_scan_error_is_rate_limited_and_does_not_raise(tmp_path: Path, caplog) -> None:
    driver = _driver(tmp_path)
    caplog.set_level(logging.DEBUG)

    driver._run_file_scan_if_due(force=True)
    driver._run_file_scan_if_due(force=True)

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "file source unavailable" in errors[0].getMessage()


def test_print_status_is_stdout_only_and_does_not_create_measurement_file(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    driver = _driver(tmp_path)
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(driver, "_query_status", lambda: "421.234;1.9234;0.10987;rest")

    driver.print_status()

    assert any(
        "CO2=421.2 ppm CH4=1.923 ppm CO=0.1099 ppm" in record.getMessage()
        for record in caplog.records
    )
    assert list((tmp_path / "data").glob("*.csv")) == []
    assert list((tmp_path / "data").glob("*.zip")) == []
    assert list((tmp_path / "outbox").glob("*.zip")) == []


def test_status_failure_is_reported_once_then_suppressed(tmp_path: Path, monkeypatch, caplog) -> None:
    driver = _driver(tmp_path)
    caplog.set_level(logging.DEBUG)

    def fail():
        raise TimeoutError("timed out")

    monkeypatch.setattr(driver, "_query_status", fail)
    driver.print_status()
    driver.print_status()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "status unavailable tcp 192.0.2.1:51020" in warnings[0].getMessage()


def test_status_recovers_after_failure(tmp_path: Path, monkeypatch, caplog) -> None:
    driver = _driver(tmp_path)
    caplog.set_level(logging.INFO)

    responses = iter([TimeoutError("timed out"), "420;1.9;0.1"])

    def query():
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(driver, "_query_status", query)
    driver.print_status()
    driver.print_status()

    messages = [record.getMessage() for record in caplog.records]
    assert any("Picarro status recovered" in message for message in messages)
    assert any("CO2=420 ppm CH4=1.9 ppm CO=0.1 ppm" in message for message in messages)


def test_query_status_sends_legacy_command_with_crlf(tmp_path: Path, monkeypatch) -> None:
    driver = _driver(tmp_path)

    class FakeSocket:
        def __init__(self):
            self.sent = b""
            self.responses = iter([b"421.2;1.91;0.10;extra\r\n"])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, payload):
            self.sent += payload

        def recv(self, size):
            try:
                return next(self.responses)
            except StopIteration:
                return b""

    fake = FakeSocket()

    def create_connection(address, timeout):
        assert address == ("192.0.2.1", 51020)
        assert timeout == pytest.approx(0.1)
        return fake

    monkeypatch.setattr("pydaq.instruments.picarro.socket.create_connection", create_connection)

    assert driver._query_status() == "421.2;1.91;0.10;extra"
    assert fake.sent == b"_Meas_GetConc\r\n"


def test_legacy_netshare_fallback_builds_unc_source(tmp_path: Path) -> None:
    driver = G2401(
        name="g2401",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logging.getLogger("test.pydaq"),
        parameters={
            "io": {
                "host": "192.168.4.102",
                "port": 51020,
                "netshare": "DataLog_User_Sync",
                "buckets": "daily",
            }
        },
    )
    assert str(driver.source_path) == r"\\192.168.4.102\DataLog_User_Sync"
