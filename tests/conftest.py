from __future__ import annotations

"""Shared pytest configuration for pydaq tests.

Live integration tests use real station configuration, instruments, network
services, credentials, private keys, and remote storage. They are therefore
disabled during an ordinary pytest or VS Code test run.

Enable them explicitly with either:

    python -m pytest --run-integration

or:

    PYDAQ_RUN_INTEGRATION=1

A station configuration can be selected with ``--station-config`` or the
``PYDAQ_STATION_CONFIG`` environment variable.
"""

import os
from pathlib import Path

import pytest


_TRUTHY_VALUES = {"1", "true", "yes", "on"}

def _environment_flag(name: str) -> bool:
    """Return whether an environment variable contains a truthy value."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY_VALUES


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
    group.addoption(
        "--run-integration",
        action="store_true",
        default=_environment_flag("PYDAQ_RUN_INTEGRATION"),
        help=(
            "Run tests that access real instruments, network services, "
            "credentials, private keys, or other station resources."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        (
            "integration: tests that touch real services, network resources, "
            "hardware, credentials, or station configuration"
        ),
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip live integration tests unless explicitly enabled."""
    if bool(config.getoption("run_integration")):
        return

    skip_live = pytest.mark.skip(
        reason=(
            "live integration test; pass --run-integration or set "
            "PYDAQ_RUN_INTEGRATION=1 to enable"
        )
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the repository root inferred from this conftest file."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def station_config_path(
    pytestconfig: pytest.Config,
    repo_root: Path,
) -> Path:
    """Return the station configuration used by integration tests.

    Precedence:

    1. ``--station-config``;
    2. ``PYDAQ_STATION_CONFIG``;
    3. ``pydaq/configs/nrb.yml`` in the current repository.
    """
    configured = pytestconfig.getoption("station_config", default=None)
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
        "No station config provided. Use --station-config, set "
        "PYDAQ_STATION_CONFIG, or add pydaq/configs/nrb.yml."
    )
