#!/usr/bin/env bash
# Run the project's unit test suites — alarm engine, manager, agent, and the
# frontend JS — back to back. Exits non-zero on the first failure so CI can
# short-circuit cleanly.
#
# Usage:
#   tools/run_tests.sh                # all suites (py + frontend js)
#   tools/run_tests.sh ae             # alarm engine only
#   tools/run_tests.sh manager        # manager only
#   tools/run_tests.sh installer      # installer helpers (toml_reconcile) only
#   tools/run_tests.sh js             # frontend js (vitest) only
#   tools/run_tests.sh -- -k auth     # pass-through to pytest (after --)
#
# First run installs `pytest` into each venv and npm devDependencies for the
# js suite if missing — dev-side only, not shipped to operator deploys.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AE_DIR="$REPO/llm-systems-alarm-engine"
MGR_DIR="$REPO/llm-systems-manager"
AGENT_DIR="$REPO/agent"
INSTALLER_DIR="$REPO/tools/installer"
FE_DIR="$REPO/llm-systems-manager/frontend"

WHICH="both"
FORWARD=()
while (( $# )); do
  case "$1" in
    ae|alarm-engine|alarm) WHICH="ae"; shift ;;
    manager|mgr)           WHICH="manager"; shift ;;
    agent)                 WHICH="agent"; shift ;;
    installer)             WHICH="installer"; shift ;;
    js|frontend|fe)        WHICH="js"; shift ;;
    both|all)              WHICH="both"; shift ;;
    --) shift; FORWARD=("$@"); break ;;
    -h|--help)
      sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# *//'
      exit 0 ;;
    *) echo "unknown arg: $1 (use 'ae', 'manager', 'agent', 'installer', 'js', 'both', or '-- <pytest args>')" >&2; exit 2 ;;
  esac
done

ensure_pytest() {
  local venv_py="$1/venv/bin/python"
  if [[ ! -x "$venv_py" ]]; then
    echo "[INFO] creating venv at $1/venv"
    python3 -m venv "$1/venv"
  fi
  if ! "$1/venv/bin/python" -c 'import pytest' >/dev/null 2>&1; then
    echo "[INFO] installing pytest into $1/venv"
    "$1/venv/bin/python" -m pip install --quiet pytest pytest-asyncio
  fi
}

run_suite() {
  local label="$1" dir="$2"
  echo
  echo "── $label ─────────────────────────────────────────────────"
  ensure_pytest "$dir"
  ( cd "$dir" && "$dir/venv/bin/python" -m pytest "${FORWARD[@]+${FORWARD[@]}}" )
}

run_js() {
  local dir="$1"
  echo
  echo "── Frontend (JS) ──────────────────────────────────────────"
  if ! command -v npm >/dev/null 2>&1; then
    echo "[WARN] npm not found — skipping frontend JS suite" >&2
    return 0
  fi
  [[ -d "$dir/node_modules" ]] || ( cd "$dir" && echo "[INFO] npm ci (frontend devDependencies)" && npm ci )
  ( cd "$dir" && npm test )
}

case "$WHICH" in
  ae)      run_suite "Alarm engine" "$AE_DIR" ;;
  manager) run_suite "Manager"      "$MGR_DIR" ;;
  agent)   run_suite "Agent"        "$AGENT_DIR" ;;
  installer) run_suite "Installer"  "$INSTALLER_DIR" ;;
  js)      run_js "$FE_DIR" ;;
  both)    run_suite "Alarm engine" "$AE_DIR"
           run_suite "Manager"      "$MGR_DIR"
           run_suite "Agent"        "$AGENT_DIR"
           run_suite "Installer"    "$INSTALLER_DIR"
           run_js "$FE_DIR" ;;
esac
