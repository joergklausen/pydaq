from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register custom command-line options for integration tests."""
    parser.addoption(
        "--station-config",
        action="store",
        default=None,
        help="Path to the station.yml file used by integration tests.",
    )


@pytest.fixture(scope="session")
def station_config_path(pytestconfig: pytest.Config) -> Path:
    """Return the station config path passed on the command line.

    The test is skipped when no path is provided or the file does not exist.
    """
    raw = pytestconfig.getoption("station_config")
    if not raw:
        pytest.skip("No --station-config path was provided.")

    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        pytest.skip(f"station config file not found: {path}")

    return path