#!/usr/bin/env bash
# Split-install cross-host wiring oracle for CI (#418). Runs on ONE runner after
# a mode-4 (alarm-engine) install into $AE_DIR and a mode-3 (manager) install
# into $MGR_DIR. It performs the manual steps a real split operator would do
# (activate the AE's generated tokens, paste them into the manager, use the
# manager-issued TLS cert) and asserts the manager<->AE token / proxy / CORS /
# TLS wiring that modes 1/2 can never exercise (they always co-locate). Run as root.
set -euo pipefail

AE_DIR="${AE_DIR:?set AE_DIR (mode-4 install dir)}"
MGR_DIR="${MGR_DIR:?set MGR_DIR (mode-3 install dir)}"
DETECTED_IP="${DETECTED_IP:?set DETECTED_IP (runner IP shared by both installs)}"

AE_TOML="$AE_DIR/config/llm-systems.toml"
MGR_TOML="$MGR_DIR/config/llm-systems.toml"
AE_CERT="$MGR_DIR/data/ae-tls.crt"
AE_URL="http://$DETECTED_IP:8081"
MGR_URL="http://$DETECTED_IP:5000"

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ FAIL: $*"; exit 1; }

# HTTP status of a request; extra args (headers, -X) pass through to curl. A
# connection failure prints 000 and does not abort, so callers assert on the code.
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }

# Read a dotted-section string key from a TOML file via stdlib tomllib.
toml_get() {
  python3 - "$1" "$2" "$3" <<'PY'
import sys, tomllib
path, section, key = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "rb") as fh:
    node = tomllib.load(fh)
for part in section.split("."):
    node = node.get(part, {})
print(node.get(key, "") if isinstance(node, dict) else "")
PY
}

wait_active() {
  for _ in $(seq 1 30); do
    if systemctl is-active --quiet "$1"; then return 0; fi
    sleep 1
  done
  return 1
}

wait_health() {
  for _ in $(seq 1 30); do
    if [ "$(code "$1")" = "200" ]; then return 0; fi
    sleep 1
  done
  return 1
}

echo "── 0. Both units up; manager issued the AE TLS cert ──────────────────"
if ! wait_active llm-systems-alarm-engine; then fail "alarm-engine unit not active"; fi
if ! wait_active llm-systems-manager;      then fail "manager unit not active"; fi
if ! wait_health "$AE_URL/health"; then fail "AE /health never 200 on $AE_URL"; fi
if ! wait_health "$MGR_URL/health";      then fail "manager never 200 on $MGR_URL"; fi
for _ in $(seq 1 30); do
  if [ -f "$AE_CERT" ]; then break; fi
  sleep 1
done
if [ ! -f "$AE_CERT" ]; then fail "manager never issued $AE_CERT"; fi
pass "both units active; AE TLS cert issued into the manager's data dir"

echo "── 1. Read the AE's generated tokens (baked in commented-out) ────────"
INGEST="$(grep -oE '^#[[:space:]]*ingest_token[[:space:]]*=[[:space:]]*"[^"]+"' "$AE_TOML" | sed -E 's/.*"([^"]+)".*/\1/')"
MGMT="$(grep -oE '^#[[:space:]]*management_token[[:space:]]*=[[:space:]]*"[^"]+"' "$AE_TOML" | sed -E 's/.*"([^"]+)".*/\1/')"
if [ -z "$INGEST" ] || [ -z "$MGMT" ]; then fail "could not read commented tokens from $AE_TOML"; fi
pass "read split-AE ingest + management tokens from the AE config"

echo "── 2. Activate the tokens on the AE (uncomment) + restart ────────────"
sed -i -E 's/^#[[:space:]]*(ingest_token[[:space:]]*=)/\1/; s/^#[[:space:]]*(management_token[[:space:]]*=)/\1/' "$AE_TOML"
systemctl restart llm-systems-alarm-engine
if ! wait_health "$AE_URL/health"; then fail "AE unhealthy after token activation"; fi
pass "AE tokens activated (now enforced)"

echo "── 3. AE enforces management_token on /api/alarm/rules ───────────────"
c="$(code "$AE_URL/api/alarm/rules")"
if [ "$c" != "401" ]; then fail "AE rules without token = $c (want 401)"; fi
c="$(code -H "Authorization: Bearer $MGMT" "$AE_URL/api/alarm/rules")"
if [ "$c" != "200" ]; then fail "AE rules with management_token = $c (want 200)"; fi
pass "no token -> 401, management_token -> 200"

echo "── 4. Manager proxy rejected until its own token is wired ────────────"
c="$(code "$MGR_URL/api/alarm/rules")"
if [ "$c" != "401" ]; then fail "manager proxy pre-wiring = $c (want 401 from the manager's default token)"; fi
pass "manager (default REPLACE_ME token) proxied -> AE 401"

echo "── 5. Wire the AE tokens into the manager (the paste step) + restart ─"
sed -i -E "s|^ingest_token[[:space:]]*=.*|ingest_token = \"$INGEST\"|; s|^management_token[[:space:]]*=.*|management_token = \"$MGMT\"|" "$MGR_TOML"
systemctl restart llm-systems-manager
if ! wait_health "$MGR_URL/health"; then fail "manager unhealthy after token wiring"; fi
pass "manager tokens wired"

echo "── 6. Manager proxy works, strips the client header, uses management_token ─"
c="$(code -H 'Authorization: Bearer bogus-client-token' "$MGR_URL/api/alarm/rules")"
if [ "$c" != "200" ]; then fail "manager proxy post-wiring (bogus client hdr) = $c (want 200)"; fi
pass "proxy 200 with a bogus client Authorization (stripped; manager's own bearer forwarded)"

echo "── 7. CORS allow-lists byte-identical across both hosts ──────────────"
MGR_CORS="$(toml_get "$MGR_TOML" manager cors_origins)"
AE_CORS="$(toml_get "$AE_TOML" alarm_engine cors_origins)"
if [ "$MGR_CORS" != "$AE_CORS" ]; then fail "CORS differ: mgr=[$MGR_CORS] ae=[$AE_CORS]"; fi
case "$MGR_CORS" in
  *"$DETECTED_IP:5000"*"$DETECTED_IP:8081"*) : ;;
  *) fail "CORS missing expected origins: [$MGR_CORS]" ;;
esac
pass "CORS identical and contains both manager + AE origins"

echo "── 8. AE TLS cert SAN covers the detected IP ─────────────────────────"
if ! openssl x509 -in "$AE_CERT" -noout -text | grep -A1 'Subject Alternative Name' | grep -qF "$DETECTED_IP"; then
  fail "AE cert SAN does not cover $DETECTED_IP"
fi
pass "AE cert SAN includes $DETECTED_IP"

echo "── 9. Read-once bearer footgun: rotation needs a manager restart ─────"
NEW_MGMT="$(openssl rand -hex 32)"
sed -i -E "s|^management_token[[:space:]]*=.*|management_token = \"$NEW_MGMT\"|" "$AE_TOML"
sed -i -E "s|^management_token[[:space:]]*=.*|management_token = \"$NEW_MGMT\"|" "$MGR_TOML"
systemctl restart llm-systems-alarm-engine
if ! wait_health "$AE_URL/health"; then fail "AE unhealthy after token rotation"; fi
c="$(code "$MGR_URL/api/alarm/rules")"
if [ "$c" != "401" ]; then fail "manager proxy after AE rotation = $c (want 401 — stale in-memory bearer)"; fi
pass "manager still 401s on the rotated token until restarted (read-once at import)"
systemctl restart llm-systems-manager
if ! wait_health "$MGR_URL/health"; then fail "manager unhealthy after restart"; fi
c="$(code "$MGR_URL/api/alarm/rules")"
if [ "$c" != "200" ]; then fail "manager proxy after restart = $c (want 200)"; fi
pass "manager restart picks up the rotated token -> 200"

echo
echo "ALL SPLIT-INSTALL (modes 3 + 4) ASSERTIONS PASSED"
