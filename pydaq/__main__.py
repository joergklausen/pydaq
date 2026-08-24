"""Command-line entry point for pydaq."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from pydaq.pydaq import Orchestrator
from pydaq.utils.single_instance_lock import AlreadyRunningError, SingleInstanceLock


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the pydaq data acquisition system.")
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="Path to the station YAML configuration file.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    config_path = Path(args.config).expanduser().resolve(strict=False)

    # Acquire the application lock before constructing Orchestrator.  This is
    # deliberately earlier than logging setup, transfer self-test, dashboard
    # startup, or instrument initialization, because all of those resources may
    # conflict with an already-running pydaq process.
    lock = SingleInstanceLock(config_path)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        # Do not use pydaq logging here: the running instance may already own
        # the rotating log file that this guard is intended to protect.
        print(f"ERROR: {exc}; exiting.", file=sys.stderr)
        return 2

    try:
        Orchestrator(config_path=config_path).run_forever()
    finally:
        lock.release()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
