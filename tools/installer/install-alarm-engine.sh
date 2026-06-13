#!/usr/bin/env bash
# =============================================================================
# tools/installer/install-alarm-engine.sh — installs the FastAPI alarm engine
#
# Assumes the repo is deployed at $LLMSYS_INSTALL_DIR and llmsys user exists.
#
# Does:
#   - Installs apt prereqs (python3, venv, pip, build essentials).
#   - Creates llm-systems-alarm-engine/venv as llmsys.
#   - pip-installs llm-systems-alarm-engine/requirements.txt.
#   - Drops the systemd unit and enables it. Does NOT start it.
#
# Does NOT:
#   - Provision InfluxDB or write the config (those are separate steps).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$HERE/lib-common.sh"

# Standalone --user override (install.sh exports LLMSYS_RUN_USER for the
# in-process flow; the flag is here so direct invocations work too).
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) [[ -n "${2:-}" ]] || die "--user requires a value"; LLMSYS_RUN_USER="$2"; LLMSYS_RUN_GROUP="$2"; shift 2 ;;
    --user=*) LLMSYS_RUN_USER="${1#*=}"; [[ -n "$LLMSYS_RUN_USER" ]] || die "--user requires a value"; LLMSYS_RUN_GROUP="$LLMSYS_RUN_USER"; shift ;;
    *) die "install-alarm-engine.sh: unexpected argument '$1' (only --user USER is accepted)" ;;
  esac
done
export LLMSYS_RUN_USER LLMSYS_RUN_GROUP

detect_os
require_linux
detect_sudo

INSTALL_DIR="${LLMSYS_INSTALL_DIR}"
# Create the user (if missing) BEFORE resolving its real primary group.
ensure_runas_user "$LLMSYS_RUN_USER"
resolve_run_group "$LLMSYS_RUN_USER"
AE_DIR="$INSTALL_DIR/llm-systems-alarm-engine"

banner "Alarm engine — apt prereqs"
ensure_apt_prereqs python3 python3-venv python3-pip python3-dev build-essential ca-certificates sqlite3
ok "apt prereqs present"

banner "Alarm engine — venv + pip install"
if [[ ! -d "$AE_DIR" ]]; then
  die "$AE_DIR does not exist — deploy the repo first."
fi
if [[ ! -f "$AE_DIR/requirements.txt" ]]; then
  die "$AE_DIR/requirements.txt missing — bad deploy?"
fi

if [[ ! -x "$AE_DIR/venv/bin/python3" ]]; then
  log "creating venv at $AE_DIR/venv"
  as_run_user python3 -m venv "$AE_DIR/venv"
else
  log "venv already present"
fi
if ! pip_filter as_run_user "$AE_DIR/venv/bin/pip" install --quiet --no-cache-dir --upgrade pip; then
  die "pip self-upgrade failed in $AE_DIR/venv — see output above"
fi
if ! pip_filter as_run_user "$AE_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$AE_DIR/requirements.txt"; then
  die "pip install -r failed — $AE_DIR/venv is half-built; the alarm engine will fail to start"
fi
ok "alarm engine venv ready"

banner "Alarm engine — log dir"
# Shared with the manager (both write into [paths].log_dir). install-manager.sh
# also creates this — install order varies, so each script ensures it exists.
ensure_log_dir /var/log/llm-systems-manager
ok "log dir /var/log/llm-systems-manager ready"

banner "Alarm engine — systemd unit"
UNIT_TPL="$AE_DIR/systemd/llm-systems-alarm-engine.service.example"
UNIT_DST=/etc/systemd/system/llm-systems-alarm-engine.service
if [[ ! -f "$UNIT_TPL" ]]; then
  die "systemd unit template missing: $UNIT_TPL"
fi
install_unit_template "$UNIT_TPL" "$UNIT_DST"
enable_unit llm-systems-alarm-engine.service
ok "unit installed + enabled"
log "AE serves HTTPS automatically when [alarm_engine].tls_enabled = true and"
log "  data/ae-tls.{crt,key} are present. Co-located installs auto-receive the"
log "  cert from the manager; split installs copy it from the manager host's"
log "  data/ dir into this host's llm-systems-alarm-engine/data/ dir."
ok "Alarm engine installed"
