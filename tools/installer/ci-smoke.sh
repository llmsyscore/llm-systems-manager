#!/usr/bin/env bash
# =============================================================================
# tools/installer/ci-smoke.sh — post-install smoke oracle for CI (#25).
#
# Lean, tracked oracle for the installer integration workflow: verifies the
# systemd units are active, the health endpoints respond, and each deployed
# service self-reports the version present in the installed source tree
# (catches "unit up but running old/broken code" after an upgrade).
#
# The full operator smoke tests (tools/*_smoke_test.sh) are local-only,
# auth/CDP-heavy, and probe the lab's real agent fleet — not usable on a
# GitHub runner. This script is the CI-scoped subset.
#
# Usage: sudo bash tools/installer/ci-smoke.sh [--no-agent]
#   --no-agent   Skip the local-agent unit check (installs without an agent).
# Exit code: 0 iff every check passed.
# =============================================================================
set -uo pipefail

INSTALL_DIR="${LLMSYS_INSTALL_DIR:-/opt/llm-systems-manager}"
CHECK_AGENT=true
[[ "${1:-}" == "--no-agent" ]] && CHECK_AGENT=false

PASS=0; FAIL=0
_pass() { echo "[PASS] $*"; PASS=$((PASS+1)); }
_fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }

_probe_code() { curl -sS -m 10 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || true; }

_unit_active() {
  if systemctl is-active --quiet "$1"; then
    _pass "unit $1 active"
  else
    _fail "unit $1 not active"
    journalctl -u "$1" -n 40 --no-pager 2>/dev/null | sed 's/^/    /' || true
  fi
}

# AE TLS defaults to ON; probe https with the internal CA, fall back to http.
_AE_CA="$INSTALL_DIR/data/internal-ca.crt"
_ae_curl() {
  if [[ -r "$_AE_CA" ]]; then
    curl -sS -m 10 --cacert "$_AE_CA" "https://127.0.0.1:8081$1" 2>/dev/null \
      || curl -sS -m 10 "http://127.0.0.1:8081$1" 2>/dev/null
  else
    curl -sS -m 10 "http://127.0.0.1:8081$1" 2>/dev/null
  fi
}

_src_version() {
  { grep -E '^(VERSION|__version__)[[:space:]]*=' "$1" 2>/dev/null || true; } \
    | head -1 | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/'
}

# _version_match NAME JSON SRC_FILE — running /health version == deployed source
_version_match() {
  local name="$1" body="$2" src_file="$3" want got
  want="$(_src_version "$src_file")"
  got="$(jq -r '.version // empty' <<<"$body" 2>/dev/null)"
  if [[ -z "$want" ]]; then
    _fail "$name: no version found in $src_file"
  elif [[ "$got" == "$want" ]]; then
    _pass "$name /health reports deployed version $got"
  else
    _fail "$name /health reports '$got' but deployed source is '$want'"
  fi
}

_http_ok() {
  local name="$1" code="$2"
  if [[ "$code" == "200" ]]; then _pass "$name → 200"; else _fail "$name → '$code' (want 200)"; fi
}

echo "── systemd units ──────────────────────────────────────────────"
_unit_active influxdb
_unit_active llm-systems-alarm-engine
_unit_active llm-systems-manager
$CHECK_AGENT && _unit_active llm-systems-agent

echo "── endpoints ──────────────────────────────────────────────────"
_http_ok "InfluxDB /health" "$(_probe_code http://127.0.0.1:8086/health)"
_http_ok "Manager /health"  "$(_probe_code http://127.0.0.1:5000/health)"
# / is auth-gated (302 → /login); -L asserts the login page renders.
_http_ok "Manager / (login)" "$(_probe_code -L http://127.0.0.1:5000/)"
_AE_HEALTH="$(_ae_curl /health)"
if [[ -n "$_AE_HEALTH" ]] && jq -e '.status == "ok"' <<<"$_AE_HEALTH" >/dev/null 2>&1; then
  _pass "Alarm engine /health → status ok"
else
  _fail "Alarm engine /health unreachable or not ok: ${_AE_HEALTH:-<empty>}"
fi

# _toml_key_present <toml> <dotted.key> — exit 0 present, 1 absent, 2 parse error
_toml_key_present() {
  python3 - "$1" "$2" <<'PY'
import sys, tomllib
try:
    cur = tomllib.loads(open(sys.argv[1]).read())
except Exception:
    sys.exit(2)
parts = sys.argv[2].split('.')
for p in parts[:-1]:
    if not isinstance(cur, dict) or p not in cur:
        sys.exit(1)
    cur = cur[p]
sys.exit(0 if isinstance(cur, dict) and parts[-1] in cur else 1)
PY
}

echo "── upstream-removed paths pruned ──────────────────────────────"
_MANIFEST="$INSTALL_DIR/tools/installer/removed-paths.manifest"
_LIVE_TOML="$INSTALL_DIR/config/llm-systems.toml"
if [[ -f "$_MANIFEST" ]]; then
  while IFS='|' read -r _d _pr _v; do
    [[ -z "$_d" || "$_d" == \#* ]] && continue
    case "$_d" in
      file)
        if [[ -e "$INSTALL_DIR/$_v" ]]; then
          _fail "stale upstream-removed file present: $_v"
        else
          _stale_pyc="$(find "$(dirname "$INSTALL_DIR/$_v")/__pycache__" -maxdepth 1 \
            -name "$(basename "$_v" .py).cpython-*.pyc" 2>/dev/null | head -1)"
          if [[ "$_v" == *.py && -n "$_stale_pyc" ]]; then
            _fail "stale bytecode of removed module present: $_stale_pyc"
          else
            _pass "pruned: $_v absent"
          fi
        fi ;;
      toml-key)
        if [[ ! -f "$_LIVE_TOML" ]]; then
          _pass "pruned: TOML key $_v absent (no live TOML)"
        else
          _toml_key_present "$_LIVE_TOML" "$_v"
          case $? in
            0) _fail "stale upstream-removed key present in live TOML: $_v" ;;
            1) _pass "pruned: TOML key $_v absent" ;;
            *) _fail "live TOML unparseable while checking $_v" ;;
          esac
        fi ;;
    esac
  done < "$_MANIFEST"
else
  _pass "no removed-paths.manifest deployed — nothing to verify"
fi

echo "── release marker + update check ──────────────────────────────"
# Asserts the deployed tree carries a stamped RELEASE marker (or is a git
# checkout), is owned by the service user, and resolves in the update check.
_REL_FILE="$INSTALL_DIR/RELEASE"
_REL_VAL="$(head -1 "$_REL_FILE" 2>/dev/null || true)"
if [[ -n "$_REL_VAL" && "$_REL_VAL" != \$Format:* ]]; then
  _pass "RELEASE marker deployed: $_REL_VAL"
  _REL_OWNER="$(stat -c '%U:%G' "$_REL_FILE" 2>/dev/null || true)"
  _TREE_OWNER="$(stat -c '%U:%G' "$INSTALL_DIR/llm-systems-manager/backend/llm-systems-manager.py" 2>/dev/null || true)"
  if [[ -z "$_TREE_OWNER" ]]; then
    _pass "RELEASE marker owner $_REL_OWNER (no manager tree to compare)"
  elif [[ "$_REL_OWNER" == "$_TREE_OWNER" ]]; then
    _pass "RELEASE marker owned by the service user ($_REL_OWNER)"
  else
    _fail "RELEASE marker owned by $_REL_OWNER but the deployed tree is $_TREE_OWNER"
  fi
elif [[ -e "$INSTALL_DIR/.git" ]]; then
  _pass "no RELEASE marker, but $INSTALL_DIR is a git checkout — describe answers"
else
  _fail "RELEASE marker missing or unstamped and no .git to fall back on: ${_REL_VAL:-<absent>}"
fi

# Drives the manager's own tag resolution and ranks a synthetic newer tag
# against it; no github.com call, no dashboard session.
_REL_PY="$INSTALL_DIR/llm-systems-manager/venv/bin/python3"
if [[ ! -x "$_REL_PY" ]]; then
  _fail "manager venv python missing at $_REL_PY"
else
  _REL_SNIP="$(mktemp)"
  cat > "$_REL_SNIP" <<'PY'
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
  _REL_OUT="$(timeout 30 "$_REL_PY" "$_REL_SNIP" "$INSTALL_DIR" 2>&1 | tail -1)"
  rm -f "$_REL_SNIP"
  case "$_REL_OUT" in
    OK*)   _pass "update check resolves the install: ${_REL_OUT#OK }" ;;
    FAIL*) _fail "update check: ${_REL_OUT#FAIL }" ;;
    *)     _fail "update check probe errored: $_REL_OUT" ;;
  esac
fi

echo "── deployed versions ──────────────────────────────────────────"
_version_match "Manager" "$(curl -sS -m 10 http://127.0.0.1:5000/health 2>/dev/null)" \
  "$INSTALL_DIR/llm-systems-manager/backend/llm-systems-manager.py"
_version_match "Alarm engine" "$_AE_HEALTH" \
  "$INSTALL_DIR/llm-systems-alarm-engine/backend/alarm_engine.py"

echo
echo "Results: $PASS passed, $FAIL failed"
(( FAIL == 0 )) || exit 1
exit 0
