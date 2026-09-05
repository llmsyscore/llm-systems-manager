#!/usr/bin/env bash
# Backup + restore round-trip oracle for CI (#856). Seeds state on the manager
# and the alarm engine, runs one scheduled-backup cycle, destroys the seeded
# state, restores both components from the archives and asserts the state is
# back and the system is operational. Run as root on a host with both units.
#
# Env: MGR_DIR (manager install root), AE_DIR (AE install root, defaults to
# MGR_DIR for a co-located install), MGR_URL, AE_URL (probed with -k),
# PASSPHRASE (set = encrypted archives), ADMIN_USER/ADMIN_PW (seeded admin).
#
# NEVER run against a live deployment: it stops units and deletes state.
set -euo pipefail

MGR_DIR="${MGR_DIR:-/opt/llm-systems-manager}"
AE_DIR="${AE_DIR:-$MGR_DIR}"
MGR_URL="${MGR_URL:-http://127.0.0.1:5000}"
AE_URL="${AE_URL:-https://127.0.0.1:8081}"
PASSPHRASE="${PASSPHRASE:-}"
ADMIN_USER="${ADMIN_USER:-llmadmin}"
ADMIN_PW="${ADMIN_PW:-llmadmin-ci-rotated}"
SHIPPED_PW="llmadmin"
MGR_UNIT=llm-systems-manager
AE_UNIT=llm-systems-alarm-engine
AGENT_UNIT=llm-systems-agent

MGR_TOML="$MGR_DIR/config/llm-systems.toml"
AE_TOML="$AE_DIR/config/llm-systems.toml"
MGR_DATA="$MGR_DIR/data"
AE_DATA="$AE_DIR/llm-systems-alarm-engine/data"
BACKUP_DIR="$MGR_DATA/backups"
USERS_FILE="$MGR_DATA/manager_users.json"

SEED_USER="ci-restore-user"
SEED_PW="Restore-Me-2026"
SEED_PALETTE="forest"
SEED_KEEP_LAST=3
RULE_NAME="ci-restore-rule"
CHAN_NAME="ci-restore-channel"
if [ -n "$PASSPHRASE" ]; then ENC=true; else ENC=false; fi

WORK="$(mktemp -d)"
JAR="$WORK/admin.cookies"
trap 'rm -rf "$WORK"' EXIT

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ FAIL: $*"; exit 1; }

# HTTP status only; -k for the AE's internal-CA cert. 000 on connect failure.
code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 30 "$@" || true; }
# Body + status: prints the body; the status is read back with last_code
# (call runs inside $(...), so it cannot set a shell variable).
call() {
  local out
  out="$(curl -sk --max-time 120 -w '\n%{http_code}' "$@" || true)"
  printf '%s' "${out##*$'\n'}" > "$WORK/last_code"
  printf '%s' "${out%$'\n'*}"
}
last_code() { cat "$WORK/last_code" 2>/dev/null || echo 000; }
# Manager call with the admin session cookie.
mgr() { call -b "$JAR" "$@"; }
# AE call with the management bearer.
ae() { call -H "Authorization: Bearer $MGMT" "$@"; }

# jq_ok BODY FILTER DESC [jq args...] — jq -e must succeed on the body.
jq_ok() {
  local body="$1" filter="$2" desc="$3"
  shift 3
  if ! jq -e "$@" "$filter" >/dev/null 2>&1 <<<"$body"; then
    fail "$desc — body: ${body:0:600}"
  fi
}
jq_get() { jq -r "$2" <<<"$1"; }

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
  for _ in $(seq 1 60); do
    if systemctl is-active --quiet "$1"; then return 0; fi
    sleep 1
  done
  return 1
}
wait_health() {
  for _ in $(seq 1 60); do
    if [ "$(code "$1")" = "200" ]; then return 0; fi
    sleep 1
  done
  return 1
}
restart_unit() {
  local unit="$1" health="$2"
  systemctl restart "$unit"
  wait_active "$unit" || fail "$unit not active after restart"
  wait_health "$health" || fail "$unit restarted but $health never 200"
}
# login USER PW JAR — fresh session; prints the /login status.
login() {
  rm -f "$3"
  code -c "$3" --data-urlencode "username=$1" --data-urlencode "password=$2" "$MGR_URL/login"
}
# admin_login — admin session in $JAR; a first login on a fresh install
# rotates the shipped default password to ADMIN_PW (mandatory-change wall).
admin_login() {
  local c
  c="$(login "$ADMIN_USER" "$ADMIN_PW" "$JAR")"
  case "$c" in 302|303) return 0 ;; esac
  c="$(login "$ADMIN_USER" "$SHIPPED_PW" "$JAR")"
  case "$c" in 302|303) : ;; *) fail "admin login returned $c (want 302/303)" ;; esac
  code -b "$JAR" -X POST -H 'Content-Type: application/json' \
    -d "{\"current_password\":\"$SHIPPED_PW\",\"new_password\":\"$ADMIN_PW\"}" "$MGR_URL/api/account/password" >/dev/null
  c="$(login "$ADMIN_USER" "$ADMIN_PW" "$JAR")"
  case "$c" in 302|303) : ;; *) fail "admin login after password rotation returned $c (want 302/303)" ;; esac
}
# unit_present UNIT — the unit file is installed on this host.
unit_present() { systemctl cat "$1" >/dev/null 2>&1; }

echo "── 0. Preconditions ─────────────────────────────────────────────────"
wait_active "$AE_UNIT"  || fail "$AE_UNIT not active"
wait_active "$MGR_UNIT" || fail "$MGR_UNIT not active"
wait_health "$MGR_URL/health" || fail "manager /health never 200 on $MGR_URL"
wait_health "$AE_URL/health"  || fail "AE /health never 200 on $AE_URL"
admin_login
MGMT="$(toml_get "$AE_TOML" alarm_engine management_token)"
[ -n "$MGMT" ] || fail "[alarm_engine].management_token is empty in $AE_TOML"
cp "$MGR_TOML" "$WORK/pristine-mgr.toml"
if [ "$AE_TOML" != "$MGR_TOML" ]; then cp "$AE_TOML" "$WORK/pristine-ae.toml"; fi
body="$(mgr "$MGR_URL/api/admin/backup-status")"
jq_ok "$body" '.ok == true and .enabled == true and .not_covered == {}' \
  "scheduler must be enabled with the alarm engine covered before seeding"
pass "units up, admin session, management token read, AE covered by the scheduler"

echo "── 1. Seed distinctive state ────────────────────────────────────────"
changes="{\"manager.branding.palette\":\"$SEED_PALETTE\",\"manager.backup.keep_last\":$SEED_KEEP_LAST}"
if [ -n "$PASSPHRASE" ]; then
  changes="$(jq -c --arg p "$PASSPHRASE" '. + {"manager.backup.passphrase": $p}' <<<"$changes")"
fi
body="$(mgr -X PUT -H 'Content-Type: application/json' -d "{\"changes\":$changes}" "$MGR_URL/api/admin/settings")"
jq_ok "$body" '.ok == true and (.applied | index("manager.branding.palette")) != null' "settings PUT"
body="$(mgr "$MGR_URL/api/admin/settings")"
jq_ok "$body" ".values[\"manager.branding.palette\"] == \"$SEED_PALETTE\"" "palette seeded"
body="$(mgr "$MGR_URL/api/admin/backup-status")"
jq_ok "$body" ".keep_last == $SEED_KEEP_LAST and .encrypted == $ENC" "keep_last + encryption seeded (hot reload)"
pass "settings: palette=$SEED_PALETTE keep_last=$SEED_KEEP_LAST encrypted=$ENC"

body="$(mgr -X POST -H 'Content-Type: application/json' \
  -d "{\"username\":\"$SEED_USER\",\"password\":\"$SEED_PW\",\"role\":\"operator\"}" "$MGR_URL/api/admin/users")"
jq_ok "$body" '.ok == true' "user create"
c="$(login "$SEED_USER" "$SEED_PW" "$WORK/seed.cookies")"
case "$c" in 302|303) : ;; *) fail "seeded user login returned $c (want 302/303)" ;; esac
USERS_SHA_BEFORE="$(sha256sum "$USERS_FILE" | cut -d' ' -f1)"
pass "dashboard user $SEED_USER created and can log in"

body="$(ae -X POST -H 'Content-Type: application/json' \
  -d "{\"name\":\"$CHAN_NAME\",\"channel_type\":\"webhook\",\"config\":{\"webhook\":{\"url\":\"http://127.0.0.1:9/ci-restore\"}}}" \
  "$AE_URL/api/alarm/notifications/channels")"
[ "$(last_code)" = "201" ] || fail "AE channel create = $(last_code) — ${body:0:300}"
CHAN_ID="$(jq_get "$body" '.channel_id')"
[ -n "$CHAN_ID" ] && [ "$CHAN_ID" != "null" ] || fail "no channel_id in ${body:0:300}"
body="$(ae -X POST -H 'Content-Type: application/json' \
  -d "{\"name\":\"$RULE_NAME\",\"metric_source\":\"cpu\",\"metric_name\":\"cpu_total\",\"rule_type\":\"threshold_above\",\"config\":{\"threshold\":{\"value\":99.5}},\"severity\":\"warning\",\"notification_channel_ids\":[\"$CHAN_ID\"]}" \
  "$AE_URL/api/alarm/rules")"
[ "$(last_code)" = "200" ] || fail "AE rule create = $(last_code) — ${body:0:300}"
RULE_ID="$(jq_get "$body" '.rule_id')"
[ -n "$RULE_ID" ] && [ "$RULE_ID" != "null" ] || fail "no rule_id in ${body:0:300}"
# The fields a restore must bring back verbatim.
rule_fp() { jq -S '{name, metric_source, metric_name, rule_type, severity, enabled, notification_channel_ids, v: .config.threshold.value}' <<<"$1"; }
RULE_FP_BEFORE="$(rule_fp "$body")"
body="$(mgr "$MGR_URL/api/alarm/rules/$RULE_ID")"
[ "$(last_code)" = "200" ] || fail "manager → AE proxy for the seeded rule = $(last_code)"
pass "AE channel $CHAN_NAME + rule $RULE_NAME created; rule visible through the manager proxy"

echo "── 2. Scheduled backup run (backup-now = the scheduler's code path) ─"
body="$(mgr -X POST "$MGR_URL/api/admin/backup-now")"
[ "$(last_code)" = "200" ] || fail "backup-now = $(last_code) — ${body:0:600}"
jq_ok "$body" '.ok == true and .last.ok == true and .last.partial == false' "backup-now must succeed without a partial run"
jq_ok "$body" '.last.components.manager.ok == true and .last.components.manager.files >= 5' "manager archive must hold the export files"
jq_ok "$body" '.last.components.alarm_engine.ok == true and .last.components.alarm_engine.skipped == null and (.last.components.alarm_engine.file | type) == "string"' \
  "scheduled run must capture the alarm engine (#855)"
jq_ok "$body" ".last.encrypted == $ENC" "archive encryption must follow the passphrase"
MGR_ARCHIVE="$(jq_get "$body" '.last.components.manager.file')"
AE_ARCHIVE="$(jq_get "$body" '.last.components.alarm_engine.file')"
RUN1="$(jq_get "$body" '.last.ts')"
for f in "$MGR_ARCHIVE" "$AE_ARCHIVE"; do
  [ -s "$BACKUP_DIR/$f" ] || fail "archive $f missing or empty under $BACKUP_DIR"
done
body="$(mgr "$MGR_URL/api/admin/backup-status")"
jq_ok "$body" \
  '([.backups[] | select(.file == $m and .component == "manager")] | length) == 1 and ([.backups[] | select(.file == $a and .component == "alarm_engine")] | length) == 1' \
  "backup-status must list both archives" --arg m "$MGR_ARCHIVE" --arg a "$AE_ARCHIVE"
run_m="$(jq -r --arg m "$MGR_ARCHIVE" '.backups[] | select(.file == $m) | .run' <<<"$body")"
run_a="$(jq -r --arg a "$AE_ARCHIVE" '.backups[] | select(.file == $a) | .run' <<<"$body")"
[ -n "$run_m" ] && [ "$run_m" = "$run_a" ] || fail "manager/AE archives carry different run stamps: $run_m vs $run_a"
# Download both the way the operator does and compare with the on-disk files.
for f in "$MGR_ARCHIVE" "$AE_ARCHIVE"; do
  c="$(curl -sk -b "$JAR" -o "$WORK/$f" -w '%{http_code}' --max-time 60 "$MGR_URL/api/admin/backup-archive/$f" || true)"
  [ "$c" = "200" ] || fail "backup-archive download of $f = $c"
  cmp -s "$WORK/$f" "$BACKUP_DIR/$f" || fail "downloaded $f differs from the on-disk archive"
done
pass "run $run_m: $MGR_ARCHIVE + $AE_ARCHIVE written, listed, downloaded byte-identical"

# Manifest guards: each component refuses the other's archive.
body="$(mgr -X POST -F "file=@$WORK/$AE_ARCHIVE" -F "password=$PASSPHRASE" "$MGR_URL/api/admin/import/manager/preview")"
[ "$(last_code)" = "400" ] || fail "manager preview accepted the AE archive ($(last_code)) — ${body:0:300}"
body="$(ae -X POST -F "file=@$WORK/$MGR_ARCHIVE" -F "password=$PASSPHRASE" "$AE_URL/api/alarm/admin/import/preview")"
[ "$(last_code)" = "400" ] || fail "AE preview accepted the manager archive ($(last_code)) — ${body:0:300}"
pass "manifest guards reject the other component's archive"

echo "── 3. Destroy the seeded state ──────────────────────────────────────"
systemctl stop "$MGR_UNIT"
install -o llmsys -g llmsys -m 0600 "$WORK/pristine-mgr.toml" "$MGR_TOML"
rm -f "$USERS_FILE"
systemctl start "$MGR_UNIT"
wait_active "$MGR_UNIT" || fail "$MGR_UNIT not active after the destroy restart"
wait_health "$MGR_URL/health" || fail "manager unhealthy after the destroy restart"
admin_login
body="$(mgr "$MGR_URL/api/admin/users")"
jq_ok "$body" '[.users[] | select(.username == $u)] | length == 0' "seeded user must be gone" --arg u "$SEED_USER"
c="$(login "$SEED_USER" "$SEED_PW" "$WORK/seed.cookies")"
case "$c" in 302|303) fail "seeded user can still log in after destroy" ;; esac
body="$(mgr "$MGR_URL/api/admin/settings")"
jq_ok "$body" ".values[\"manager.branding.palette\"] != \"$SEED_PALETTE\"" "palette must be back to default"
body="$(mgr "$MGR_URL/api/admin/backup-status")"
jq_ok "$body" ".keep_last != $SEED_KEEP_LAST and .encrypted == false" "backup settings must be back to default"
pass "manager: users file removed, TOML reverted, default admin re-seeded, seeded state gone"

systemctl stop "$AE_UNIT"
if [ -f "$WORK/pristine-ae.toml" ]; then
  install -o llmsys -g llmsys -m 0600 "$WORK/pristine-ae.toml" "$AE_TOML"
fi
rm -f "$AE_DATA"/ae_notif_rules.db "$AE_DATA"/ae_notif_rules.db-* "$AE_DATA"/ae_alarms.db "$AE_DATA"/ae_alarms.db-*
systemctl start "$AE_UNIT"
wait_active "$AE_UNIT" || fail "$AE_UNIT not active after the destroy restart"
wait_health "$AE_URL/health" || fail "AE unhealthy after the destroy restart"
c="$(code -H "Authorization: Bearer $MGMT" "$AE_URL/api/alarm/rules/$RULE_ID")"
[ "$c" = "404" ] || fail "seeded rule still answers $c after the AE DBs were removed"
c="$(code -H "Authorization: Bearer $MGMT" "$AE_URL/api/alarm/notifications/channels/$CHAN_ID")"
[ "$c" = "404" ] || fail "seeded channel still answers $c after the AE DBs were removed"
pass "alarm engine: rules + alarms DBs removed, rule/channel gone"

echo "── 4. Restore both components from the archives ─────────────────────"
body="$(mgr -X POST -F "file=@$WORK/$MGR_ARCHIVE" -F "password=$PASSPHRASE" "$MGR_URL/api/admin/import/manager/preview")"
[ "$(last_code)" = "200" ] || fail "manager preview = $(last_code) — ${body:0:600}"
jq_ok "$body" ".ok == true and .encrypted == $ENC and .manifest.component == \"manager\"" "manager preview manifest"
for want in config/llm-systems.toml data/manager_users.json data/internal-ca.crt data/internal-ca.key data/manager_secret; do
  jq_ok "$body" '[.entries[] | select(.name == $n and .size > 0)] | length == 1' "manager archive lacks $want" --arg n "$want"
done
body="$(mgr -X POST -F "file=@$WORK/$MGR_ARCHIVE" -F "password=$PASSPHRASE" \
  -F 'categories=["config","identity"]' "$MGR_URL/api/admin/import/manager/apply")"
[ "$(last_code)" = "200" ] || fail "manager apply = $(last_code) — ${body:0:600}"
jq_ok "$body" '.ok == true and ([.written[] | select(endswith("config/llm-systems.toml"))] | length) == 1 and ([.written[] | select(endswith("data/manager_users.json"))] | length) == 1' \
  "manager apply must write the TOML and the users file"
USERS_SHA_AFTER="$(sha256sum "$USERS_FILE" | cut -d' ' -f1)"
[ "$USERS_SHA_BEFORE" = "$USERS_SHA_AFTER" ] || fail "restored manager_users.json differs from the backed-up copy"
restart_unit "$MGR_UNIT" "$MGR_URL/health"
pass "manager archive previewed, applied (config + identity), service restarted"

body="$(ae -X POST -F "file=@$WORK/$AE_ARCHIVE" -F "password=$PASSPHRASE" "$AE_URL/api/alarm/admin/import/preview")"
[ "$(last_code)" = "200" ] || fail "AE preview = $(last_code) — ${body:0:600}"
jq_ok "$body" ".ok == true and .encrypted == $ENC and .manifest.component == \"alarm_engine\"" "AE preview manifest"
jq_ok "$body" '[.entries[] | select(.name == "data/ae_notif_rules.db" and .size > 0)] | length == 1' "AE archive lacks data/ae_notif_rules.db"
body="$(ae -X POST -F "file=@$WORK/$AE_ARCHIVE" -F "password=$PASSPHRASE" "$AE_URL/api/alarm/admin/import/apply")"
[ "$(last_code)" = "200" ] || fail "AE apply = $(last_code) — ${body:0:600}"
jq_ok "$body" '.ok == true and ([.written[] | select(endswith("ae_notif_rules.db"))] | length) == 1' \
  "AE apply must write the rules DB"
restart_unit "$AE_UNIT" "$AE_URL/health"
pass "alarm-engine archive previewed, applied, service restarted"

echo "── 5. Seeded state is back and the system is operational ───────────"
admin_login
body="$(mgr "$MGR_URL/api/admin/settings")"
jq_ok "$body" ".values[\"manager.branding.palette\"] == \"$SEED_PALETTE\"" "palette restored"
body="$(mgr "$MGR_URL/api/admin/backup-status")"
jq_ok "$body" ".keep_last == $SEED_KEEP_LAST and .encrypted == $ENC and .not_covered == {}" "backup settings restored, AE still covered"
body="$(mgr "$MGR_URL/api/admin/users")"
jq_ok "$body" '[.users[] | select(.username == $u and .role == "operator")] | length == 1' "seeded user missing after restore" --arg u "$SEED_USER"
c="$(login "$SEED_USER" "$SEED_PW" "$WORK/seed.cookies")"
case "$c" in 302|303) : ;; *) fail "seeded user login after restore returned $c (want 302/303)" ;; esac
pass "manager: settings + dashboard user restored, user can log in"

body="$(ae "$AE_URL/api/alarm/rules/$RULE_ID")"
[ "$(last_code)" = "200" ] || fail "restored rule GET = $(last_code) — ${body:0:300}"
[ "$(rule_fp "$body")" = "$RULE_FP_BEFORE" ] || fail "restored rule differs: $(rule_fp "$body") vs $RULE_FP_BEFORE"
body="$(ae "$AE_URL/api/alarm/notifications/channels/$CHAN_ID")"
[ "$(last_code)" = "200" ] || fail "restored channel GET = $(last_code) — ${body:0:300}"
jq_ok "$body" ".name == \"$CHAN_NAME\" and .channel_type == \"webhook\"" "restored channel fields"
pass "alarm engine: rule + channel restored with identical fields"

body="$(mgr "$MGR_URL/api/alarm/rules/$RULE_ID")"
[ "$(last_code)" = "200" ] || fail "manager → AE proxy after restore = $(last_code)"
jq_ok "$body" ".name == \"$RULE_NAME\"" "proxied rule name"
body="$(call "$AE_URL/health")"
jq_ok "$body" '.status == "ok"' "AE /health status"
[ "$(code "$MGR_URL/health")" = "200" ] || fail "manager /health not 200"
[ "$(code -L "$MGR_URL/")" = "200" ] || fail "manager login page not 200"
systemctl is-active --quiet "$MGR_UNIT" || fail "$MGR_UNIT not active"
systemctl is-active --quiet "$AE_UNIT"  || fail "$AE_UNIT not active"
if unit_present "$AGENT_UNIT"; then
  wait_active "$AGENT_UNIT" || fail "$AGENT_UNIT not active after the identity restore"
fi
pass "health, login page, manager↔AE link and units all good"

body="$(mgr -X POST "$MGR_URL/api/admin/backup-now")"
[ "$(last_code)" = "200" ] || fail "post-restore backup-now = $(last_code) — ${body:0:600}"
jq_ok "$body" '.ok == true and .last.partial == false and .last.components.alarm_engine.ok == true' "post-restore scheduled run must cover both components"
RUN2="$(jq_get "$body" '.last.ts')"
[ "$RUN2" != "$RUN1" ] || fail "post-restore run reused the first run's timestamp"
pass "a fresh scheduled run after restore captures both components"

echo
echo "ALL BACKUP + RESTORE ASSERTIONS PASSED (encrypted=$ENC, mgr=$MGR_DIR, ae=$AE_DIR)"
