# tests/test_instrument_parquet_simple.py
from __future__ import annotations

import pytest
from pathlib import Path

from instr.thermo import Thermo


@pytest.mark.skipif(__import__("importlib").import_module("importlib").util.find_spec("polars") is None, reason="polars not installed")
def test_parquet_save(tmp_path, monkeypatch):
    import polars as pl
    root = tmp_path / "results"
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "staging").mkdir(parents=True, exist_ok=True)

    cfg = {
        "simulate": True,
        "local": {"root": str(root), "data": "data", "staging": "staging"},
        "instruments": [{"name": "thermo", "class": "instr.thermo.Thermo",
                         "model": "49i",
                         "params": {
                             "communication": "serial",
                             "serial": "COM1",
                             "get_data": "lr00",
                             "filename_extension": "parquet"
                         }}],
    }
    monkeypatch.setattr("instr.instrument.load_config", lambda _: cfg)

    t = Thermo("thermo", "ignored.yaml")
    t.accumulate_dataframe(pl.DataFrame({"a": [1, 2], "b": ["x", "y"]}))
    t.save_data_file()
    assert t._saved_data_path and t._saved_data_path.exists()
    assert t._saved_data_path.suffix == ".parquet"