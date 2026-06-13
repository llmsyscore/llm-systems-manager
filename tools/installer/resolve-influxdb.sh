#!/usr/bin/env bash
# =============================================================================
# tools/installer/resolve-influxdb.sh
#
# Decides where the alarm engine's InfluxDB lives, prompting the operator
# when a choice is needed:
#
#   - Local InfluxDB already responding on :8086  → offer to use it
#   - No local InfluxDB                            → ask: provide remote /
#                                                          install locally /
#                                                          skip
#
# Writes/refreshes $LLMSYS_INFLUXDB_TOKEN_FILE (transient /tmp file) so the downstream
# install-config-bootstrap.sh can read INFLUX_HOST + tokens and substitute
# them into [influxdb] + [influxdb.tokens] in llm-systems.toml.
#
# Only invoked for Modes 2 (Mgr+AE on this box) and 4 (AE only). Modes 1
# and 6 always install locally; Mode 3 has no AE and skips InfluxDB
# entirely.
#
# Env file format (compatible with what install-influxdb.sh writes):
#   INFLUX_HOST=http://<host>:<port>
#   INFLUX_ORG=<org>
#   INFLUX_OPERATOR_TOKEN=<admin token>
#   INFLUX_METRICS_TOKEN=<scoped token for alarm_engine_metrics>
#   INFLUX_METRICS_ROLLUP_TOKEN=<scoped token for alarm_engine_metrics_rollup>
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

detect_os
require_linux
detect_sudo

INSTALL_DIR="${LLMSYS_INSTALL_DIR}"
USER_ARG="${LLMSYS_RUN_USER}"
ENV_FILE="$LLMSYS_INFLUXDB_TOKEN_FILE"
LOCAL_URL="http://localhost:8086"

banner "InfluxDB — resolve target"

# ── Detect a local InfluxDB ────────────────────────────────────────────────
LOCAL_OK=0
if [[ "$(probe_url "$LOCAL_URL/health" 2>/dev/null)" == "200" ]]; then
  LOCAL_OK=1
  ok "detected local InfluxDB on $LOCAL_URL"
else
  log "no local InfluxDB responding on $LOCAL_URL"
fi

# ── Decide what to do ─────────────────────────────────────────────────────
PROMPT=true
[[ -t 0 ]] || PROMPT=false

DECISION=""
if (( LOCAL_OK )); then
  if $PROMPT; then
    read -rp "  Use the local InfluxDB? [Y/n]: " ans
    case "$(printf '%s' "${ans:-y}" | tr '[:upper:]' '[:lower:]')" in
      n|no) DECISION="" ;;   # fall through to the no-local menu
      *)    DECISION="local-existing" ;;
    esac
  else
    log "non-interactive — defaulting to local InfluxDB"
    DECISION="local-existing"
  fi
fi

if [[ -z "$DECISION" ]]; then
  if $PROMPT; then
    cat <<EOF

  No InfluxDB selected yet. Choose how the alarm engine should reach it:

    1) Use a remote InfluxDB        (paste host + tokens)
    2) Install InfluxDB locally now (provisions on this host)
    3) Skip                          (leave tokens as REPLACE_ME — you
                                      must fill them in by hand later)

EOF
    read -rp "  Choice [1-3]: " choice
  else
    log "non-interactive and no local InfluxDB — skipping (leaving tokens unset)"
    choice="3"
  fi
  case "$choice" in
    1) DECISION="remote" ;;
    2) DECISION="install-local" ;;
    3) DECISION="skip" ;;
    *) die "Invalid choice '$choice'" ;;
  esac
fi

# ── Helpers ───────────────────────────────────────────────────────────────
_write_env() {
  write_influx_token_file "$ENV_FILE" "$@"
}

# ── Apply the chosen path ────────────────────────────────────────────────
case "$DECISION" in
  install-local)
    # install-influxdb.sh writes data/influxdb.env itself. Just delegate.
    log "installing InfluxDB on this host…"
    bash "$HERE/install-influxdb.sh"
    [[ -f "$ENV_FILE" ]] || die "install-influxdb.sh finished but $ENV_FILE is missing"
    ;;

  local-existing)
    # Local InfluxDB was already running. If the env file is already on disk
    # (e.g. mode 6 ran here previously, or a prior install populated it),
    # reuse it untouched. Otherwise prompt for the three tokens — we have
    # no way to mint them without the operator's admin token.
    if $SUDO test -s "$ENV_FILE"; then
      ok "reusing existing tokens in $ENV_FILE"
    else
      warn "no $ENV_FILE on disk — the local InfluxDB wasn't provisioned by this installer."
      if ! $PROMPT; then
        die "non-interactive and no tokens available — cannot continue"
      fi
      echo "  Provide tokens for the local InfluxDB. The simplest way to mint"
      echo "  scoped tokens (if you have the operator token):"
      echo "    sudo cat /root/.influxdb-operator-token"
      echo "    influx auth create --org <org> --read-bucket <id> --write-bucket <id>"
      echo
      read -rp "  Org [llm-systems-manager]: " ORG;        ORG="${ORG:-llm-systems-manager}"
      read -rp "  Operator (admin) token: "      OP_TOKEN
      read -rp "  Metrics bucket scoped token: " METRICS_TOKEN
      read -rp "  Metrics-rollup bucket token: " ROLLUP_TOKEN
      [[ -n "$METRICS_TOKEN" ]] || die "metrics token is required"
      [[ -z "$OP_TOKEN" ]]     || validate_influx_token "operator token"      "$OP_TOKEN"     0 || die "operator token failed sanity check"
      validate_influx_token "metrics token"        "$METRICS_TOKEN" 1 || die "metrics token failed sanity check"
      [[ -z "$ROLLUP_TOKEN" ]] || validate_influx_token "metrics-rollup token" "$ROLLUP_TOKEN" 1 || die "metrics-rollup token failed sanity check"
      _write_env "$LOCAL_URL" "$ORG" "$OP_TOKEN" "$METRICS_TOKEN" "$ROLLUP_TOKEN"
    fi
    ;;

  remote)
    if ! $PROMPT; then
      die "non-interactive but no remote InfluxDB details available — re-run interactively or set up tokens at $ENV_FILE first"
    fi
    echo "  Remote InfluxDB details:"
    read -rp "  Host or IP (no scheme): " HOSTNAME_IN
    [[ -n "$HOSTNAME_IN" ]] || die "host is required"
    check_resolves "$HOSTNAME_IN" "InfluxDB host" \
      || warn "  Continuing anyway; the probe below may fail."
    read -rp "  Port [8086]: " PORT_IN; PORT_IN="${PORT_IN:-8086}"
    read -rp "  Org [llm-systems-manager]: " ORG; ORG="${ORG:-llm-systems-manager}"
    URL="http://${HOSTNAME_IN}:${PORT_IN}"
    log "probing $URL/health…"
    if [[ "$(probe_url "$URL/health")" == "200" ]]; then
      ok "$URL/health responded 200"
    else
      warn "$URL/health did not respond — continuing anyway (you can fix the URL in llm-systems.toml later)"
    fi
    echo "  Tokens (paste from the InfluxDB host):"
    read -rp "  Operator (admin) token: "      OP_TOKEN
    read -rp "  Metrics bucket scoped token: " METRICS_TOKEN
    read -rp "  Metrics-rollup bucket token: " ROLLUP_TOKEN
    [[ -n "$METRICS_TOKEN" ]] || die "metrics token is required"
    [[ -z "$OP_TOKEN" ]]     || validate_influx_token "operator token"      "$OP_TOKEN"     0 || die "operator token failed sanity check"
    validate_influx_token "metrics token"        "$METRICS_TOKEN" 1 || die "metrics token failed sanity check"
    [[ -z "$ROLLUP_TOKEN" ]] || validate_influx_token "metrics-rollup token" "$ROLLUP_TOKEN" 1 || die "metrics-rollup token failed sanity check"
    _write_env "$URL" "$ORG" "$OP_TOKEN" "$METRICS_TOKEN" "$ROLLUP_TOKEN"
    ;;

  skip)
    warn "skipping InfluxDB resolution — [influxdb.tokens] in llm-systems.toml will keep REPLACE_ME entries"
    warn "  the alarm engine will fail every read/write until you populate $ENV_FILE or edit the toml by hand"
    ;;
esac
