from __future__ import annotations

"""Shared pytest configuration for pydaq integration tests.

The integration tests use a real station YAML file to build the pydaq
configuration.  On the command line this can still be selected explicitly with
``--station-config``.  When the option is omitted, as is common when tests are
started from VS Code, the fixture falls back to:

1. the ``PYDAQ_STATION_CONFIG`` environment variable;
2. ``./pydaq/configs/nrb.yml`` relative to the repository root.

This makes the tests runnable from different environments without repeating the
same pytest arguments in every VS Code workspace.
"""

import os
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register pydaq-specific pytest options."""
    group = parser.getgroup("pydaq")
    group.addoption(
        "--station-config",
        action="store",
        default=os.environ.get("PYDAQ_STATION_CONFIG"),
        metavar="PATH",
        help=(
            "Path to a pydaq station YAML config. If omitted, pytest uses "
            "PYDAQ_STATION_CONFIG or ./pydaq/configs/nrb.yml when present."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Document custom markers so pytest does not warn about them."""
    config.addinivalue_line(
        "markers",
        "integration: tests that touch real services, network resources, hardware, or station config",
    )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the repository root inferred from this conftest file."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def station_config_path(pytestconfig: pytest.Config, repo_root: Path) -> Path:
    """Return the station config path for integration tests.

    Precedence:
    1. ``--station-config`` command-line option;
    2. ``PYDAQ_STATION_CONFIG`` environment variable;
    3. ``pydaq/configs/nrb.yml`` in the current repository.

    If no usable file is found, tests that require station configuration are
    skipped with a clear reason rather than failing during collection.
    """
    configured = pytestconfig.getoption("--station-config", default=None)
    if configured:
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            path = repo_root / path
        path = path.resolve()
        if not path.exists():
            pytest.skip(f"Station config path does not exist: {path}")
        return path

    default_path = repo_root / "pydaq" / "configs" / "nrb.yml"
    if default_path.exists():
        return default_path.resolve()

    pytest.skip(
        "No station config provided. Use --station-config, set PYDAQ_STATION_CONFIG, "
        "or add pydaq/configs/nrb.yml."
    )
