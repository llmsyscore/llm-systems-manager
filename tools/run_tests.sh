#!/usr/bin/env bash
# Run the project's unit test suites — alarm engine + manager — back to back.
# Each suite uses its own venv (Python deps differ). Exits non-zero on the
# first failure so CI can short-circuit cleanly.
#
# Usage:
#   tools/run_tests.sh                # both suites
#   tools/run_tests.sh ae             # alarm engine only
#   tools/run_tests.sh manager        # manager only
#   tools/run_tests.sh -- -k auth     # pass-through to pytest (after --)
#
# The wrapper installs `pytest` into each venv on first run if it's missing
# (the venvs are dev-side here — operator deploys don't ship pytest).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AE_DIR="$REPO/llm-systems-alarm-engine"
MGR_DIR="$REPO/llm-systems-manager"

WHICH="both"
FORWARD=()
while (( $# )); do
  case "$1" in
    ae|alarm-engine|alarm) WHICH="ae"; shift ;;
    manager|mgr)           WHICH="manager"; shift ;;
    both|all)              WHICH="both"; shift ;;
    --) shift; FORWARD=("$@"); break ;;
    -h|--help)
      sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# *//'
      exit 0 ;;
    *) echo "unknown arg: $1 (use 'ae', 'manager', 'both', or '-- <pytest args>')" >&2; exit 2 ;;
  esac
done

ensure_pytest() {
  local venv_py="$1/venv/bin/python"
  if [[ ! -x "$venv_py" ]]; then
    echo "ERR: venv missing at $1/venv — run the installer first" >&2
    return 1
  fi
  if ! "$venv_py" -c 'import pytest' >/dev/null 2>&1; then
    echo "[INFO] installing pytest into $1/venv"
    "$venv_py" -m pip install --quiet pytest pytest-asyncio
  fi
}

run_suite() {
  local label="$1" dir="$2"
  echo
  echo "── $label ─────────────────────────────────────────────────"
  ensure_pytest "$dir"
  ( cd "$dir" && "$dir/venv/bin/python" -m pytest "${FORWARD[@]+${FORWARD[@]}}" )
}

case "$WHICH" in
  ae)      run_suite "Alarm engine" "$AE_DIR" ;;
  manager) run_suite "Manager"      "$MGR_DIR" ;;
  both)    run_suite "Alarm engine" "$AE_DIR"
           run_suite "Manager"      "$MGR_DIR" ;;
esac
