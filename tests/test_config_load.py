from pathlib import Path
from pydaq.utils.config_handler import load_config

def test_load_example_config():
    config = load_config(Path("pydaq/configs/nrb.yml"))
    assert config.station.id == "nrb"
    assert "49c" in config.instruments
