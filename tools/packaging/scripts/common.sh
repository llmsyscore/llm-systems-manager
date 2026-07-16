#!/bin/bash
# Shared functions for the .deb/.rpm maintainer scripts. build-packages.sh
# concatenates this file ahead of each per-format wrapper in scripts/deb|rpm/.
set -e

LLMSYS_INSTALL_DIR=/opt/llm-systems-manager
LLMSYS_RUN_USER=llmsys
LLMSYS_LOG_DIR=/var/log/llm-systems-manager
LLMSYS_UNITS="llm-systems-alarm-engine.service llm-systems-manager.service"
LLMSYS_CFG="$LLMSYS_INSTALL_DIR/config/llm-systems.toml"

# systemctl exists and PID 1 is systemd (false in containers/chroots).
llmsys_systemd_ready() {
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

# Prints the newest interpreter that is Python >= 3.10.
llmsys_pick_python() {
  local p
  for p in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$p" >/dev/null 2>&1 || continue
    if "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

# Group is created explicitly so it always matches the Group= baked into
# the packaged units regardless of the host's USERGROUPS_ENAB setting.
llmsys_create_user() {
  getent group "$LLMSYS_RUN_USER" >/dev/null 2>&1 \
    || groupadd --system "$LLMSYS_RUN_USER"
  getent passwd "$LLMSYS_RUN_USER" >/dev/null 2>&1 \
    || useradd --system --create-home --shell /bin/bash \
         -g "$LLMSYS_RUN_USER" "$LLMSYS_RUN_USER"
}

llmsys_run_group() {
  id -gn "$LLMSYS_RUN_USER"
}

# Creates the venv (bootstrapping pip via get-pip.py if ensurepip can't) and
# syncs it to the component's requirements file.
llmsys_build_one_venv() {
  local py="$1" dir="$2" req="$3" venv="$2/venv"
  if [ ! -x "$venv/bin/python3" ]; then
    runuser -u "$LLMSYS_RUN_USER" -- "$py" -m venv "$venv" 2>/dev/null \
      || runuser -u "$LLMSYS_RUN_USER" -- "$py" -m venv --without-pip "$venv"
  fi
  if [ ! -e "$venv/bin/pip" ] && [ ! -e "$venv/bin/pip3" ]; then
    runuser -u "$LLMSYS_RUN_USER" -- "$venv/bin/python3" -m ensurepip --upgrade 2>/dev/null \
      || curl -fsSL https://bootstrap.pypa.io/get-pip.py \
           | runuser -u "$LLMSYS_RUN_USER" -- "$venv/bin/python3" -
  fi
  runuser -u "$LLMSYS_RUN_USER" -- "$venv/bin/python3" -m pip install --quiet --upgrade pip
  runuser -u "$LLMSYS_RUN_USER" -- "$venv/bin/python3" -m pip install --quiet -r "$dir/$req"
}

llmsys_build_venvs() {
  local py
  if ! py="$(llmsys_pick_python)"; then
    echo "llm-systems-manager: no Python >= 3.10 found." >&2
    echo "Install one (e.g. 'dnf install python3.11 python3.11-pip'), then run 'dnf reinstall llm-systems-manager' (or 'dpkg --configure -a' on Debian)." >&2
    return 1
  fi
  llmsys_build_one_venv "$py" "$LLMSYS_INSTALL_DIR/llm-systems-manager" backend/requirements.txt
  llmsys_build_one_venv "$py" "$LLMSYS_INSTALL_DIR/llm-systems-alarm-engine" requirements.txt
}

# Merges new example keys into an existing live config via toml_reconcile.py.
llmsys_reconcile_config() {
  local py merged tmp grp
  py="$(llmsys_pick_python)" || return 0
  if ! merged="$("$py" "$LLMSYS_INSTALL_DIR/tools/installer/toml_reconcile.py" merge \
                 "$LLMSYS_CFG" "$LLMSYS_INSTALL_DIR/config/llm-systems.toml.example" 2>/dev/null)"; then
    echo "llm-systems-manager: config reconcile failed — existing config left untouched" >&2
    return 0
  fi
  tmp="$(mktemp "$LLMSYS_CFG.XXXXXX")"
  printf '%s\n' "$merged" > "$tmp"
  grp="$(llmsys_run_group)"
  chown "$LLMSYS_RUN_USER:$grp" "$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$LLMSYS_CFG"
}

# Refreshes the live unified_config.py schema from the packaged example
# (same force-sync update.sh does — a stale schema breaks the services).
llmsys_refresh_schema() {
  local src="$LLMSYS_INSTALL_DIR/config/unified_config.py.example"
  local dst="$LLMSYS_INSTALL_DIR/config/unified_config.py" grp
  [ -f "$src" ] || return 0
  cmp -s "$src" "$dst" 2>/dev/null && return 0
  grp="$(llmsys_run_group)"
  install -m 0644 -o "$LLMSYS_RUN_USER" -g "$grp" "$src" "$dst"
}

# Fresh install: generate config via the installer's bootstrap (non-TTY path;
# LLMSYS_CFG_* env overrides carry debconf answers). Upgrade: merge new keys.
llmsys_write_config() {
  if [ -f "$LLMSYS_CFG" ]; then
    llmsys_reconcile_config
    llmsys_refresh_schema
  else
    LLMSYS_INSTALL_MODE=2 bash "$LLMSYS_INSTALL_DIR/tools/installer/install-config-bootstrap.sh" </dev/null
  fi
}

llmsys_enable_start() {
  llmsys_systemd_ready || return 0
  systemctl daemon-reload
  # shellcheck disable=SC2086
  systemctl enable $LLMSYS_UNITS >/dev/null 2>&1 || true
  # shellcheck disable=SC2086
  systemctl start $LLMSYS_UNITS \
    || echo "llm-systems-manager: services failed to start — check 'journalctl -u llm-systems-manager'" >&2
}

llmsys_restart_upgraded() {
  llmsys_systemd_ready || return 0
  systemctl daemon-reload
  # shellcheck disable=SC2086
  systemctl try-restart $LLMSYS_UNITS || true
}

llmsys_stop_disable() {
  llmsys_systemd_ready || return 0
  # shellcheck disable=SC2086
  systemctl disable --now $LLMSYS_UNITS >/dev/null 2>&1 || true
}

# Full configure pass. $1 empty = fresh install, non-empty = upgrade.
llmsys_configure() {
  local upgrade="${1:-}" grp
  llmsys_create_user
  grp="$(llmsys_run_group)"
  install -d -m 0750 -o "$LLMSYS_RUN_USER" -g "$grp" "$LLMSYS_INSTALL_DIR/data"
  install -d -m 0755 -o "$LLMSYS_RUN_USER" -g "$grp" "$LLMSYS_LOG_DIR"
  chown -R "$LLMSYS_RUN_USER:$grp" "$LLMSYS_INSTALL_DIR"
  llmsys_build_venvs
  llmsys_write_config
  if [ -z "$upgrade" ]; then
    llmsys_enable_start
    echo "llm-systems-manager: dashboard on port 5000; config: $LLMSYS_CFG (edit + 'systemctl restart llm-systems-manager')"
  else
    llmsys_restart_upgraded
  fi
}

# Drops regenerable artifacts (venvs, bytecode caches).
llmsys_remove_generated() {
  rm -rf "$LLMSYS_INSTALL_DIR/llm-systems-manager/venv" \
         "$LLMSYS_INSTALL_DIR/llm-systems-alarm-engine/venv"
  [ -d "$LLMSYS_INSTALL_DIR" ] \
    && find "$LLMSYS_INSTALL_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
}

# Removes config, data, logs, and the runtime user (deb purge only).
llmsys_purge_all() {
  rm -rf "$LLMSYS_INSTALL_DIR" "$LLMSYS_LOG_DIR"
  if getent passwd "$LLMSYS_RUN_USER" >/dev/null 2>&1; then
    userdel -r "$LLMSYS_RUN_USER" 2>/dev/null || userdel "$LLMSYS_RUN_USER" 2>/dev/null || true
  fi
}
