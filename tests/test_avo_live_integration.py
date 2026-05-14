from __future__ import annotations

"""Live integration test for the pydaq AVO downloader.

This test uses one real AVO URL from ``--station-config`` but writes only to a
pytest temporary directory.  It is intended for field-system checks where the
network should be available.

Example:

    pytest -vv -rs -s tests/test_avo_live_integration.py \
        --station-config ./pydaq/configs/nrb.yml
"""

import logging
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

pytest.importorskip("polars")
pytest.importorskip("requests")

from pydaq.instruments.avo import AVO


def _station_config_path(pytestconfig: pytest.Config) -> Path:
    value = pytestconfig.getoption("--station-config", default=None)
    if value:
        return Path(str(value)).expanduser()
    default = Path("pydaq/configs/nrb.yml")
    if default.exists():
        return default
    pytest.skip("No --station-config supplied and pydaq/configs/nrb.yml not found.")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise AssertionError(f"Station config is not a mapping: {path}")
    return data


def _avo_config(config: Mapping[str, Any]) -> dict[str, Any]:
    instruments = config.get("instruments")
    if isinstance(instruments, Mapping) and isinstance(instruments.get("avo"), Mapping):
        return dict(instruments["avo"])
    if isinstance(config.get("avo"), Mapping):
        return dict(config["avo"])
    pytest.skip("Station config has no instruments.avo block.")


def _first_url_only(avo_cfg: Mapping[str, Any]) -> dict[str, Any]:
    io_cfg = dict(avo_cfg.get("io") or {})
    raw_urls = io_cfg.get("urls") or io_cfg.get("sources")
    if not isinstance(raw_urls, Mapping) or not raw_urls:
        pytest.skip("AVO config has no io.urls mapping.")

    first_name = next(iter(raw_urls))
    first_value = raw_urls[first_name]
    io_cfg["urls"] = {first_name: first_value}

    return {
        "io": io_cfg,
        "processing": {"datasets": ["instant"], "append": False, "remove_duplicates": True},
        "output": {"data_path": "avo", "staging_path": "avo"},
    }


@pytest.mark.integration
def test_live_avo_url_downloads_and_writes_parquet(tmp_path: Path, pytestconfig: pytest.Config) -> None:
    """Download one live AVO source and verify that a Parquet file is staged."""
    station_config = _load_yaml(_station_config_path(pytestconfig))
    params = _first_url_only(_avo_config(station_config))

    driver = AVO(
        name="avo",
        data_dir=tmp_path / "data",
        outbox_dir=tmp_path / "outbox",
        logger=logging.getLogger("test"),
        parameters=params,
    )
    driver.initialize()
    summary = driver.get_record()

    assert summary["errors"] == ""
    assert summary["sources_total"] == 1
    assert summary["sources_ok"] == 1
    assert summary["files_written"] >= 1
    assert summary["rows_written"] >= 1

    data_files = sorted((tmp_path / "data" / "avo").glob("*_avo_instant-*.parquet"))
    staged_files = sorted((tmp_path / "outbox" / "avo").glob("*_avo_instant-*.parquet"))
    assert data_files, f"No AVO parquet file was written. Summary: {summary}"
    assert [path.name for path in data_files] == [path.name for path in staged_files]
