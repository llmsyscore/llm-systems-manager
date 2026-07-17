#!/usr/bin/env bash
# Agent-lifecycle oracle for CI (#418): verify a monitoring agent registers with
# the manager, is admin-approved, reports, and upgrades to FULL TLS — both its
# local HTTPS serve and the manager-confirmed control-channel flip. Shared by the
# fresh-install (mode-1 local agent) and split-install (mode-5 agent pointed at
# the mode-3 manager) jobs; only MGR_URL / AGENT_HOSTNAME differ. Run as root.
#
# Admin HTTP calls always target loopback (127.0.0.1 is unconditionally in
# admin_cidrs) using the seeded llmadmin/llmadmin default — if a future change
# seeds a random admin password this oracle fails loudly at login, by design.
set -euo pipefail

MGR_URL="${MGR_URL:-http://127.0.0.1:5000}"
AGENT_HOSTNAME="${AGENT_HOSTNAME:-$(hostname)}"
AGENT_HEALTH_HOST="${AGENT_HEALTH_HOST:-127.0.0.1}"
AGENT_HEALTH_PORT="${AGENT_HEALTH_PORT:-8082}"
AGENT_HTTP="http://$AGENT_HEALTH_HOST:$AGENT_HEALTH_PORT/health"
AGENT_HTTPS="https://$AGENT_HEALTH_HOST:$AGENT_HEALTH_PORT/health"

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ FAIL: $*"; exit 1; }

COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

# Admin GET of the /api/agents JSON (cookie-jar auth).
agents_json() { curl -sS -b "$COOKIE_JAR" -m 10 "$MGR_URL/api/agents"; }
# HTTP status of an arbitrary curl; extra args pass through, never aborts.
code() { curl -s -o /dev/null -w '%{http_code}' -m 5 "$@" || true; }

echo "── 1. Admin login (seeded llmadmin/llmadmin) ────────────────────────"
login_code="$(curl -sS -c "$COOKIE_JAR" -o /dev/null -w '%{http_code}' -m 10 \
  --data-urlencode 'username=llmadmin' --data-urlencode 'password=llmadmin' "$MGR_URL/login")"
case "$login_code" in
  302|303) : ;;
  *) fail "login returned $login_code (want 302/303) — admin seed/creds changed?" ;;
esac
pass "logged in as llmadmin"

echo "── 2. Agent registered (poll for it by hostname) ────────────────────"
AID=""
for _ in $(seq 1 30); do
  AID="$(agents_json | jq -r --arg h "$AGENT_HOSTNAME" '(.agents[] | select(.hostname==$h) | .agent_id) // empty' | head -1)"
  if [ -z "$AID" ]; then
    # Both CI jobs run exactly one agent — fall back to the sole agent if the
    # hostname didn't match (FQDN / case differences).
    AID="$(agents_json | jq -r 'if (.agents|length)==1 then .agents[0].agent_id else empty end')"
  fi
  if [ -n "$AID" ]; then break; fi
  sleep 2
done
if [ -z "$AID" ]; then fail "agent '$AGENT_HOSTNAME' never registered with the manager"; fi
pass "agent registered: $AID"

echo "── 3. Approve the agent (admin) ─────────────────────────────────────"
status="$(agents_json | jq -r --arg id "$AID" '.agents[] | select(.agent_id==$id) | .status')"
echo "    pre-approve status=$status registered_from=$(agents_json | jq -r --arg id "$AID" '.agents[] | select(.agent_id==$id) | .registered_from')"
if [ "$status" != "approved" ]; then
  resp="$(curl -sS -b "$COOKIE_JAR" -m 10 -X POST "$MGR_URL/api/agents/$AID/approve")"
  if ! jq -e '.ok==true and .status=="approved"' >/dev/null 2>&1 <<<"$resp"; then
    fail "approve did not return ok/approved: $resp"
  fi
fi
status="$(agents_json | jq -r --arg id "$AID" '.agents[] | select(.agent_id==$id) | .status')"
if [ "$status" != "approved" ]; then fail "agent status still $status after approve (want approved)"; fi
pass "agent approved"

echo "── 4. Agent reports approved (agent-side /health) ───────────────────"
ok=0
for _ in $(seq 1 40); do
  if curl -sk -m 5 "$AGENT_HTTP"  2>/dev/null | jq -e '.approved==true' >/dev/null 2>&1 \
     || curl -sk -m 5 "$AGENT_HTTPS" 2>/dev/null | jq -e '.approved==true' >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
if [ "$ok" != 1 ]; then fail "agent never reported approved=true on its /health"; fi
pass "agent reports approved=true"

echo "── 5. Full local TLS: HTTPS serve up, plain HTTP dropped ────────────"
ok=0
for _ in $(seq 1 40); do
  if [ "$(code -k "$AGENT_HTTPS")" = "200" ]; then ok=1; break; fi
  sleep 5
done
if [ "$ok" != 1 ]; then fail "agent never served HTTPS on :$AGENT_HEALTH_PORT (TLS upgrade)"; fi
http_code="$(code "$AGENT_HTTP")"
if [ "$http_code" = "200" ]; then fail "plain HTTP still 200 on :$AGENT_HEALTH_PORT — TLS not truly bound"; fi
pass "agent serving HTTPS; plain HTTP dropped (http code=$http_code)"

echo "── 6. Control channel upgraded to TLS (manager-confirmed) ───────────"
ok=0
for _ in $(seq 1 40); do
  if agents_json | jq -e --arg id "$AID" \
       '.agents[] | select(.agent_id==$id) | .last_heartbeat_data.control_channel_tls==true' >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done
if [ "$ok" != 1 ]; then fail "control_channel_tls never became true (agent→manager HTTPS upgrade)"; fi
pass "control channel upgraded to TLS (manager-confirmed)"

echo "── 7. Agent liveness live/stale (heartbeats landing) ────────────────"
liveness="$(agents_json | jq -r --arg id "$AID" '.agents[] | select(.agent_id==$id) | .liveness')"
case "$liveness" in
  live|stale) : ;;
  *) fail "agent liveness=$liveness (want live/stale — heartbeats not landing)" ;;
esac
pass "agent liveness=$liveness"

echo
echo "ALL AGENT-LIFECYCLE ASSERTIONS PASSED ($AID)"
