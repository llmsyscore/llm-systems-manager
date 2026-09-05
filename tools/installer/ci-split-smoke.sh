#!/usr/bin/env bash
# Split-install cross-host wiring oracle for CI (#418). Runs on ONE runner after
# a mode-4 (alarm-engine) install into $AE_DIR and a mode-3 (manager) install
# into $MGR_DIR. It performs the manual steps a real split operator would do
# (copy the manager-issued TLS cert to the AE, activate the AE's generated
# tokens, paste them into the manager) and asserts the manager<->AE token /
# proxy / CORS / TLS wiring that modes 1/2 can never exercise. Run as root.
#
# The manager flips its outbound alarm_engine_url to https when tls_enabled is
# on (the default), so the AE MUST serve HTTPS for the proxy to reach it — hence
# the cert copy, and the AE is probed over https (-k for the internal-CA cert).
set -euo pipefail

AE_DIR="${AE_DIR:?set AE_DIR (mode-4 install dir)}"
MGR_DIR="${MGR_DIR:?set MGR_DIR (mode-3 install dir)}"
DETECTED_IP="${DETECTED_IP:?set DETECTED_IP (runner IP shared by both installs)}"

AE_TOML="$AE_DIR/config/llm-systems.toml"
MGR_TOML="$MGR_DIR/config/llm-systems.toml"
AE_DATA="$AE_DIR/llm-systems-alarm-engine/data"
AE_CERT="$MGR_DIR/data/ae-tls.crt"
AE_KEY="$MGR_DIR/data/ae-tls.key"
AE_URL="https://$DETECTED_IP:8081"
MGR_URL="http://$DETECTED_IP:5000"

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ FAIL: $*"; exit 1; }

# HTTP status of a request; extra args (headers, -X) pass through to curl. -k
# accepts the AE's internal-CA cert; a connection failure prints 000 and does
# not abort, so callers assert on the code.
code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }
ADMIN_USER="${ADMIN_USER:-llmadmin}"
ADMIN_PW="${ADMIN_PW:-llmadmin-ci-rotated}"
SHIPPED_PW="llmadmin"
# ci_admin_login JAR — admin session; the first login on a fresh install
# rotates the shipped default password to ADMIN_PW (mandatory-change wall).
ci_admin_login() {
  local jar="$1" c
  rm -f "$jar"
  c="$(code -c "$jar" --data-urlencode "username=$ADMIN_USER" --data-urlencode "password=$ADMIN_PW" "$MGR_URL/login")"
  case "$c" in 302|303) return 0 ;; esac
  rm -f "$jar"
  c="$(code -c "$jar" --data-urlencode "username=$ADMIN_USER" --data-urlencode "password=$SHIPPED_PW" "$MGR_URL/login")"
  case "$c" in 302|303) : ;; *) echo "$c"; return 1 ;; esac
  code -b "$jar" -X POST -H 'Content-Type: application/json' \
    -d "{\"current_password\":\"$SHIPPED_PW\",\"new_password\":\"$ADMIN_PW\"}" "$MGR_URL/api/account/password" >/dev/null
  rm -f "$jar"
  c="$(code -c "$jar" --data-urlencode "username=$ADMIN_USER" --data-urlencode "password=$ADMIN_PW" "$MGR_URL/login")"
  case "$c" in 302|303) return 0 ;; *) echo "$c"; return 1 ;; esac
}

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
if ! wait_health "$MGR_URL/health";        then fail "manager /health never 200 on $MGR_URL"; fi
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

echo "── 2. Copy the TLS cert to the AE + activate tokens + restart ────────"
mkdir -p "$AE_DATA"
cp "$AE_CERT" "$AE_KEY" "$AE_DATA/"
chown llmsys:llmsys "$AE_DATA/ae-tls.crt" "$AE_DATA/ae-tls.key"
chmod 0644 "$AE_DATA/ae-tls.crt"
chmod 0600 "$AE_DATA/ae-tls.key"
sed -i -E 's/^#[[:space:]]*(ingest_token[[:space:]]*=)/\1/; s/^#[[:space:]]*(management_token[[:space:]]*=)/\1/' "$AE_TOML"
# sed -i as root can leave the file root-owned; the AE runs as llmsys and must
# be able to read its own 0600 config, so restore owner+mode after every edit.
chown llmsys:llmsys "$AE_TOML"; chmod 0600 "$AE_TOML"
systemctl restart llm-systems-alarm-engine
if ! wait_health "$AE_URL/health"; then fail "AE not serving HTTPS /health after cert copy + token activation"; fi
pass "AE serving HTTPS with tokens enforced"

echo "── 3. AE enforces management_token on /api/alarm/rules ───────────────"
c="$(code "$AE_URL/api/alarm/rules")"
if [ "$c" != "401" ]; then fail "AE rules without token = $c (want 401)"; fi
c="$(code -H "Authorization: Bearer $MGMT" "$AE_URL/api/alarm/rules")"
if [ "$c" != "200" ]; then fail "AE rules with management_token = $c (want 200)"; fi
pass "no token -> 401, management_token -> 200"

echo "── 3b. AE gates metrics reads, dbstats and the backup export (#826) ──"
for p in /api/alarm/metrics "/api/alarm/metrics/system/cpu_total?since_minutes=5" /api/alarm/dbstats/sqlite; do
  c="$(code "$AE_URL$p")"
  if [ "$c" != "401" ]; then fail "AE $p without token = $c (want 401)"; fi
  c="$(code -H "Authorization: Bearer $MGMT" "$AE_URL$p")"
  if [ "$c" != "200" ]; then fail "AE $p with management_token = $c (want 200)"; fi
done
pass "metrics/dbstats: no token -> 401, management_token -> 200"
# The backup archive carries every secret: only the management token opens it.
c="$(code -X POST "$AE_URL/api/alarm/admin/export")"
if [ "$c" != "401" ]; then fail "AE admin/export without token = $c (want 401)"; fi
c="$(code -X POST -H "Authorization: Bearer $INGEST" "$AE_URL/api/alarm/admin/export")"
if [ "$c" != "401" ]; then fail "AE admin/export with ingest_token = $c (want 401)"; fi
c="$(code -X POST -H "Authorization: Bearer $MGMT" -H 'Content-Type: application/json' -d '{}' "$AE_URL/api/alarm/admin/export")"
if [ "$c" != "200" ]; then fail "AE admin/export with management_token = $c (want 200)"; fi
c="$(code -X POST -H "Authorization: Bearer $INGEST" "$AE_URL/api/alarm/admin/import/preview")"
if [ "$c" != "401" ]; then fail "AE admin/import/preview with ingest_token = $c (want 401)"; fi
pass "backup export/import: ingest_token -> 401, management_token -> 200"

echo "── 4. Wire the AE tokens into the manager (the paste step) + restart ─"
sed -i -E "s|^ingest_token[[:space:]]*=.*|ingest_token = \"$INGEST\"|; s|^management_token[[:space:]]*=.*|management_token = \"$MGMT\"|" "$MGR_TOML"
chown llmsys:llmsys "$MGR_TOML"; chmod 0600 "$MGR_TOML"
systemctl restart llm-systems-manager
if ! wait_health "$MGR_URL/health"; then fail "manager unhealthy after token wiring"; fi
pass "manager tokens wired"

echo "── 5. Manager + AE configs agree on the shared tokens ────────────────"
MGR_MGMT="$(toml_get "$MGR_TOML" alarm_engine management_token)"
AE_MGMT="$(toml_get "$AE_TOML" alarm_engine management_token)"
if [ -z "$AE_MGMT" ] || [ "$MGR_MGMT" != "$AE_MGMT" ]; then
  fail "management_token mismatch: mgr=[${MGR_MGMT:0:8}] ae=[${AE_MGMT:0:8}]"
fi
pass "both hosts carry the same (non-empty) management_token"

echo "── 6. Manager proxies /api/alarm/* to the AE (login → proxy → 200) ───"
# The manager's own auth gate answers /api/alarm/* before the proxy runs; its
# 401 body carries the auth_required marker, unlike an AE-origin 401.
resp="$(curl -sk -m 10 -w '|%{http_code}' -H 'Authorization: Bearer bogus-client-token' \
  "$MGR_URL/api/alarm/rules" || true)"
anon_code="${resp##*|}"; anon_body="${resp%|*}"
case "$anon_body" in
  *auth_required*) : ;;
  *) fail "anonymous probe: code=${anon_code:-000} body=${anon_body:-<empty>} (want the manager auth-gate 401; 000 = connect/TLS broken)" ;;
esac
# Log in with the seeded admin (same helper as ci-agent-tls-smoke.sh), then
# assert the real chain: session auth → proxy → management bearer → AE HTTPS.
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT
if ! login_code="$(ci_admin_login "$COOKIE_JAR")"; then
  fail "manager login returned $login_code (want 302/303) — seeded admin missing, creds changed, or login rate-limited?"
fi
c="$(code -b "$COOKIE_JAR" "$MGR_URL/api/alarm/rules")"
if [ "$c" != "200" ]; then fail "authenticated manager → AE proxy = $c (want 200)"; fi
pass "auth gate 401s anonymous probes; logged-in proxy reaches the AE (200)"

echo "── 7. AE CORS allow-list carries the manager origin ──────────────────"
AE_CORS="$(toml_get "$AE_TOML" alarm_engine cors_origins)"
case "$AE_CORS" in
  *"$DETECTED_IP:5000"*"$DETECTED_IP:8081"*) : ;;
  *) fail "AE CORS missing expected origins: [$AE_CORS]" ;;
esac
if python3 -c 'import sys,tomllib; sys.exit(0 if "cors_origins" in tomllib.load(open(sys.argv[1],"rb")).get("manager",{}) else 1)' "$MGR_TOML"; then
  fail "manager TOML still carries the removed [manager].cors_origins key"
fi
pass "AE CORS contains both manager + AE origins; manager key absent"

echo "── 8. AE TLS cert SAN covers the detected IP ─────────────────────────"
if ! openssl x509 -in "$AE_CERT" -noout -text | grep -A1 'Subject Alternative Name' | grep -qF "$DETECTED_IP"; then
  fail "AE cert SAN does not cover $DETECTED_IP"
fi
pass "AE cert SAN includes $DETECTED_IP"

echo
echo "ALL SPLIT-INSTALL (modes 3 + 4) ASSERTIONS PASSED"
