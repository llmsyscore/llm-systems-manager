#!/usr/bin/env bash
# =============================================================================
# tools/installer/ci-release-marker.sh — deployed-tree release-marker oracle.
#
# Asserts a deployed install dir carries a stamped RELEASE marker (or is a git
# checkout), that the marker is owned by the service user, and — when the
# manager is installed — that the manager's own update check resolves it.
#
# Usage: sudo bash tools/installer/ci-release-marker.sh [--no-manager] [INSTALL_DIR]
#   --no-manager   AE-only tree (mode 4): skip the manager-venv resolution probe.
# Sourced by ci-smoke.sh for release_marker_checks(); exit 0 iff all passed.
# =============================================================================
set -uo pipefail

# release_marker_checks INSTALL_DIR [--no-manager] — uses the caller's _pass/_fail.
release_marker_checks() {
  local dir="$1" with_manager=1 marker val owner tree_owner ref py snip out
  [[ "${2:-}" == "--no-manager" ]] && with_manager=0
  marker="$dir/RELEASE"
  val="$(head -1 "$marker" 2>/dev/null || true)"
  if [[ -n "$val" && "$val" != \$Format:* ]]; then
    _pass "RELEASE marker deployed: $val"
    owner="$(stat -c '%U:%G' "$marker" 2>/dev/null || true)"
    tree_owner=""
    for ref in "$dir/llm-systems-manager/backend/llm-systems-manager.py" \
               "$dir/llm-systems-alarm-engine/backend/alarm_engine.py"; do
      [[ -f "$ref" ]] && { tree_owner="$(stat -c '%U:%G' "$ref" 2>/dev/null || true)"; break; }
    done
    if [[ -z "$tree_owner" ]]; then
      _pass "RELEASE marker owner $owner (no deployed service tree to compare)"
    elif [[ "$owner" == "$tree_owner" ]]; then
      _pass "RELEASE marker owned by the service user ($owner)"
    else
      _fail "RELEASE marker owned by $owner but the deployed tree is $tree_owner"
    fi
  elif [[ -e "$dir/.git" ]]; then
    _pass "no RELEASE marker, but $dir is a git checkout — describe answers"
  else
    _fail "RELEASE marker missing or unstamped and no .git to fall back on: ${val:-<absent>}"
  fi

  (( with_manager )) || return 0
  # Drives the manager's own tag resolution and ranks a synthetic newer tag
  # against it; no github.com call, no dashboard session.
  py="$dir/llm-systems-manager/venv/bin/python3"
  if [[ ! -x "$py" ]]; then
    _fail "manager venv python missing at $py"
    return 0
  fi
  snip="$(mktemp)"
  cat > "$snip" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "llm-systems-manager" / "backend"))
import companion  # noqa: E402

inst = companion._installed_release()
tag = inst.get("tag") or ""
if not tag:
    print("FAIL no tag resolved (source=%r describe=%r)"
          % (inst.get("source"), inst.get("describe")))
elif not companion._newer("v99.99.99", tag):
    print("FAIL %r not ranked older than a synthetic v99.99.99" % tag)
else:
    print("OK %s (source=%s kind=%s)"
          % (tag, inst.get("source"), companion._install_kind(inst)))
PY
  out="$(timeout 30 "$py" "$snip" "$dir" 2>&1 | tail -1)"
  rm -f "$snip"
  case "$out" in
    OK*)   _pass "update check resolves the install: ${out#OK }" ;;
    FAIL*) _fail "update check: ${out#FAIL }" ;;
    *)     _fail "update check probe errored: $out" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  PASS=0; FAIL=0
  _pass() { echo "[PASS] $*"; PASS=$((PASS+1)); }
  _fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
  _flag=""; _dir="${LLMSYS_INSTALL_DIR:-/opt/llm-systems-manager}"
  for a in "$@"; do
    case "$a" in --no-manager) _flag="--no-manager" ;; *) _dir="$a" ;; esac
  done
  echo "── release marker + update check ($_dir) ────────────────────"
  release_marker_checks "$_dir" $_flag
  echo "Results: $PASS passed, $FAIL failed"
  (( FAIL == 0 )) || exit 1
fi
