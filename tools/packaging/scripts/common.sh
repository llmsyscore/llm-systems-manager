#!/bin/bash
# Shared functions for the .deb/.rpm maintainer scripts. build-packages.sh
# concatenates this file ahead of each per-format wrapper in scripts/deb|rpm/.
set -e

LLMSYS_INSTALL_DIR=/opt/llm-systems-manager
LLMSYS_RUN_USER=llmsys
LLMSYS_LOG_DIR=/var/log/llm-systems-manager
LLMSYS_UNITS="llm-systems-alarm-engine.service llm-systems-manager.service"
LLMSYS_CFG="$LLMSYS_INSTALL_DIR/config/llm-systems.toml"
LLMSYS_PKG_MARKER="$LLMSYS_INSTALL_DIR/.llmsys-package-state"

# Script-installer state this package must not silently adopt (#416).
# Prints what was found; rc 0 = foreign state present.
llmsys_foreign_state() {
  local hits="" u
  for u in $LLMSYS_UNITS; do
    [ -e "/etc/systemd/system/$u" ] && hits="$hits /etc/systemd/system/$u"
  done
  if [ -f "$LLMSYS_CFG" ] && [ ! -f "$LLMSYS_PKG_MARKER" ]; then
    hits="$hits $LLMSYS_CFG(not-package-created)"
  fi
  [ -n "$hits" ] || return 1
  echo "$hits"
}

# rc 1 when something else already listens on one of the given ports
# (script install or docker control plane on the same host).
llmsys_ports_free() {
  command -v ss >/dev/null 2>&1 || return 0
  local p busy=""
  for p in "$@"; do
    ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$" && busy="$busy $p"
  done
  [ -z "$busy" ] && return 0
  echo "llm-systems-manager: port(s)$busy already in use on this host — a script-installed" >&2
  echo "or docker control plane may be running. Installing would crash-loop the services (#416)." >&2
  return 1
}

# Fresh-install gate (deb preinst "install" / rpm %pre $1==1).
llmsys_guard_fresh_install() {
  [ "${LLMSYS_PACKAGE_FORCE:-0}" = "1" ] && return 0
  local hits
  if hits="$(llmsys_foreign_state)"; then
    echo "llm-systems-manager: refusing to install over an existing non-package install (#416):" >&2
    echo "  found:$hits" >&2
    echo "  Remove it first (tools/installer/uninstall.sh) or, to force over it," >&2
    echo "  re-run with LLMSYS_PACKAGE_FORCE=1 in the environment." >&2
    return 1
  fi
  llmsys_ports_free 5000 8081 || return 1
}

# Records that this tree is package-created. adopted=1 = installed over
# pre-existing state (LLMSYS_PACKAGE_FORCE) — purge then keeps config/data.
llmsys_write_marker() {
  [ -f "$LLMSYS_PKG_MARKER" ] && return 0
  local adopted=0
  [ "$1" = "adopted" ] && adopted=1
  printf 'format=1\nadopted=%s\n' "$adopted" > "$LLMSYS_PKG_MARKER"
  chmod 0644 "$LLMSYS_PKG_MARKER"
}

llmsys_warn_if_shadowed() {
  local u
  for u in $LLMSYS_UNITS; do
    if [ -e "/etc/systemd/system/$u" ]; then
      echo "llm-systems-manager: WARNING — /etc/systemd/system/$u exists and shadows the packaged unit; package upgrades will not affect the running service until it is removed (#416)." >&2
    fi
  done
}

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

# Best-effort probe of the configured InfluxDB; loud notice when absent
# (influxdb2 is only a weak dependency — third-party repo, may be remote).
llmsys_influx_notice() {
  local host port
  host="$(awk -F'"' '/^\[influxdb\]/{s=1;next} /^\[/{s=0} s && /^host[ \t]*=/{print $2; exit}' "$LLMSYS_CFG" 2>/dev/null)"
  port="$(awk '/^\[influxdb\]/{s=1;next} /^\[/{s=0} s && /^port[ \t]*=/{gsub(/[^0-9]/,""); print; exit}' "$LLMSYS_CFG" 2>/dev/null)"
  curl -fsS -m 3 "http://${host:-localhost}:${port:-8086}/health" >/dev/null 2>&1 && return 0
  echo "llm-systems-manager: NOTICE — InfluxDB is not reachable at ${host:-localhost}:${port:-8086}." >&2
  echo "  Metric history/alarms need it: install locally (tools/installer/install-influxdb.sh or the" >&2
  echo "  influxdb2 package from repos.influxdata.com) or point [influxdb] + [influxdb.tokens] in" >&2
  echo "  $LLMSYS_CFG at an existing server, then restart the services." >&2
}

# rc 0 when [influxdb.tokens] still carries REPLACE_ME placeholders.
llmsys_influx_tokens_unset() {
  awk '/^\[influxdb\.tokens\]/{s=1;next} /^\[/{s=0} s && /REPLACE_ME/{f=1} END{exit f?0:1}' \
    "$LLMSYS_CFG" 2>/dev/null
}

# Prominent end-of-install block for the not-started alarm engine.
llmsys_ae_gated_notice() {
  cat >&2 <<EOF

==============================================================================
 ACTION REQUIRED — the alarm engine was NOT started

 [influxdb.tokens] in $LLMSYS_CFG
 still holds REPLACE_ME placeholders; the alarm engine needs real tokens.

   1. Install InfluxDB locally:
        sudo bash $LLMSYS_INSTALL_DIR/tools/installer/install-influxdb.sh
      (it prints the tokens to paste), or point [influxdb] at an existing
      server and create tokens there.
   2. Set [influxdb] host/port + [influxdb.tokens] in the config above.
   3. Run: systemctl start llm-systems-alarm-engine
==============================================================================

EOF
}

# Enables both units; starts the alarm engine only when the InfluxDB
# tokens are real (sets LLMSYS_AE_GATED=1 otherwise).
llmsys_enable_start() {
  llmsys_systemd_ready || return 0
  systemctl daemon-reload
  # shellcheck disable=SC2086
  systemctl enable $LLMSYS_UNITS >/dev/null 2>&1 || true
  local units="$LLMSYS_UNITS"
  if llmsys_influx_tokens_unset; then
    units="llm-systems-manager.service"
    LLMSYS_AE_GATED=1
  fi
  # shellcheck disable=SC2086
  systemctl start $units \
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
# Marker backfill: on fresh, a pre-existing config means adoption; on
# upgrade (pre-#416 packages have no marker) only script units do.
llmsys_configure() {
  local upgrade="${1:-}" grp adopt=clean u
  if [ ! -f "$LLMSYS_PKG_MARKER" ]; then
    if [ -z "$upgrade" ]; then
      [ -f "$LLMSYS_CFG" ] && adopt=adopted
    else
      for u in $LLMSYS_UNITS; do
        [ -e "/etc/systemd/system/$u" ] && adopt=adopted
      done
    fi
  fi
  llmsys_create_user
  grp="$(llmsys_run_group)"
  install -d -m 0750 -o "$LLMSYS_RUN_USER" -g "$grp" "$LLMSYS_INSTALL_DIR/data"
  install -d -m 0755 -o "$LLMSYS_RUN_USER" -g "$grp" "$LLMSYS_LOG_DIR"
  chown -R "$LLMSYS_RUN_USER:$grp" "$LLMSYS_INSTALL_DIR"
  llmsys_build_venvs
  llmsys_write_config
  llmsys_write_marker "$adopt"
  llmsys_warn_if_shadowed
  if [ -z "$upgrade" ]; then
    llmsys_enable_start
    llmsys_influx_notice || true
    echo "llm-systems-manager: dashboard on port 5000; config: $LLMSYS_CFG (edit + 'systemctl restart llm-systems-manager')"
    if [ "${LLMSYS_AE_GATED:-0}" = "1" ]; then llmsys_ae_gated_notice; fi
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
# The user is shared with the llm-systems-agent package — keep it while
# that package's tree is still present.
llmsys_purge_all() {
  rm -rf "$LLMSYS_INSTALL_DIR" "$LLMSYS_LOG_DIR"
  [ -d /opt/llm-systems-agent ] && return 0
  if getent passwd "$LLMSYS_RUN_USER" >/dev/null 2>&1; then
    userdel -r "$LLMSYS_RUN_USER" 2>/dev/null || userdel "$LLMSYS_RUN_USER" 2>/dev/null || true
  fi
}

# Purge only when this package created the tree and no script-installer
# state appeared since; otherwise keep config/data/logs (#416).
llmsys_scoped_purge() {
  if [ -f "$LLMSYS_PKG_MARKER" ] \
     && ! grep -q '^adopted=1' "$LLMSYS_PKG_MARKER" 2>/dev/null \
     && ! llmsys_foreign_state >/dev/null; then
    llmsys_purge_all
    return 0
  fi
  rm -f "$LLMSYS_PKG_MARKER"
  echo "llm-systems-manager: purge kept $LLMSYS_CFG, $LLMSYS_INSTALL_DIR/data and $LLMSYS_LOG_DIR — the tree contains state this package did not create (#416). Remove manually if intended."
}
