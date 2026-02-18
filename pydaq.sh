#!/usr/bin/env bash
set -euo pipefail

BASE="$HOME/Documents/git/pydaq"
CFG="pydaq/configs/buc.yml"
PY="$BASE/.venv/bin/python"

LOCK="/tmp/pydaq-buc.lock"

# Ensure only one instance (lock is held for the lifetime of this script/process)
exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

# Extra safety: don't launch if an instance is already running (even if started elsewhere)
if pgrep -f "python.*-m[[:space:]]+pydaq.*-c[[:space:]]+.*${CFG}" >/dev/null 2>&1; then
  exit 0
fi

cd "$BASE"
exec "$PY" -m pydaq -c "$CFG"
