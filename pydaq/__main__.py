"""Module entry point.

Example:
    python -m pydaq -c pydaq/configs/mkn.yml
    python -m pydaq -c pydaq/configs/buc.yml
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pydaq.pydaq import Orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(prog="pydaq", description="PYDAQ data acquisition system for atmospheric monitoring")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("pydaq/configs/nrb.yml"),
        help="Path to station YAML config (e.g. pydaq/configs/nrb.yml)",
    )
    args = parser.parse_args()

    Orchestrator(config_path=args.config).run_forever()


if __name__ == "__main__":
    main()
