#!/usr/bin/env bash

# pydaq.sh — cron-friendly launcher for pydaq
# - activates the local .venv
# - prevents double-starts (per config) using flock + pgrep
# - runs: python -m pydaq -c <config>

set -euo pipefail

# cron has a very small PATH
export PATH="/usr/local/bin:/usr/bin:/bin"

# Repo dir = directory where this script lives (place pydaq.sh in repo root)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${SCRIPT_DIR}"
VENV_DIR="${REPO_DIR}/.venv"

ts() { date +"%Y-%m-%dT%H:%M:%S"; }
log() { echo "$(ts), INFO, pydaq.sh, $*"; }
err() { echo "$(ts), ERROR, pydaq.sh, $*" >&2; }

usage() {
  cat <<USAGE
Usage:
  ./pydaq.sh [CONFIG]
  ./pydaq.sh -c CONFIG

Examples:
  ./pydaq.sh pydaq/configs/buc.yml
  ./pydaq.sh -c pydaq/configs/other_site.yml

Notes:
  - CONFIG may be absolute or relative to the repo root.
  - One running instance per CONFIG is allowed.
USAGE
}

# ---- parse args (minimal) ----
CFG_IN=""
if [[ ${#} -eq 0 ]]; then
  CFG_IN="pydaq/configs/nrb.yml"
elif [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
elif [[ ${1:-} == "-c" || ${1:-} == "--config" ]]; then
  CFG_IN="${2:-}"
  if [[ -z "${CFG_IN}" ]]; then
    err "Missing CONFIG after -c/--config"
    usage
    exit 2
  fi
else
  CFG_IN="${1}"
fi

# ---- venv ----
if [[ ! -f "${VENV_DIR}/bin/activate" ]]; then
  err "Missing venv at ${VENV_DIR}/bin/activate"
  exit 1
fi

# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"
log ".venv activated (${VENV_DIR})"

# ---- config path resolution ----
if [[ "${CFG_IN}" = /* ]]; then
  CFG_PATH="${CFG_IN}"
else
  CFG_PATH="${REPO_DIR}/${CFG_IN}"
fi

# Resolve to absolute if possible
if command -v readlink >/dev/null 2>&1; then
  CFG_ABS="$(readlink -f "${CFG_PATH}" 2>/dev/null || true)"
else
  CFG_ABS=""
fi

if [[ -z "${CFG_ABS}" ]]; then
  # Fallback if readlink -f is unavailable
  if [[ -f "${CFG_PATH}" ]]; then
    CFG_ABS="${CFG_PATH}"
  else
    err "Config file not found: ${CFG_PATH}"
    exit 1
  fi
fi

if [[ ! -f "${CFG_ABS}" ]]; then
  err "Config file not found: ${CFG_ABS}"
  exit 1
fi

# Derive a config path relative to repo (if applicable) for matching older/manual launches
CFG_REL=""
case "${CFG_ABS}" in
  "${REPO_DIR}"/*)
    CFG_REL="${CFG_ABS#${REPO_DIR}/}"
    ;;
  *)
    CFG_REL=""
    ;;
esac

# ---- single-instance guard per config ----
# Lock name includes basename + checksum-ish number (cksum exists everywhere)
CFG_TAG_BASE="$(basename "${CFG_ABS}")"
CFG_TAG_NUM="$(printf '%s' "${CFG_ABS}" | cksum | awk '{print $1}')"
LOCK="/tmp/pydaq-${CFG_TAG_BASE}-${CFG_TAG_NUM}.lock"

exec 9>"${LOCK}"
if ! flock -n 9; then
  log "Already running (lock held) for config: ${CFG_ABS}"
  exit 0
fi

# Extra safety: if a process is running that wasn't started via this script/lock
# (e.g., manual start), don't start another.
# Match both absolute and (if available) repo-relative config path.
PAT_ABS="python.*-m[[:space:]]+pydaq.*-c[[:space:]]+${CFG_ABS}"
if pgrep -f "${PAT_ABS}" >/dev/null 2>&1; then
  log "Already running (pgrep) for config: ${CFG_ABS}"
  exit 0
fi

if [[ -n "${CFG_REL}" ]]; then
  PAT_REL="python.*-m[[:space:]]+pydaq.*-c[[:space:]]+${CFG_REL}"
  if pgrep -f "${PAT_REL}" >/dev/null 2>&1; then
    log "Already running (pgrep) for config: ${CFG_REL}"
    exit 0
  fi
fi

# ---- start ----
cd "${REPO_DIR}"
log "Starting pydaq with config: ${CFG_ABS}"

# Keep the lock for the full lifetime of pydaq by exec'ing into python.
exec "${VENV_DIR}/bin/python" -u -m pydaq -c "${CFG_ABS}"
