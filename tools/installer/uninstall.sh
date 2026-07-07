#!/usr/bin/env bash
# =============================================================================
# tools/installer/uninstall.sh — self-contained, auto-detecting uninstaller.
#
# Safe to drop onto any host (manager, agent, mixed; Linux or macOS) and run
# standalone with no repo checkout. Scans systemd, launchd, and the file
# system for traces left by the installer, presents what it found, and
# removes only what the operator confirms.
#
# Invocation:
#   sudo bash uninstall.sh
#   (or via the bootstrap: sudo bash install.sh --uninstall)
# =============================================================================
set -euo pipefail

# When fetched into /tmp by the bootstrap, self-delete on exit so the
# host is left exactly as the operator confirmed — no stray helper file.
# A repo-resident copy (e.g. /opt/.../tools/installer/uninstall.sh) is
# preserved.
_SELF="${BASH_SOURCE[0]:-$0}"
case "$_SELF" in
  /tmp/llm-systems-uninstall.*.sh) trap 'rm -f "$_SELF"' EXIT ;;
esac

# ── Colored markers (no lib-common dependency — this script stands alone) ──
if [[ -t 1 ]]; then
  _GRN=$'\033[32m'; _YLW=$'\033[33m'; _RED=$'\033[31m'; _BLU=$'\033[34m'; _RST=$'\033[0m'
else
  _GRN=""; _YLW=""; _RED=""; _BLU=""; _RST=""
fi
ok()     { printf '%s[ OK ]%s  %s\n' "$_GRN" "$_RST" "$*"; }
log()    { printf '%s[INFO]%s  %s\n' "$_BLU" "$_RST" "$*"; }
warn()   { printf '%s[WARN]%s  %s\n' "$_YLW" "$_RST" "$*" >&2; }
err()    { printf '%s[ERR ]%s  %s\n' "$_RED" "$_RST" "$*" >&2; }
die()    { err "$*"; exit 1; }
banner() { printf '\n%s── %s ───────────────────────────────────────────────%s\n' "$_BLU" "$1" "$_RST"; }

confirm() {
  if [[ ! -t 0 ]]; then return 1; fi
  local prompt="$1" ans
  read -rp "  $prompt [y/N] " ans
  case "$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]')" in
    y|yes) return 0 ;;
    *)     return 1 ;;
  esac
}

# True only for system-range UIDs (never 0): Linux uses UID_MIN from
# /etc/login.defs (default 1000); macOS human accounts start at 501.
_service_account_uid_ok() {
  local user="$1" uid uid_min
  uid="$(id -u "$user" 2>/dev/null)" || return 1
  [[ "$uid" =~ ^[0-9]+$ ]] || return 1
  if [[ "$OS" == "linux" ]]; then
    uid_min="$(awk '$1=="UID_MIN"{print $2; exit}' /etc/login.defs 2>/dev/null)"
    [[ "$uid_min" =~ ^[0-9]+$ ]] || uid_min=1000
  else
    uid_min=501
  fi
  (( uid > 0 && uid < uid_min ))
}

# Single guarded path for deleting the runtime account — refuses anything
# that isn't a system-range UID, and names the account in the prompt.
_offer_delete_run_user() {
  local user="$1" uid
  uid="$(id -u "$user" 2>/dev/null || echo '?')"
  banner "Runtime user $user"
  if ! _service_account_uid_ok "$user"; then
    warn "detected runtime user '$user' (uid $uid) is NOT in the system-account UID range."
    warn "It may be a real login account (detection follows the unit file's User= and"
    warn "install-dir ownership). Refusing to delete it — remove it manually if intended."
    return 0
  fi
  warn "If '$user' owns files outside the install dirs, userdel will fail or leave orphans."
  if confirm "Delete user '$user' (uid $uid) and its home directory?"; then
    if [[ "$OS" == "linux" ]]; then
      $SUDO userdel -r "$user" 2>/dev/null || warn "userdel failed (user may own files elsewhere)"
    else
      $SUDO dscl . -delete "/Users/$user" 2>/dev/null || warn "dscl delete failed"
    fi
    ok "user '$user' removed"
  fi
}

# ── OS + privilege detection ───────────────────────────────────────────────
case "$(uname -s)" in
  Linux)  OS=linux ;;
  Darwin) OS=macos ;;
  *) die "Unsupported OS: $(uname -s)" ;;
esac
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
ok "OS detected: $OS"

# ── Detect the deployed run-as user ────────────────────────────────────────
# Honor `install.sh --user foo` deployments — without this, uninstall would
# only prompt to remove the literal `llmsys` account and silently leave a
# custom service account behind. Detect from the manager unit first, AE unit
# second, install dir ownership third; fall back to `llmsys` so legacy hosts
# still work.
RUN_USER="llmsys"
for _unit in /etc/systemd/system/llm-systems-manager.service \
             /etc/systemd/system/llm-systems-alarm-engine.service \
             /etc/systemd/system/llm-systems-agent.service; do
  if [[ -f "$_unit" ]]; then
    _u="$(awk -F= '/^User=/{print $2; exit}' "$_unit" 2>/dev/null || true)"
    if [[ -n "$_u" ]]; then RUN_USER="$_u"; break; fi
  fi
done
if [[ "$RUN_USER" == "llmsys" && -d /opt/llm-systems-manager ]]; then
  _u="$(stat -c %U /opt/llm-systems-manager 2>/dev/null || stat -f %Su /opt/llm-systems-manager 2>/dev/null || true)"
  [[ -n "$_u" ]] && RUN_USER="$_u"
fi
unset _unit _u

# ── Detection: walk every place the installer touches and record what's here.
# Each list is filled with absolute paths or unit names; the removal phase
# below iterates these. An empty host produces empty lists and bails early.
# ----------------------------------------------------------------------------
FOUND_UNITS=()       # systemd unit names (Linux)
FOUND_LAUNCHD=()     # launchd plist paths (macOS)
FOUND_DIRS=()        # install dirs to remove
FOUND_FILES=()       # sudoers fragments + similar single files
FOUND_CACHES=()      # /tmp clones, /var/log dirs, ~/Library/Logs dirs
INFLUX_INSTALLED=false

if [[ "$OS" == "linux" ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    while IFS= read -r unit; do
      [[ -n "$unit" ]] && FOUND_UNITS+=("$unit")
    done < <(systemctl list-unit-files --no-legend 2>/dev/null \
             | awk '{print $1}' \
             | grep -E '^llm-systems-(manager|alarm-engine|agent)\.service$' || true)
    if systemctl list-unit-files --no-legend 2>/dev/null \
         | awk '{print $1}' | grep -qx "influxdb.service"; then
      INFLUX_INSTALLED=true
    fi
  fi
  [[ -f /etc/sudoers.d/llm-systems-agent ]]   && FOUND_FILES+=("/etc/sudoers.d/llm-systems-agent")
  [[ -f /etc/sudoers.d/llm-systems-manager ]] && FOUND_FILES+=("/etc/sudoers.d/llm-systems-manager")
  [[ -d /var/log/llm-systems-manager ]]       && FOUND_CACHES+=("/var/log/llm-systems-manager")
fi

if [[ "$OS" == "macos" ]]; then
  # System-wide LaunchDaemon (rare for this project; agent installs as user agent)
  [[ -f /Library/LaunchDaemons/com.llm-systems-agent.plist ]] \
    && FOUND_LAUNCHD+=("/Library/LaunchDaemons/com.llm-systems-agent.plist")
  # Per-user LaunchAgent — scan every human-owned home directory. UniqueIDs
  # under 500 are system accounts on macOS; humans start at 501.
  while IFS= read -r user; do
    [[ -z "$user" ]] && continue
    home="$(dscl . -read "/Users/$user" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
    [[ -z "$home" ]] && continue
    plist="$home/Library/LaunchAgents/com.llm-systems-agent.plist"
    [[ -f "$plist" ]] && FOUND_LAUNCHD+=("$plist")
    [[ -d "$home/Library/Logs/llm-systems-agent" ]] \
      && FOUND_CACHES+=("$home/Library/Logs/llm-systems-agent")
  done < <(dscl . -list /Users UniqueID 2>/dev/null | awk '$2>=500{print $1}')
fi

# Install dirs — both the manager and the agent locations the installer uses.
for d in /opt/llm-systems-manager /opt/llm-systems-agent; do
  [[ -d "$d" ]] && FOUND_DIRS+=("$d")
done

# launchd plists may point to a non-default install dir; mine the file for it.
for plist in "${FOUND_LAUNCHD[@]:-}"; do
  [[ -z "${plist:-}" ]] && continue
  wd="$(awk '/<key>WorkingDirectory<\/key>/{getline; gsub(/.*<string>|<\/string>.*/,""); print; exit}' \
        "$plist" 2>/dev/null || true)"
  [[ -n "$wd" && -d "$wd" ]] && FOUND_DIRS+=("$wd")
done

# Cached repo clone the bootstrap leaves at /tmp on every host.
[[ -d /tmp/llm-systems-manager-install ]] && FOUND_CACHES+=("/tmp/llm-systems-manager-install")

# Dedupe FOUND_DIRS without using mapfile (macOS bash 3.2 doesn't have it).
if (( ${#FOUND_DIRS[@]} > 0 )); then
  _seen=""; _uniq=()
  for d in "${FOUND_DIRS[@]}"; do
    case "$_seen" in *"|$d|"*) ;; *) _uniq+=("$d"); _seen="$_seen|$d|" ;; esac
  done
  FOUND_DIRS=("${_uniq[@]}")
  unset _seen _uniq
fi

# ── Nothing to do? bail clean. ─────────────────────────────────────────────
_total=$(( ${#FOUND_UNITS[@]} + ${#FOUND_LAUNCHD[@]} \
        + ${#FOUND_DIRS[@]}  + ${#FOUND_FILES[@]}  + ${#FOUND_CACHES[@]} ))
if (( _total == 0 )) && ! $INFLUX_INSTALLED; then
  if id "$RUN_USER" >/dev/null 2>&1; then
    log "no installer artifacts found, but user '$RUN_USER' still exists"
    _offer_delete_run_user "$RUN_USER"
  else
    ok "nothing to uninstall — host is clean"
  fi
  exit 0
fi

# ── Summary ────────────────────────────────────────────────────────────────
banner "Detected installation"
for x in "${FOUND_UNITS[@]:-}";   do [[ -n "$x" ]] && log "systemd unit:   $x"; done
for x in "${FOUND_LAUNCHD[@]:-}"; do [[ -n "$x" ]] && log "launchd plist:  $x"; done
for x in "${FOUND_DIRS[@]:-}";    do [[ -n "$x" ]] && log "install dir:    $x"; done
for x in "${FOUND_FILES[@]:-}";   do [[ -n "$x" ]] && log "config file:    $x"; done
for x in "${FOUND_CACHES[@]:-}";  do [[ -n "$x" ]] && log "cache / log:    $x"; done
$INFLUX_INSTALLED                                 && log "influxdb.service installed (separate confirmation)"

echo
if ! confirm "Proceed with uninstall? (each step asks individually)"; then
  ok "aborted; nothing removed"
  exit 0
fi

# ── Stop + remove services ─────────────────────────────────────────────────
if (( ${#FOUND_UNITS[@]} > 0 )); then
  banner "Stopping services"
  for unit in "${FOUND_UNITS[@]}"; do
    log "stopping $unit"
    $SUDO systemctl stop "$unit"   2>/dev/null || true
    $SUDO systemctl disable "$unit" 2>/dev/null || true
    for path in "/etc/systemd/system/$unit" "/lib/systemd/system/$unit"; do
      if $SUDO test -f "$path"; then
        $SUDO rm -f "$path"
        ok "removed $path"
      fi
    done
  done
  $SUDO systemctl daemon-reload 2>/dev/null || true
fi

# ── Unload + remove launchd plists ─────────────────────────────────────────
for plist in "${FOUND_LAUNCHD[@]:-}"; do
  [[ -z "$plist" ]] && continue
  banner "Removing launchd plist"
  # User plist → unload as user; system plist → unload via sudo.
  case "$plist" in
    /Library/*)
      $SUDO launchctl unload "$plist" 2>/dev/null || true
      $SUDO rm -f "$plist"
      ;;
    *)
      launchctl unload "$plist" 2>/dev/null || true
      rm -f "$plist" 2>/dev/null || $SUDO rm -f "$plist"
      ;;
  esac
  ok "removed $plist"
done

# ── Sudoers + other single files ───────────────────────────────────────────
for f in "${FOUND_FILES[@]:-}"; do
  [[ -z "$f" ]] && continue
  if confirm "Remove $f ?"; then
    $SUDO rm -f "$f"
    ok "removed $f"
  fi
done

# ── Install dirs ───────────────────────────────────────────────────────────
for d in "${FOUND_DIRS[@]:-}"; do
  [[ -z "$d" ]] && continue
  banner "Install tree at $d"
  warn "Contains config, venvs, data, layout, internal-CA certs."
  if confirm "Delete $d ?"; then
    $SUDO rm -rf "$d"
    ok "removed $d"
  fi
done

# ── Caches + logs ──────────────────────────────────────────────────────────
for c in "${FOUND_CACHES[@]:-}"; do
  [[ -z "$c" ]] && continue
  if confirm "Delete $c ?"; then
    rm -rf "$c" 2>/dev/null || $SUDO rm -rf "$c"
    ok "removed $c"
  fi
done

# ── Runtime user (auto-detected) ───────────────────────────────────────────
if id "$RUN_USER" >/dev/null 2>&1; then
  _offer_delete_run_user "$RUN_USER"
fi

# ── InfluxDB (Linux only — manager host) ───────────────────────────────────
if $INFLUX_INSTALLED; then
  banner "InfluxDB"
  warn "Removing InfluxDB deletes the metrics database. Backups happen via 'influx backup'."
  if confirm "Stop + apt-purge influxdb2 + influxdb2-cli?"; then
    $SUDO systemctl stop influxdb.service    2>/dev/null || true
    $SUDO systemctl disable influxdb.service 2>/dev/null || true
    $SUDO apt-get purge -y influxdb2 influxdb2-cli 2>/dev/null || warn "apt purge failed"
    if confirm "Also delete /var/lib/influxdb (all bucket data)?"; then
      $SUDO rm -rf /var/lib/influxdb /etc/influxdb
      ok "removed /var/lib/influxdb /etc/influxdb"
    fi
    if confirm "Remove InfluxData apt source and GPG key?"; then
      $SUDO rm -f /etc/apt/sources.list.d/influxdata.list /etc/apt/keyrings/influxdata-archive.gpg
      $SUDO apt-get update -qq 2>/dev/null || true
      ok "InfluxData apt source removed"
    fi
    if confirm "Remove cached operator/admin tokens in /root?"; then
      $SUDO rm -f /root/.influxdb-operator-token /root/.influxdb-admin-pass
      ok "removed cached tokens"
    fi
  fi
fi

banner "Done"
ok "Uninstall complete. Anything you opted to keep is still on disk."
