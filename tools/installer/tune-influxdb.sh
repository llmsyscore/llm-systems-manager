#!/usr/bin/env bash
# =============================================================================
# tools/installer/tune-influxdb.sh — refreshes the InfluxDB host tuning on a
# host that runs influxdb.service: the OOM drop-in, the GOMEMLIMIT block
# (shared hosts only) and the managed WAL fsync delay.
#
#   sudo bash tune-influxdb.sh [--restart | --no-restart] [--dry-run]
#
# Default: asks before restarting InfluxDB on a terminal, prints the restart
# command otherwise. update.sh runs it on InfluxDB-only hosts; the deb/rpm
# postinst runs it on upgrade when influxdb.service is local.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

detect_os
require_linux
detect_sudo

RESTART=ask
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart)    RESTART=yes; shift ;;
    --no-restart) RESTART=no; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    sed -n '3,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown flag: $1 (see --help)" ;;
  esac
done

_units="$(systemctl list-unit-files --no-legend --type=service 2>/dev/null | awk '{print $1}' || true)"
if ! grep -qx 'influxdb.service' <<<"$_units"; then
  log "no local influxdb.service — nothing to tune"
  exit 0
fi
# Shared host = any manager, alarm engine or agent unit next to InfluxDB.
_colocated=0
grep -qE '^llm-systems-(manager|alarm-engine|agent)\.service$' <<<"$_units" && _colocated=1

if (( DRY_RUN )); then
  log "[dry-run] would refresh the influxdb OOM drop-in, GOMEMLIMIT block (colocated=$_colocated) and WAL fsync delay"
  exit 0
fi

apply_influxdb_host_tuning "$_colocated"
apply_influxdb_wal_fsync_delay
if (( ! LLMSYS_INFLUX_TUNING_CHANGED )); then
  ok "InfluxDB host tuning already current (GOMEMLIMIT $LLMSYS_INFLUX_GOMEMLIMIT)"
  exit 0
fi

if [[ "$RESTART" == "ask" ]]; then
  RESTART=no
  if [[ -t 0 ]]; then
    read -rp "  Restart influxdb now to apply? [Y/n] " _ans || _ans=""
    _ans="$(printf '%s' "${_ans:-y}" | tr '[:upper:]' '[:lower:]')"
    [[ "$_ans" == "y" || "$_ans" == "yes" ]] && RESTART=yes
  fi
fi
if [[ "$RESTART" != "yes" ]]; then
  warn "InfluxDB host tuning changed; apply it with: sudo systemctl restart influxdb"
  exit 0
fi

log "restarting influxdb"
$SUDO systemctl restart influxdb
_wait="${LLMSYS_INFLUX_HEALTH_WAIT:-30}"
for (( _i = 0; _i < _wait; _i++ )); do
  [[ "$(probe_url http://127.0.0.1:8086/health)" == "200" ]] && break
  sleep 1
done
if [[ "$(probe_url http://127.0.0.1:8086/health)" == "200" ]]; then
  ok "influxdb restarted with the refreshed tuning"
else
  warn "influxdb did not answer on :8086 within ${_wait}s — check: sudo journalctl -u influxdb -n 60 --no-pager"
fi
