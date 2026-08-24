#!/usr/bin/env bash
set -euo pipefail

# Thin launcher for cron/systemd/manual use.  pydaq itself owns the
# single-instance lock; do not add ps/pgrep/flock process heuristics here.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
PYTHON_EXE="$REPO_DIR/.venv/bin/python"

usage() {
    echo "Usage: pydaq.sh CONFIG" >&2
    echo "       pydaq.sh -c CONFIG" >&2
}

if [[ $# -eq 0 ]]; then
    usage
    exit 2
fi

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    -c|--config)
        if [[ $# -lt 2 ]]; then
            usage
            exit 2
        fi
        CFG_IN="$2"
        ;;
    *)
        CFG_IN="$1"
        ;;
esac

if [[ ! -x "$PYTHON_EXE" ]]; then
    echo "ERROR: missing venv python at $PYTHON_EXE" >&2
    exit 1
fi

if [[ "$CFG_IN" = /* ]]; then
    CFG_ABS="$CFG_IN"
else
    CFG_ABS="$REPO_DIR/$CFG_IN"
fi

if [[ ! -f "$CFG_ABS" ]]; then
    echo "ERROR: config file not found: $CFG_ABS" >&2
    exit 1
fi

cd "$REPO_DIR"
exec "$PYTHON_EXE" -u -m pydaq -c "$CFG_ABS"
