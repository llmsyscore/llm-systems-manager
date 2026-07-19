#!/usr/bin/env bash
# =============================================================================
# tools/installer/brew-influx-setup.sh — provision InfluxDB v2 for a Homebrew
# install and write the tokens into llm-systems.toml.
#
# Installed as `llm-systems-influx-setup` by the llm-systems-manager formula.
# Run once after `brew install influxdb@2 influxdb-cli` (the plain `influxdb`
# formula is InfluxDB 3.x now — no v2 API, so this stack can't use it):
#   - starts the influxdb@2 brew service if nothing answers on /health
#   - first boot: onboards via `influx setup` (generated admin password +
#     operator token); re-runs reuse the active influx CLI config
#   - creates the alarm_engine_metrics + alarm_engine_metrics_rollup buckets
#     and a read+write scoped token for each
#   - writes [influxdb.tokens] metrics / metrics_rollup / admin into the
#     shared config, then prints the service restart commands
#
# Env overrides:
#   LSM_BREW_CONFIG    config path (default $(brew --prefix)/etc/llm-systems-manager/llm-systems.toml)
#   INFLUX_HOST_URL    server URL  (default http://localhost:8086)
#   INFLUX_ORG         org name    (default llm-systems-manager)
#   LSM_INFLUX_WAIT_S  health-wait ceiling in seconds (default 120)
#
# No sudo; macOS bash 3.2 safe. Idempotent — a config whose tokens are
# already filled is left untouched.
# =============================================================================
set -euo pipefail

log()  { echo "[influx-setup] $*"; }
ok()   { echo "[influx-setup] OK: $*"; }
warn() { echo "[influx-setup] WARN: $*" >&2; }
die()  { echo "[influx-setup] ERROR: $*" >&2; exit 1; }

INFLUX_URL="${INFLUX_HOST_URL:-http://localhost:8086}"
INFLUX_ORG="${INFLUX_ORG:-llm-systems-manager}"
# Homebrew's `influxdb` formula is InfluxDB 3.x (different API, no /health
# on :8086); the v2 server this stack needs lives in the versioned formula.
INFLUX_FORMULA="influxdb@2"

CONFIG="${LSM_BREW_CONFIG:-}"
if [ -z "$CONFIG" ]; then
  command -v brew >/dev/null 2>&1 || die "brew not found — set LSM_BREW_CONFIG to your llm-systems.toml path"
  CONFIG="$(brew --prefix)/etc/llm-systems-manager/llm-systems.toml"
fi
[ -f "$CONFIG" ] || die "config not found: $CONFIG — install llm-systems-manager first"

command -v influx >/dev/null 2>&1 \
  || die "the 'influx' CLI is not installed (it is separate from the server): brew install influxdb-cli"
command -v curl >/dev/null 2>&1 || die "curl not found"

# Exact seeded lines — untouched means the token still needs generating.
NEED_METRICS=0; NEED_ROLLUP=0; NEED_ADMIN=0
grep -q '^metrics        = "REPLACE_ME"' "$CONFIG" && NEED_METRICS=1
grep -q '^metrics_rollup = "REPLACE_ME"' "$CONFIG" && NEED_ROLLUP=1
grep -q '^admin    = ""' "$CONFIG" && NEED_ADMIN=1
if [ "$NEED_METRICS" = 0 ] && [ "$NEED_ROLLUP" = 0 ]; then
  ok "[influxdb.tokens] already filled in $CONFIG — nothing to do"
  exit 0
fi

# ── Server up? Start the brew service if needed ────────────────────────────
probe() { curl -fsS -o /dev/null -w '%{http_code}' "$INFLUX_URL/health" 2>/dev/null || true; }

# "started" / "error" / "none" per `brew services info --json`; empty if unknown.
service_state() {
  brew services info "$INFLUX_FORMULA" --json 2>/dev/null \
    | sed -n 's/.*"status": *"\([^"]*\)".*/\1/p' | head -n 1
}

dump_influx_diagnostics() {
  echo "---- brew services info $INFLUX_FORMULA ----" >&2
  brew services info "$INFLUX_FORMULA" 2>&1 | sed 's/^/  /' >&2 || true
  # influxdb@2 logs under var/log/influxdb2/; older layouts used flat files.
  for lg in "$(brew --prefix 2>/dev/null)"/var/log/influxdb*.log \
            "$(brew --prefix 2>/dev/null)"/var/log/influxdb*/*.log; do
    [ -f "$lg" ] || continue
    echo "---- tail -n 40 $lg ----" >&2
    tail -n 40 "$lg" | sed 's/^/  /' >&2 || true
  done
  echo "Hint: 'systemctl --user status homebrew.${INFLUX_FORMULA}' (Linux) or 'brew services info $INFLUX_FORMULA' for the full state." >&2
}

WAIT_S="${LSM_INFLUX_WAIT_S:-120}"
if [ "$(probe)" != "200" ]; then
  command -v brew >/dev/null 2>&1 \
    || die "no InfluxDB answering on $INFLUX_URL and brew is unavailable to start one"
  if ! brew list --versions "$INFLUX_FORMULA" >/dev/null 2>&1; then
    if brew list --versions influxdb >/dev/null 2>&1; then
      die "the installed 'influxdb' formula is InfluxDB 3.x — it has no v2 API and never answers $INFLUX_URL/health. Install the v2 server: brew install $INFLUX_FORMULA"
    fi
    die "no InfluxDB answering on $INFLUX_URL and the '$INFLUX_FORMULA' formula is not installed: brew install $INFLUX_FORMULA"
  fi
  log "starting $INFLUX_FORMULA via brew services…"
  brew services start "$INFLUX_FORMULA" >/dev/null \
    || { dump_influx_diagnostics; die "brew services start $INFLUX_FORMULA failed — diagnostics above"; }
  log "waiting for InfluxDB on $INFLUX_URL/health (up to ${WAIT_S}s)…"
  i=0
  while [ "$i" -lt "$WAIT_S" ]; do
    [ "$(probe)" = "200" ] && break
    if [ $((i % 5)) -eq 4 ]; then
      if [ "$(service_state)" = "error" ]; then
        dump_influx_diagnostics
        die "the $INFLUX_FORMULA brew service entered an error state — diagnostics above"
      fi
      printf '.' >&2
    fi
    sleep 1; i=$((i + 1))
  done
  echo >&2
fi
if [ "$(probe)" != "200" ]; then
  dump_influx_diagnostics
  die "InfluxDB did not become healthy on $INFLUX_URL/health within ${WAIT_S}s — diagnostics above"
fi
ok "InfluxDB healthy on $INFLUX_URL"

gen_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    # head reads first so no downstream stage triggers SIGPIPE under pipefail.
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    echo
  fi
}

# ── First-boot onboarding (or reuse the active CLI config) ─────────────────
# Secrets never touch argv: onboarding goes through POST /api/v2/setup with a
# 0600 body file, and later CLI calls get the token via the INFLUX_TOKEN env.
OPERATOR_TOKEN=""; ADMIN_PASS=""; FRESH_SETUP=0
if curl -fsS "$INFLUX_URL/api/v2/setup" 2>/dev/null | grep -q '"allowed": *true'; then
  FRESH_SETUP=1
  OPERATOR_TOKEN="$(gen_token)"
  ADMIN_PASS="$(gen_token | cut -c1-24)"
  log "fresh InfluxDB — onboarding via /api/v2/setup"
  umask 077
  SETUP_BODY="$(mktemp)"
  trap 'rm -f "$SETUP_BODY"' EXIT
  # All values are fixed strings or generated hex — safe to printf into JSON.
  printf '{"username":"admin","password":"%s","token":"%s","org":"%s","bucket":"alarm_engine_metrics","retentionPeriodSeconds":2592000}' \
    "$ADMIN_PASS" "$OPERATOR_TOKEN" "$INFLUX_ORG" > "$SETUP_BODY"
  curl -fsS -X POST "$INFLUX_URL/api/v2/setup" \
      -H "Content-Type: application/json" --data @"$SETUP_BODY" >/dev/null \
    || die "InfluxDB onboarding via /api/v2/setup failed"
  rm -f "$SETUP_BODY"
  ok "InfluxDB initialized (org=$INFLUX_ORG)"
else
  log "InfluxDB already set up — using the active influx CLI config"
  influx bucket list --host "$INFLUX_URL" --org "$INFLUX_ORG" >/dev/null 2>&1 \
    || die "no working influx CLI config for org '$INFLUX_ORG' — run 'influx config create' with your operator token, then re-run"
fi

# Operator token (when known) reaches the CLI via env, never argv.
run_influx() {
  if [ -n "$OPERATOR_TOKEN" ]; then
    INFLUX_TOKEN="$OPERATOR_TOKEN" influx "$@"
  else
    influx "$@"
  fi
}

# ── Buckets + scoped tokens ────────────────────────────────────────────────
bucket_id() { run_influx bucket list --host "$INFLUX_URL" --org "$INFLUX_ORG" --name "$1" --hide-headers 2>/dev/null | awk 'NR==1 {print $1}'; }
for name in alarm_engine_metrics alarm_engine_metrics_rollup; do
  if [ -n "$(bucket_id "$name")" ]; then
    log "bucket $name already exists"
    continue
  fi
  if [ "$name" = "alarm_engine_metrics" ]; then
    run_influx bucket create --host "$INFLUX_URL" --org "$INFLUX_ORG" --name "$name" --retention 720h >/dev/null
  else
    run_influx bucket create --host "$INFLUX_URL" --org "$INFLUX_ORG" --name "$name" >/dev/null
  fi
  ok "created bucket $name"
done

# Token values are URL-safe base64 — the sed never sees quotes/backslashes.
scoped_token() {
  bid="$(bucket_id "$1")"
  [ -n "$bid" ] || die "bucket $1 not found after creation"
  run_influx auth create --host "$INFLUX_URL" --org "$INFLUX_ORG" \
      --read-bucket "$bid" --write-bucket "$bid" \
      --description "$1 scoped (brew-influx-setup)" --json \
    | sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' | head -n 1
}
TOK_METRICS=""; TOK_ROLLUP=""
if [ "$NEED_METRICS" = 1 ]; then
  TOK_METRICS="$(scoped_token alarm_engine_metrics)"
  [ -n "$TOK_METRICS" ] || die "failed to create scoped token for alarm_engine_metrics"
  ok "created scoped token for alarm_engine_metrics"
fi
if [ "$NEED_ROLLUP" = 1 ]; then
  TOK_ROLLUP="$(scoped_token alarm_engine_metrics_rollup)"
  [ -n "$TOK_ROLLUP" ] || die "failed to create scoped token for alarm_engine_metrics_rollup"
  ok "created scoped token for alarm_engine_metrics_rollup"
fi

# ── Write tokens into the config (line rewrite, no sed on values) ──────────
TMP="$CONFIG.influx.$$"
umask 077
: > "$TMP"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    'metrics        = "REPLACE_ME"'*)
      if [ -n "$TOK_METRICS" ]; then printf 'metrics        = "%s"\n' "$TOK_METRICS" >> "$TMP"
      else printf '%s\n' "$line" >> "$TMP"; fi ;;
    'metrics_rollup = "REPLACE_ME"'*)
      if [ -n "$TOK_ROLLUP" ]; then printf 'metrics_rollup = "%s"\n' "$TOK_ROLLUP" >> "$TMP"
      else printf '%s\n' "$line" >> "$TMP"; fi ;;
    'admin    = ""'*)
      if [ "$NEED_ADMIN" = 1 ] && [ -n "$OPERATOR_TOKEN" ]; then printf 'admin    = "%s"\n' "$OPERATOR_TOKEN" >> "$TMP"
      else printf '%s\n' "$line" >> "$TMP"; fi ;;
    *)
      printf '%s\n' "$line" >> "$TMP" ;;
  esac
done < "$CONFIG"

if [ -n "$TOK_METRICS" ]; then
  grep -q "^metrics        = \"$TOK_METRICS\"" "$TMP" || { rm -f "$TMP"; die "metrics token rewrite failed — config drifted?"; }
fi
if [ -n "$TOK_ROLLUP" ]; then
  grep -q "^metrics_rollup = \"$TOK_ROLLUP\"" "$TMP" || { rm -f "$TMP"; die "metrics_rollup token rewrite failed — config drifted?"; }
fi
mv "$TMP" "$CONFIG"
chmod 0600 "$CONFIG"
ok "wrote [influxdb.tokens] into $CONFIG"
if [ "$NEED_ADMIN" = 1 ] && [ -z "$OPERATOR_TOKEN" ]; then
  warn "operator token unknown on this re-run — [influxdb.tokens] admin left blank (rollup task auto-creation stays off)"
fi

echo
if [ "$FRESH_SETUP" = 1 ]; then
  echo "  InfluxDB admin web UI login ($INFLUX_URL) — RECORD THIS NOW, it is not stored:"
  echo "    username = admin"
  echo "    password = $ADMIN_PASS"
  echo
fi
echo "  Restart the services to pick up the tokens:"
echo "    brew services restart llm-systems-manager"
echo "    brew services restart llm-systems-alarm-engine"
