#!/usr/bin/env bash
# =============================================================================
# tools/installer/install-influxdb.sh — installs and provisions InfluxDB v2
#
# Linux-only. Adds the InfluxData apt repo + GPG key, installs influxdb2 +
# influxdb2-cli, starts the service, runs `influx setup` if first-boot, then
# creates the three buckets the alarm engine uses and a read+write scoped
# token for each.
#
# Output is a key=value file written to:
#     $LLMSYS_INSTALL_DIR/data/influxdb.env
# containing INFLUX_ORG, INFLUX_HOST, INFLUX_OPERATOR_TOKEN, and per-bucket
# tokens. install-config-bootstrap.sh consumes this to write tokens into
# the real config TOML.
#
# Idempotent: re-runs reuse the operator token stashed at
# /root/.influxdb-operator-token and skip already-configured steps.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

detect_os
require_linux
detect_sudo

# Mirror everything we say + everything we run into a debug log. Operators
# can read /tmp/llm-systems-influxdb-install.log after any failure.
INFLUX_LOG=/tmp/llm-systems-influxdb-install.log
: >"$INFLUX_LOG"
exec > >(tee -a "$INFLUX_LOG") 2> >(tee -a "$INFLUX_LOG" >&2)
trap 'rc=$?; echo "[ERR ]  install-influxdb.sh aborted at line $LINENO (exit $rc) — full log: $INFLUX_LOG" >&2' ERR

INSTALL_DIR="${LLMSYS_INSTALL_DIR}"
INFLUX_ORG_DEFAULT="llm-systems-manager"
INFLUX_URL="http://localhost:8086"
ENV_FILE="$LLMSYS_INFLUXDB_TOKEN_FILE"

banner "InfluxDB — apt repo + install"

# Prereqs
APT_PKGS=(wget gnupg ca-certificates curl jq openssl)
MISSING=()
mapfile -t MISSING < <(missing_apt_pkgs "${APT_PKGS[@]}")
NONEMPTY=()
for p in "${MISSING[@]}"; do [[ -n "$p" ]] && NONEMPTY+=("$p"); done
if (( ${#NONEMPTY[@]} > 0 )); then
  offer_apt_install "${NONEMPTY[@]}" || die "Required apt packages missing"
fi

KEYRING=/etc/apt/keyrings/influxdata-archive.gpg
SOURCE=/etc/apt/sources.list.d/influxdata.list
if [[ ! -f "$KEYRING" ]]; then
  $SUDO install -d -m 0755 /etc/apt/keyrings
  wget -qO- https://repos.influxdata.com/influxdata-archive.key \
    | $SUDO gpg --dearmor -o "$KEYRING"
  $SUDO chmod 0644 "$KEYRING"
  ok "added InfluxData GPG key → $KEYRING"
else
  log "GPG key already present"
fi
if [[ ! -f "$SOURCE" ]]; then
  echo "deb [signed-by=$KEYRING] https://repos.influxdata.com/debian stable main" \
    | $SUDO tee "$SOURCE" >/dev/null
  ok "added InfluxData apt source → $SOURCE"
else
  log "apt source already present"
fi
# Use the clock-recovery wrapper — this apt-get update comes right after
# adding the InfluxData repo and is the most likely place to hit a VM
# clock skew on a freshly-rolled-back snapshot.
apt_update_with_clock_recovery
$SUDO apt-get install -y --no-install-recommends influxdb2 influxdb2-cli
ok "influxdb2 + influxdb2-cli installed"

banner "InfluxDB — start service"
$SUDO systemctl enable --now influxdb
# Wait for health
for i in {1..30}; do
  code="$(probe_url "$INFLUX_URL/health")"
  [[ "$code" == "200" ]] && break
  sleep 1
done
[[ "$(probe_url "$INFLUX_URL/health")" == "200" ]] \
  || die "InfluxDB did not become healthy on $INFLUX_URL/health"
ok "InfluxDB healthy"

banner "InfluxDB — first-boot setup"
INFLUX_OPERATOR_TOKEN=""
INFLUX_ORG="$INFLUX_ORG_DEFAULT"
FRESH_SETUP=0

# Reuse stashed token if present
if $SUDO test -s /root/.influxdb-operator-token; then
  log "operator token already on disk — reusing"
  INFLUX_OPERATOR_TOKEN=$($SUDO tr -d '[:space:]' < /root/.influxdb-operator-token)
  EXISTING_ORG=$($SUDO influx config list --json 2>/dev/null | jq -r '.[0].org // empty' || true)
  [[ -n "$EXISTING_ORG" ]] && INFLUX_ORG="$EXISTING_ORG"
elif $SUDO influx config list --json 2>/dev/null | jq -e 'length > 0' >/dev/null 2>&1; then
  log "CLI config has an entry — reading operator token from it"
  INFLUX_OPERATOR_TOKEN=$($SUDO influx config list --json | jq -r '.[0].token' | tr -d '[:space:]')
  INFLUX_ORG=$($SUDO influx config list --json | jq -r '.[0].org')
else
  log "fresh InfluxDB — running 'influx setup'"
  FRESH_SETUP=1
  INFLUX_OPERATOR_TOKEN=$(openssl rand -hex 32)
  ADMIN_PASS=$(openssl rand -hex 24)
  $SUDO influx setup \
      --username admin \
      --password "$ADMIN_PASS" \
      --org "$INFLUX_ORG" \
      --bucket alarm_engine_metrics \
      --retention 720h \
      --token "$INFLUX_OPERATOR_TOKEN" \
      --force
  ok "InfluxDB initialized (org=$INFLUX_ORG)"
  # Mode 6 is a DB-only host — the operator records the values out-of-band
  # at the end of this run, so don't litter /root with stash files. Other
  # modes keep the stashes so install-influxdb.sh can be re-run
  # idempotently (token reuse loop at the top of this block reads them).
  if [[ "${LLMSYS_INSTALL_MODE:-}" != "6" ]]; then
    $SUDO bash -c "umask 077; printf '%s\n' '$INFLUX_OPERATOR_TOKEN' > /root/.influxdb-operator-token"
    $SUDO bash -c "umask 077; printf 'admin\n%s\n' '$ADMIN_PASS' > /root/.influxdb-admin-pass"
  fi
  $SUDO influx config create \
      --config-name default \
      --host-url "$INFLUX_URL" \
      --org "$INFLUX_ORG" \
      --token "$INFLUX_OPERATOR_TOKEN" \
      --active 2>/dev/null || true
fi

_influx() { $SUDO influx "$@" --host "$INFLUX_URL" --token "$INFLUX_OPERATOR_TOKEN"; }

banner "InfluxDB — buckets"
for name in alarm_engine_metrics alarm_engine_metrics_rollup; do
  if _influx bucket list --org "$INFLUX_ORG" --json 2>/dev/null \
       | jq -e --arg n "$name" '.[] | select(.name==$n)' >/dev/null; then
    # `influx setup` always creates the initial bucket (alarm_engine_metrics)
    # — on a fresh install treat that as a creation, not a pre-existing one.
    if [[ "$FRESH_SETUP" == "1" && "$name" == "alarm_engine_metrics" ]]; then
      ok "created bucket $name"
    else
      log "bucket $name already exists"
    fi
  else
    case "$name" in
      alarm_engine_metrics_rollup)   _influx bucket create --name "$name" --org "$INFLUX_ORG" >/dev/null ;;
      *)                             _influx bucket create --name "$name" --org "$INFLUX_ORG" --retention 720h >/dev/null ;;
    esac
    ok "created bucket $name"
  fi
done

banner "InfluxDB — scoped tokens"
declare -A SCOPED_TOKENS=()
for name in alarm_engine_metrics alarm_engine_metrics_rollup; do
  bid=$(_influx bucket list --org "$INFLUX_ORG" --json | jq -r --arg n "$name" '.[] | select(.name==$n) | .id')
  if [[ -z "$bid" || "$bid" == "null" ]]; then
    warn "bucket $name not found — skipping token"
    continue
  fi
  tok=$(_influx auth create \
        --org "$INFLUX_ORG" \
        --read-bucket "$bid" \
        --write-bucket "$bid" \
        --description "$name scoped (installer $(date -Iseconds))" \
        --json | jq -r '.token')
  if [[ -z "$tok" || "$tok" == "null" ]]; then
    die "failed to create scoped token for $name"
  fi
  SCOPED_TOKENS[$name]="$tok"
  ok "created scoped token for $name"
done

if [[ "${LLMSYS_INSTALL_MODE:-}" == "6" ]]; then
  # DB-only host: nothing on this box consumes the env file (the AE lives
  # elsewhere). Print to stdout for the operator to record, write nothing
  # to disk under $INSTALL_DIR. Admin pass is only known on a fresh setup.
  banner "InfluxDB — RECORD THESE NOW (not persisted to disk)"
  echo
  echo "  ╔═══════════════════════════════════════════════════════════════════╗"
  echo "  ║                                                                   ║"
  echo "  ║   These values are NOT saved anywhere on this host. Copy them     ║"
  echo "  ║   into the alarm-engine host's config now — there is no second    ║"
  echo "  ║   chance to look them up later without rotating tokens.           ║"
  echo "  ║                                                                   ║"
  echo "  ╚═══════════════════════════════════════════════════════════════════╝"
  echo
  echo "  INFLUX_HOST                  = $INFLUX_URL"
  echo "  INFLUX_ORG                   = $INFLUX_ORG"
  echo "  INFLUX_OPERATOR_TOKEN        = $INFLUX_OPERATOR_TOKEN"
  echo "  INFLUX_METRICS_TOKEN         = ${SCOPED_TOKENS[alarm_engine_metrics]:-<not generated>}"
  echo "  INFLUX_METRICS_ROLLUP_TOKEN  = ${SCOPED_TOKENS[alarm_engine_metrics_rollup]:-<not generated>}"
  if [[ "${FRESH_SETUP:-0}" == "1" ]]; then
    echo
    echo "  Admin web UI login (http://<this-host>:8086/):"
    echo "    username = admin"
    echo "    password = $ADMIN_PASS"
  else
    echo
    echo "  (admin password not re-printed: this run reused an existing"
    echo "   InfluxDB install. Reset it via 'influx user password' if lost.)"
  fi
  echo
  echo "  On the alarm-engine host, put these under [influxdb] /"
  echo "  [influxdb.tokens] in config/llm-systems.toml."
  echo
else
  if [[ -d "$INSTALL_DIR/llm-systems-alarm-engine" ]]; then
    as_run_user mkdir -p "$INSTALL_DIR/llm-systems-alarm-engine/data"
  fi
  write_influx_token_file "$ENV_FILE" \
    "$INFLUX_URL" "$INFLUX_ORG" "$INFLUX_OPERATOR_TOKEN" \
    "${SCOPED_TOKENS[alarm_engine_metrics]:-}" \
    "${SCOPED_TOKENS[alarm_engine_metrics_rollup]:-}"
fi

banner "InfluxDB — apply tuned config"
# Idempotent: a managed-block delimited by these markers is stripped first,
# then re-appended with current values. Re-running the installer always
# converges to whatever this script defines, without clobbering hand-edits
# the operator made outside the markers.
CONF=/etc/influxdb/config.toml
BEGIN_MARKER="# === llm-systems-manager tuning (managed) ==="
END_MARKER="# === END llm-systems-manager tuning ==="

if $SUDO test ! -f "$CONF"; then
  # The influxdb2 package ships /etc/influxdb/config.toml but doesn't
  # require it; older packagings omitted it. Create a stub so our append
  # has somewhere to land.
  $SUDO install -o root -g root -m 0644 /dev/null "$CONF"
fi

TMP_BASE="$(mktemp)"
TMP_OUT="$(mktemp)"
trap 'rm -f "$TMP_BASE" "$TMP_OUT"' EXIT
$SUDO cat "$CONF" > "$TMP_BASE"
# Strip any prior managed block before re-appending.
awk -v b="$BEGIN_MARKER" -v e="$END_MARKER" '
  $0 == b { skip=1; next }
  $0 == e { skip=0; next }
  !skip
' "$TMP_BASE" > "$TMP_OUT"
{
  cat "$TMP_OUT"
  printf '\n%s\n' "$BEGIN_MARKER"
  cat <<'BLOCK'
# Cache
storage-cache-max-memory-size              = 1073741824
storage-cache-snapshot-memory-size         = 134217728
storage-cache-snapshot-write-cold-duration = "30m"
# Compactions
storage-max-concurrent-compactions         = 1
storage-compact-throughput-burst           = "16m"
# Write path
storage-wal-fsync-delay                    = "100ms"
# Queries
query-concurrency                          = 2
query-queue-size                           = 32
query-memory-bytes                         = 268435456
query-initial-memory-bytes                 = 4194304
# Series index cache
storage-series-id-set-cache-size           = 4000
# Quiet the daemon
log-level                                  = "warn"
reporting-disabled                         = true
BLOCK
  printf '%s\n' "$END_MARKER"
} > "$TMP_BASE"
$SUDO install -o root -g root -m 0644 "$TMP_BASE" "$CONF"
rm -f "$TMP_BASE" "$TMP_OUT"; trap - EXIT
ok "tuned config block written to $CONF"

# Sudoers drop-in: lets the alarm-engine (running as llmsys) read
# InfluxDB's on-disk size via `sudo -n du -sb /var/lib/influxdb`
# without a password. /var/lib/influxdb is owned by influxdb:influxdb
# 0750, so a bare `du` from llmsys gets EACCES. influx_monitor falls
# back to None on failure, which makes the "InfluxDB Health" card
# blank-out the disk-bytes metric. Created here regardless of mode
# because the file only matters on hosts where InfluxDB is installed
# — and if AE gets colocated later, the rule is already in place.
SUDOERS_DU=/etc/sudoers.d/llmsys-du-influxdb
SUDOERS_DU_LINE="${LLMSYS_RUN_USER} ALL=(root) NOPASSWD: /usr/bin/du -sb /var/lib/influxdb"
if ! $SUDO test -f "$SUDOERS_DU" || ! $SUDO grep -qxF "$SUDOERS_DU_LINE" "$SUDOERS_DU" 2>/dev/null; then
  TMP_SUDO="$(mktemp)"
  printf '%s\n' "$SUDOERS_DU_LINE" > "$TMP_SUDO"
  if $SUDO visudo -cf "$TMP_SUDO" >/dev/null; then
    $SUDO install -o root -g root -m 0440 "$TMP_SUDO" "$SUDOERS_DU"
    ok "installed sudoers rule $SUDOERS_DU"
  else
    warn "skipped $SUDOERS_DU — visudo rejected the generated rule"
  fi
  rm -f "$TMP_SUDO"
else
  log "sudoers rule already present at $SUDOERS_DU"
fi

# Reload so the tuning takes effect now — restart is part of the install
# transaction the operator already opted into.
$SUDO systemctl restart influxdb
for _ in {1..30}; do
  [[ "$(probe_url "$INFLUX_URL/health")" == "200" ]] && break
  sleep 1
done
if [[ "$(probe_url "$INFLUX_URL/health")" != "200" ]]; then
  warn "InfluxDB did not come back healthy within 30s after tuning restart"
  warn "  check: sudo journalctl -u influxdb -n 60 --no-pager"
else
  ok "InfluxDB restarted with tuned config"
fi

cat <<EOF

InfluxDB provisioned.
  Org:                $INFLUX_ORG
  Health:             $INFLUX_URL/health
  Tuned config:       $CONF  (managed block — re-run installer to refresh)
EOF
