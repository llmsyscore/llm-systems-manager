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

echo "── deployed versions ──────────────────────────────────────────"
_version_match "Manager" "$(curl -sS -m 10 http://127.0.0.1:5000/health 2>/dev/null)" \
  "$INSTALL_DIR/llm-systems-manager/backend/llm-systems-manager.py"
_version_match "Alarm engine" "$_AE_HEALTH" \
  "$INSTALL_DIR/llm-systems-alarm-engine/backend/alarm_engine.py"

echo
echo "Results: $PASS passed, $FAIL failed"
(( FAIL == 0 )) || exit 1
exit 0
