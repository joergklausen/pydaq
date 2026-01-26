from pathlib import Path
from pydaq.config import load_config

def test_load_example_config():
    config = load_config(Path("configs/mkn.yml"))
    assert config.station.id == "mkn"
    assert "tei49c" in config.instruments
