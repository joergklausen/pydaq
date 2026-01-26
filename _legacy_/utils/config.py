"""
Configuration utilities for loading YAML configuration files.

This module provides a function to load a YAML-based configuration file
commonly used in instrument drivers and data acquisition setups.

Typical usage:
    from utils.config import load_config
    config = load_config("path/to/config.yaml")
"""

import yaml
from pathlib import Path


def load_config(path: str | Path) -> dict:
    """
    Load and parse a YAML configuration file.

    Args:
        path (str | Path): Expanded path to the YAML configuration file.

    Returns:
        dict: Parsed configuration as a dictionary.

    Raises:
        FileNotFoundError: If the configuration file does not exist.
        yaml.YAMLError: If the file cannot be parsed as valid YAML.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file  {path} not found.")
    if not path.name.endswith(('yaml', 'yml', 'YAML', 'YML')):
        raise ValueError(f"Extension of configuration file {path} not recognized.")
    with open(path, 'r') as fh:
        config = yaml.safe_load(fh)
    return config