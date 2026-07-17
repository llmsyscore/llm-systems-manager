#!/bin/bash
# Shared functions for the llm-systems-agent .deb/.rpm maintainer scripts.
# build-agent-package.sh concatenates this ahead of scripts/deb|rpm/ bodies.
set -e

LLMSYS_AGENT_DIR=/opt/llm-systems-agent
LLMSYS_RUN_USER=llmsys
LLMSYS_AGENT_UNIT=llm-systems-agent.service
LLMSYS_AGENT_CFG="$LLMSYS_AGENT_DIR/agent_config.yaml"

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

# Full configure pass. $1 empty = fresh install, non-empty = upgrade.
# The whole tree (binary included) is owned by the agent user so
# manager-driven frozen self-update keeps working.
llmsys_agent_configure() {
  local upgrade="${1:-}" grp
  llmsys_create_user
  grp="$(id -gn "$LLMSYS_RUN_USER")"
  install -d -m 0750 -o "$LLMSYS_RUN_USER" -g "$grp" "$LLMSYS_AGENT_DIR/data"
  chown -R "$LLMSYS_RUN_USER:$grp" "$LLMSYS_AGENT_DIR"
  llmsys_agent_write_config
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
