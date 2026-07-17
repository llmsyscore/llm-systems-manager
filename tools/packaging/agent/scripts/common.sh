#!/bin/bash
# Shared functions for the llm-systems-agent .deb/.rpm maintainer scripts.
# build-agent-package.sh concatenates this ahead of scripts/deb|rpm/ bodies.
set -e

LLMSYS_AGENT_DIR=/opt/llm-systems-agent
LLMSYS_RUN_USER=llmsys
LLMSYS_AGENT_UNIT=llm-systems-agent.service
LLMSYS_AGENT_CFG="$LLMSYS_AGENT_DIR/agent_config.yaml"
LLMSYS_AGENT_MARKER="$LLMSYS_AGENT_DIR/.llmsys-package-state"
LLMSYS_AGENT_PREV_VER="$LLMSYS_AGENT_DIR/.llmsys-prev-version"

# Script-installer (venv) state this package must not silently shadow (#416).
# A bare tarball install (binary + config, no unit/venv) is adoptable instead.
llmsys_agent_foreign_state() {
  local hits=""
  [ -e "/etc/systemd/system/$LLMSYS_AGENT_UNIT" ] && hits="$hits /etc/systemd/system/$LLMSYS_AGENT_UNIT"
  [ -d "$LLMSYS_AGENT_DIR/venv" ] && hits="$hits $LLMSYS_AGENT_DIR/venv"
  [ -f "$LLMSYS_AGENT_DIR/llm-systems-agent.py" ] && hits="$hits $LLMSYS_AGENT_DIR/llm-systems-agent.py"
  [ -n "$hits" ] || return 1
  echo "$hits"
}

llmsys_agent_ports_free() {
  command -v ss >/dev/null 2>&1 || return 0
  if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE '[:.]8082$'; then
    echo "llm-systems-agent: port 8082 already in use — another agent install is likely running; installing would crash-loop the service (#416)." >&2
    return 1
  fi
}

# Fresh-install gate (deb preinst "install" / rpm %pre $1==1).
llmsys_agent_guard_fresh_install() {
  [ "${LLMSYS_PACKAGE_FORCE:-0}" = "1" ] && return 0
  local hits
  if hits="$(llmsys_agent_foreign_state)"; then
    echo "llm-systems-agent: refusing to install over an existing script (venv) install (#416):" >&2
    echo "  found:$hits" >&2
    echo "  Remove it first (agent/install/install.sh --uninstall) or, to force over it," >&2
    echo "  re-run with LLMSYS_PACKAGE_FORCE=1 in the environment." >&2
    return 1
  fi
  llmsys_agent_ports_free || return 1
  if [ -x "$LLMSYS_AGENT_DIR/llm-systems-agent" ] && [ ! -f "$LLMSYS_AGENT_MARKER" ]; then
    echo "llm-systems-agent: adopting an existing binary-tarball install — config/token are preserved; the binary is replaced by the packaged one."
  fi
}

# Stashes the current binary's version (preinst, before unpack) so
# postinst can flag a replace-with-older.
llmsys_agent_stash_prev_version() {
  rm -f "$LLMSYS_AGENT_PREV_VER"
  [ -x "$LLMSYS_AGENT_DIR/llm-systems-agent" ] || return 0
  "$LLMSYS_AGENT_DIR/llm-systems-agent" --version 2>/dev/null | tail -1 \
    > "$LLMSYS_AGENT_PREV_VER" 2>/dev/null || rm -f "$LLMSYS_AGENT_PREV_VER"
}

# Records that this tree is package-created. adopted=1 = pre-existing
# state was present — purge then keeps config/data.
llmsys_agent_write_marker() {
  [ -f "$LLMSYS_AGENT_MARKER" ] && return 0
  local adopted=0
  [ "$1" = "adopted" ] && adopted=1
  printf 'format=1\nadopted=%s\n' "$adopted" > "$LLMSYS_AGENT_MARKER"
  chmod 0644 "$LLMSYS_AGENT_MARKER"
}

llmsys_systemd_ready() {
  command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

# Group is created explicitly so it always matches the Group= baked into
# the packaged unit regardless of the host's USERGROUPS_ENAB setting.
llmsys_create_user() {
  getent group "$LLMSYS_RUN_USER" >/dev/null 2>&1 \
    || groupadd --system "$LLMSYS_RUN_USER"
  getent passwd "$LLMSYS_RUN_USER" >/dev/null 2>&1 \
    || useradd --system --create-home --shell /bin/bash \
         -g "$LLMSYS_RUN_USER" "$LLMSYS_RUN_USER"
}

# Generates agent_config.yaml from the packaged example, setting the
# identity keys + manager/AE URLs (LLMSYS_CFG_* carry debconf answers).
llmsys_agent_write_config() {
  [ -f "$LLMSYS_AGENT_CFG" ] && return 0
  local mgr="${LLMSYS_CFG_MANAGER_URL:-}" ae="${LLMSYS_CFG_ALARM_ENGINE_URL:-}" host line
  if [ -n "$mgr" ] && [ -z "$ae" ]; then
    host="${mgr#*://}"; host="${host%%/*}"; host="${host%%:*}"
    ae="http://${host}:8081"
  fi
  while IFS= read -r line; do
    case "$line" in
      "AGENT_USER:"*)        printf 'AGENT_USER:        "%s"\n' "$LLMSYS_RUN_USER" ;;
      "AGENT_INSTALL_DIR:"*) printf 'AGENT_INSTALL_DIR: "%s"\n' "$LLMSYS_AGENT_DIR" ;;
      "MANAGER_URL:"*)       printf 'MANAGER_URL:      "%s"\n' "$mgr" ;;
      "ALARM_ENGINE_URL:"*)  printf 'ALARM_ENGINE_URL: "%s"\n' "$ae" ;;
      *)                     printf '%s\n' "$line" ;;
    esac
  done < "$LLMSYS_AGENT_DIR/agent_config.yaml.example" > "$LLMSYS_AGENT_CFG"
  chown "$LLMSYS_RUN_USER:$(id -gn "$LLMSYS_RUN_USER")" "$LLMSYS_AGENT_CFG"
  chmod 0640 "$LLMSYS_AGENT_CFG"
}

# Warns when a package upgrade replaced a self-updated binary with an
# older one (versions are vYYYY.MM.DD-N — sort -V comparable). #416.
llmsys_agent_downgrade_check() {
  local old new
  [ -f "$LLMSYS_AGENT_PREV_VER" ] || return 0
  old="$(cat "$LLMSYS_AGENT_PREV_VER" 2>/dev/null)"
  rm -f "$LLMSYS_AGENT_PREV_VER"
  new="$("$LLMSYS_AGENT_DIR/llm-systems-agent" --version 2>/dev/null | tail -1)" || return 0
  [ -n "$old" ] && [ -n "$new" ] && [ "$old" != "$new" ] || return 0
  if [ "$(printf '%s\n%s\n' "$old" "$new" | sort -V | tail -1)" = "$old" ]; then
    echo "llm-systems-agent: WARNING — this package replaced a self-updated binary $old with older $new. Re-update from Admin -> Agents -> Update, or install a newer package (#416)." >&2
  fi
}

# Full configure pass. $1 empty = fresh install, non-empty = upgrade.
# The whole tree (binary included) is owned by the agent user so
# manager-driven frozen self-update keeps working.
llmsys_agent_configure() {
  local upgrade="${1:-}" grp adopt=clean
  # Marker backfill: on fresh, a pre-existing config means adoption; on
  # upgrade (pre-#416 packages have no marker) only script-install state does.
  if [ ! -f "$LLMSYS_AGENT_MARKER" ]; then
    if [ -z "$upgrade" ]; then
      [ -f "$LLMSYS_AGENT_CFG" ] && adopt=adopted
    elif llmsys_agent_foreign_state >/dev/null; then
      adopt=adopted
    fi
  fi
  llmsys_create_user
  grp="$(id -gn "$LLMSYS_RUN_USER")"
  install -d -m 0750 -o "$LLMSYS_RUN_USER" -g "$grp" "$LLMSYS_AGENT_DIR/data"
  chown -R "$LLMSYS_RUN_USER:$grp" "$LLMSYS_AGENT_DIR"
  llmsys_agent_write_config
  llmsys_agent_write_marker "$adopt"
  llmsys_agent_downgrade_check
  if [ -e "/etc/systemd/system/$LLMSYS_AGENT_UNIT" ]; then
    echo "llm-systems-agent: WARNING — /etc/systemd/system/$LLMSYS_AGENT_UNIT exists and shadows the packaged unit; package upgrades will not affect the running service until it is removed (#416)." >&2
  fi
  llmsys_systemd_ready || return 0
  systemctl daemon-reload
  if [ -z "$upgrade" ]; then
    systemctl enable "$LLMSYS_AGENT_UNIT" >/dev/null 2>&1 || true
    systemctl start "$LLMSYS_AGENT_UNIT" \
      || echo "llm-systems-agent: failed to start — check 'journalctl -u llm-systems-agent'" >&2
    if grep -q '^MANAGER_URL: *""' "$LLMSYS_AGENT_CFG" 2>/dev/null; then
      echo "llm-systems-agent: NOTICE — MANAGER_URL is not set. Edit $LLMSYS_AGENT_CFG, then 'systemctl restart llm-systems-agent'." >&2
    fi
  else
    systemctl try-restart "$LLMSYS_AGENT_UNIT" || true
  fi
}

llmsys_agent_stop_disable() {
  llmsys_systemd_ready || return 0
  systemctl disable --now "$LLMSYS_AGENT_UNIT" >/dev/null 2>&1 || true
}

# Drops self-update leftovers (regenerable backups of replaced binaries).
llmsys_agent_remove_generated() {
  rm -f "$LLMSYS_AGENT_DIR"/.self-update.bak.* 2>/dev/null || true
}

# Removes config, token/data, and (unless the manager package's tree is
# still present — shared user) the runtime user. deb purge only.
llmsys_agent_purge_all() {
  rm -rf "$LLMSYS_AGENT_DIR"
  [ -d /opt/llm-systems-manager ] && return 0
  if getent passwd "$LLMSYS_RUN_USER" >/dev/null 2>&1; then
    userdel -r "$LLMSYS_RUN_USER" 2>/dev/null || userdel "$LLMSYS_RUN_USER" 2>/dev/null || true
  fi
}

# Purge only when this package created the tree and no script-installer
# state appeared since; otherwise keep config/data (#416).
llmsys_agent_scoped_purge() {
  if [ -f "$LLMSYS_AGENT_MARKER" ] \
     && ! grep -q '^adopted=1' "$LLMSYS_AGENT_MARKER" 2>/dev/null \
     && ! llmsys_agent_foreign_state >/dev/null; then
    llmsys_agent_purge_all
    return 0
  fi
  rm -f "$LLMSYS_AGENT_MARKER" "$LLMSYS_AGENT_PREV_VER"
  echo "llm-systems-agent: purge kept $LLMSYS_AGENT_CFG and $LLMSYS_AGENT_DIR/data — the tree contains state this package did not create (#416). Remove manually if intended."
}
