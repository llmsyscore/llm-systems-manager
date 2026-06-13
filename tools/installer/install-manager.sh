#!/usr/bin/env bash
# =============================================================================
# tools/installer/install-manager.sh — installs the Flask manager
#
# Assumes:
#   - The repo has already been deployed to /opt/llm-systems-manager
#     (or wherever LLMSYS_INSTALL_DIR points).
#   - The 'llmsys' user/group exists and owns the install dir.
#   - lib-common.sh is sourceable from $(dirname "$0")/lib-common.sh.
#
# Does:
#   - Installs apt prereqs (python3, venv, pip, dev, build essentials,
#     openssh-client) when missing.
#   - Creates /opt/llm-systems-manager/venv as the llmsys user.
#   - pip-installs llm-systems-manager/backend/requirements.txt.
#   - Drops systemd/llm-systems-manager.service into /etc/systemd/system/
#     and runs daemon-reload + enable.
#   - Ensures /var/log/llm-systems-manager exists, owned by llmsys.
#
# Does NOT:
#   - Restart the service (per Operating Rules — operator decides).
#   - Touch config/llm-systems.toml (handled by install-config-bootstrap.sh).
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
    *) die "install-manager.sh: unexpected argument '$1' (only --user USER is accepted)" ;;
  esac
done
export LLMSYS_RUN_USER LLMSYS_RUN_GROUP

detect_os
require_linux
detect_sudo

INSTALL_DIR="${LLMSYS_INSTALL_DIR}"
# Standalone runs: create the user if it doesn't exist (install.sh's
# ensure_runas_user is only called in the multi-mode flow). Resolve the real
# primary group AFTER creation (useradd's group choice varies by USERGROUPS_ENAB).
ensure_runas_user "$LLMSYS_RUN_USER"
resolve_run_group "$LLMSYS_RUN_USER"

banner "Manager — apt prereqs"
ensure_apt_prereqs python3 python3-venv python3-pip python3-dev build-essential \
  ca-certificates curl wget jq sqlite3
ok "apt prereqs present"

banner "Manager — venv + pip install"
if [[ ! -d "$INSTALL_DIR" ]]; then
  die "$INSTALL_DIR does not exist — deploy the repo first."
fi
MGR_DIR="$INSTALL_DIR/llm-systems-manager"
if [[ ! -f "$MGR_DIR/backend/requirements.txt" ]]; then
  die "$MGR_DIR/backend/requirements.txt missing — bad deploy?"
fi

if [[ ! -x "$MGR_DIR/venv/bin/python3" ]]; then
  log "creating venv at $MGR_DIR/venv"
  as_run_user python3 -m venv "$MGR_DIR/venv"
else
  log "venv already present"
fi
if ! pip_filter as_run_user "$MGR_DIR/venv/bin/pip" install --quiet --no-cache-dir --upgrade pip; then
  die "pip self-upgrade failed in $MGR_DIR/venv — see output above"
fi
if ! pip_filter as_run_user "$MGR_DIR/venv/bin/pip" install --quiet --no-cache-dir \
       -r "$MGR_DIR/backend/requirements.txt"; then
  die "pip install -r failed — $MGR_DIR/venv is half-built; the manager will fail to start"
fi
ok "manager venv ready"

banner "Manager — log dir"
ensure_log_dir /var/log/llm-systems-manager
ok "log dir /var/log/llm-systems-manager ready"

banner "Manager — systemd unit"
UNIT_TPL="$INSTALL_DIR/systemd/llm-systems-manager.service.example"
UNIT_DST=/etc/systemd/system/llm-systems-manager.service
if [[ ! -f "$UNIT_TPL" ]]; then
  die "systemd unit template missing: $UNIT_TPL"
fi
# Materialise the unit by substituting @@PLACEHOLDERS@@ in the .example.
install_unit_template "$UNIT_TPL" "$UNIT_DST"
enable_unit llm-systems-manager.service
ok "unit installed + enabled"

banner "Manager — sudoers (admin-tab service restart)"
# Lets the runtime user restart the manager + alarm-engine units from the
# admin tab's System Health card. Scoped to exactly those two commands.
install_sudoers_fragment "$INSTALL_DIR/systemd/llm-systems-manager.sudoers.tmpl" \
  /etc/sudoers.d/llm-systems-manager || true
ok "Manager installed"
