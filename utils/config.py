"""
Configuration utilities for loading YAML configuration files.

This module provides a function to load a YAML-based configuration file
commonly used in instrument drivers and data acquisition setups.

Typical usage:
    from utils.config import load_yaml_config
    config = load_yaml_config("path/to/config.yaml")
"""

import yaml
from pathlib import Path


def load_yaml_config(config_path: str | Path) -> dict:
    """
    Load and parse a YAML configuration file.

    Args:
        config_path (str | Path): Path to the YAML configuration file.

    Returns:
        dict: Parsed configuration as a dictionary.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
        yaml.YAMLError: If the file cannot be parsed as valid YAML.
    """
    path = Path(config_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r") as f:
        return yaml.safe_load(f)
