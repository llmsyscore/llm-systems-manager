#!/usr/bin/env bash
# =============================================================================
# tools/installer/lib-common.sh — shared helpers for the universal installer
#
# Sourced by install.sh and every tools/installer/install-*.sh sub-script.
# Provides logging, prereq probing, the apt-install offer flow, public-repo
# git clone helpers, and a few common paths.
#
# All variables here are read-only constants OR are namespaced with
# LLMSYS_ to make sourcing safe.
# =============================================================================
set -euo pipefail

# ── Paths ───────────────────────────────────────────────────────────────────
LLMSYS_INSTALL_DIR="${LLMSYS_INSTALL_DIR:-/opt/llm-systems-manager}"
LLMSYS_REPO_SLUG="${LLMSYS_REPO_SLUG:-llmsyscore/llm-systems-manager}"
LLMSYS_REPO_URL="${LLMSYS_REPO_URL:-https://github.com/${LLMSYS_REPO_SLUG}}"
LLMSYS_CLONE_TMP="${LLMSYS_CLONE_TMP:-/tmp/llm-systems-manager-install}"
LLMSYS_RUN_USER="${LLMSYS_RUN_USER:-llmsys}"
LLMSYS_RUN_GROUP="${LLMSYS_RUN_GROUP:-llmsys}"

# ── Logging ─────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  LLMSYS_C_RED=$'\033[31m'
  LLMSYS_C_GRN=$'\033[32m'
  LLMSYS_C_YLW=$'\033[33m'
  LLMSYS_C_BLU=$'\033[34m'
  LLMSYS_C_RST=$'\033[0m'
else
  LLMSYS_C_RED=""; LLMSYS_C_GRN=""; LLMSYS_C_YLW=""; LLMSYS_C_BLU=""; LLMSYS_C_RST=""
fi

log()  { printf '%s[INFO]%s  %s\n'  "$LLMSYS_C_BLU" "$LLMSYS_C_RST" "$*"; }
ok()   { printf '%s[ OK ]%s  %s\n'  "$LLMSYS_C_GRN" "$LLMSYS_C_RST" "$*"; }
warn() { printf '%s[WARN]%s  %s\n'  "$LLMSYS_C_YLW" "$LLMSYS_C_RST" "$*" >&2; }
err()  { printf '%s[ERR ]%s  %s\n'  "$LLMSYS_C_RED" "$LLMSYS_C_RST" "$*" >&2; }
die()  { err "$*"; exit 1; }

banner() {
  local title="$1"
  printf '\n%s── %s %s\n' "$LLMSYS_C_BLU" "$title" \
    "$(printf '─%.0s' $(seq 1 $((72 - ${#title}))))${LLMSYS_C_RST}"
}

# Strip known-noisy harmless pip warnings (case-insensitive — pip emits both
# "Cache entry deserialization failed" and "cache entry deserialization failed"
# across versions; the previous pattern was lowercase-only and let the capital-C
# variant through). Exit status preserved.
pip_filter() {
  "$@" 2> >(grep -viE 'cache entry deserialization failed' >&2)
}

# confirm <prompt> [default] — interactive yes/no; default "y" (empty=yes) or
# "n" (empty=no). Returns 0 for yes, 1 for no. Caller owns non-TTY handling.
confirm() {
  local prompt="$1" default="${2:-n}" ans hint='[y/N]'
  [[ "$default" == "y" ]] && hint='[Y/n]'
  read -rp "$prompt $hint " ans
  ans="${ans,,}"
  [[ -z "$ans" ]] && ans="$default"
  [[ "$ans" == "y" || "$ans" == "yes" ]]
}

# ── Privilege wrapper ───────────────────────────────────────────────────────
# Set SUDO="" when running as root, "sudo" otherwise. Caller can override.
detect_sudo() {
  if [[ $EUID -eq 0 ]]; then
    SUDO=""
  else
    SUDO="sudo"
  fi
  export SUDO
}

# as_run_user <cmd...> — run a command AS $LLMSYS_RUN_USER, dropping privileges
# whether the caller is root ($SUDO empty) or not. Prefers `sudo -u`; falls back
# to `runuser` (util-linux, always present) for a root host without sudo.
# `$SUDO -u "$user"` silently breaks under root (collapses to `-u user …`).
as_run_user() {
  if command -v sudo >/dev/null 2>&1; then
    sudo -u "$LLMSYS_RUN_USER" "$@"
  else
    runuser -u "$LLMSYS_RUN_USER" -- "$@"
  fi
}

# resolve_run_group <user> — set LLMSYS_RUN_GROUP to the user's ACTUAL primary
# group. Call AFTER the user exists (useradd's group choice varies by
# USERGROUPS_ENAB). Falls back to the current value on lookup miss.
resolve_run_group() {
  LLMSYS_RUN_GROUP="$(id -gn "$1" 2>/dev/null || echo "$LLMSYS_RUN_GROUP")"
  export LLMSYS_RUN_GROUP
}

# ensure_apt_prereqs <pkg...> — install any missing apt packages (prompted).
ensure_apt_prereqs() {
  local missing=() nonempty=() p
  mapfile -t missing < <(missing_apt_pkgs "$@")
  for p in "${missing[@]}"; do [[ -n "$p" ]] && nonempty+=("$p"); done
  if (( ${#nonempty[@]} > 0 )); then
    offer_apt_install "${nonempty[@]}" || die "Required apt packages missing"
  fi
}

# ensure_log_dir <path> — create the dir, own it $LLMSYS_RUN_USER:$LLMSYS_RUN_GROUP, 0755.
ensure_log_dir() {
  local dir="$1"
  $SUDO mkdir -p "$dir"
  $SUDO chown "$LLMSYS_RUN_USER:$LLMSYS_RUN_GROUP" "$dir"
  $SUDO chmod 0755 "$dir"
}

# enable_unit <unit_name> — systemd daemon-reload + enable (does NOT start).
enable_unit() {
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$1"
}

# read_toml_key <key> <toml_file> — echo the double-quoted value of the first
# top-level `<key> = "..."` line. Empty on missing key/file. Uses $SUDO so it
# can read a 0600 operator-owned config; safe under set -o pipefail.
read_toml_key() {
  local key="$1" file="$2"
  $SUDO test -f "$file" 2>/dev/null || { echo ""; return 0; }
  { $SUDO grep -E "^[[:space:]]*${key}[[:space:]]*=" "$file" 2>/dev/null || true; } \
    | head -n1 | sed -E 's/[^"]*"([^"]+)".*/\1/'
}

# ── Systemd unit + sudoers templating ───────────────────────────────────────
# render_unit_template <src> <out> — substitute the @@INSTALL_DIR@@/@@RUN_USER@@/
# @@RUN_GROUP@@ tokens a *.service.example uses, writing the result to <out>.
render_unit_template() {
  local src="$1" out="$2"
  sed -e "s|@@INSTALL_DIR@@|$LLMSYS_INSTALL_DIR|g" \
      -e "s|@@RUN_USER@@|$LLMSYS_RUN_USER|g" \
      -e "s|@@RUN_GROUP@@|$LLMSYS_RUN_GROUP|g" \
      "$src" > "$out"
}

# install_unit_template <src_tpl> <unit_dst> — render to a temp and install 0644.
# Removes the temp on every path. Returns install's exit status.
install_unit_template() {
  local tpl="$1" dst="$2" rendered rc=0
  rendered="$(mktemp)"
  render_unit_template "$tpl" "$rendered"
  $SUDO install -m 0644 "$rendered" "$dst" || rc=$?
  rm -f "$rendered"
  return "$rc"
}

# install_sudoers_fragment <tpl> <dst> — render @@RUN_USER@@, visudo-validate, and
# install 0440 root:root only if valid. Removes the temp on every path. Returns 0
# on install, 1 on missing template / invalid fragment.
install_sudoers_fragment() {
  local tpl="$1" dst="$2" rendered rc=0
  if [[ ! -f "$tpl" ]]; then
    warn "sudoers template missing ($tpl) — admin-tab restart disabled"
    return 1
  fi
  rendered="$(mktemp)"
  sed -e "s|@@RUN_USER@@|$LLMSYS_RUN_USER|g" "$tpl" > "$rendered"
  if $SUDO visudo -cf "$rendered" >/dev/null 2>&1; then
    if $SUDO install -m 0440 -o root -g root "$rendered" "$dst"; then
      ok "sudoers fragment installed ($dst)"
    else
      warn "sudoers fragment install failed — admin-tab restart will error until fixed"
      rc=1
    fi
  else
    warn "sudoers fragment failed visudo validation — admin-tab restart will error until fixed"
    rc=1
  fi
  rm -f "$rendered"
  return "$rc"
}

# ── OS detection ────────────────────────────────────────────────────────────
detect_os() {
  case "$(uname -s)" in
    Linux)  LLMSYS_OS="Linux"  ;;
    Darwin) LLMSYS_OS="Darwin"  ;;
    *)      LLMSYS_OS="other"  ;;
  esac
  export LLMSYS_OS
}

require_linux() {
  [[ "${LLMSYS_OS:-}" == "Linux" ]] || \
    die "This component only runs on Linux (detected: ${LLMSYS_OS:-unknown})."
}

# ── Prereq probing ──────────────────────────────────────────────────────────
have() { command -v "$1" >/dev/null 2>&1; }

# python3 >= 3.10 check
python_ok() {
  have python3 || return 1
  python3 - <<'PYEOF' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PYEOF
}

# Returns list of missing apt packages (Linux-only)
missing_apt_pkgs() {
  local pkgs=("$@") missing=()
  for p in "${pkgs[@]}"; do
    if ! dpkg -s "$p" >/dev/null 2>&1; then
      missing+=("$p")
    fi
  done
  printf '%s\n' "${missing[@]}"
}

# offer_apt_install <pkg> [<pkg>...]
#   Prompts the user; on yes, runs apt-get install. Returns 0 on install
#   success or no-op, 1 on refusal/failure.
offer_apt_install() {
  local pkgs=("$@")
  if (( ${#pkgs[@]} == 0 )); then return 0; fi
  warn "Missing system packages: ${pkgs[*]}"
  if [[ ! -t 0 ]]; then
    err "stdin is not a TTY; cannot prompt. Install manually:"
    err "  sudo apt-get install -y ${pkgs[*]}"
    return 1
  fi
  if ! confirm "  Install with apt-get now?" y; then
    err "Refused — install manually and re-run."; return 1
  fi
  detect_sudo
  apt_update_once
  $SUDO apt-get install -y --no-install-recommends "${pkgs[@]}"
}

# validate_influx_token <label> <token> [require_eqeq]
#   Sanity-check a pasted InfluxDB v2 token. Refuses anything that doesn't
#   look like a token to keep an accidental shell snippet / command pipe out
#   of the TOML (which is later sourced by Python — not bash — but the value
#   ends up in $SUDO-readable files and journald, so we still reject
#   characters that have no business in a base64-ish token).
#
#   - Allowed chars: [A-Za-z0-9+/=_-] (standard + URL-safe base64 alphabets)
#   - Length: 40..200 (real tokens are 88 chars; bound keeps us sane)
#   - If require_eqeq=1: must end in '==' (Influx scoped tokens do)
#   Returns 0 if OK, 1 with err() on failure.
validate_influx_token() {
  local label="$1" tok="$2" require_eqeq="${3:-0}"
  if [[ -z "$tok" ]]; then
    err "$label is empty"; return 1
  fi
  if (( ${#tok} < 40 || ${#tok} > 200 )); then
    err "$label looks wrong (length ${#tok}; expected ~88)"; return 1
  fi
  if [[ ! "$tok" =~ ^[A-Za-z0-9+/=_-]+$ ]]; then
    err "$label contains disallowed characters (only base64 chars allowed)"
    return 1
  fi
  if (( require_eqeq )) && [[ "$tok" != *== ]]; then
    err "$label must end in '==' (scoped InfluxDB v2 tokens always do)"
    return 1
  fi
  return 0
}

# Run apt-get update with one specific recovery path: if it fails with the
# 'Release file ... is not valid yet (invalid for another Xh Ymin Zs)' error
# (system clock is behind real time — common after a VM snapshot rollback),
# offer a one-time 'sudo date -s' that does NOT enable NTP or modify any
# system services, just nudges the clock for this run, then retries once.
apt_update_with_clock_recovery() {
  detect_sudo
  local output rc=0
  # Put the failing command in an if-context so 'set -e' in the caller
  # doesn't bail before we even examine the output.
  if output="$($SUDO apt-get update 2>&1)"; then
    printf '%s\n' "$output" | grep -E '^(Get|Hit|Reading)' || true
    return 0
  else
    rc=$?
  fi
  # Always show what apt said so the operator sees the real error.
  printf '%s\n' "$output" >&2
  if printf '%s' "$output" | grep -q 'is not valid yet (invalid for another'; then
    # apt prints one error line PER lagging repo, each with its own skew
    # amount. Take the MAXIMUM across all lines (+60s safety margin) so one
    # date adjustment lands every repo ahead of its "valid from" timestamp.
    # Picking the first line — what we used to do — left repos with bigger
    # skews still rejected after the supposed fix.
    local total max_total=0 line h m s
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      h="$(printf '%s' "$line" | grep -oE '[0-9]+h'   | head -n1 | tr -d h   || true)"
      m="$(printf '%s' "$line" | grep -oE '[0-9]+min' | head -n1 | tr -d min || true)"
      s="$(printf '%s' "$line" | grep -oE '[0-9]+s'   | head -n1 | tr -d s   || true)"
      : "${h:=0}" "${m:=0}" "${s:=0}"
      total=$(( 10#$h * 3600 + 10#$m * 60 + 10#$s ))
      (( total > max_total )) && max_total=$total
    done < <(printf '%s' "$output" | grep -oE 'invalid for another [^)]+')
    if (( max_total <= 0 )); then return $rc; fi
    total=$(( max_total + 60 ))
    warn ""
    warn "apt rejected the repo metadata because the system clock is ~${total}s"
    warn "behind real time (common after a VM snapshot rollback)."
    warn "No system services will be modified — only a one-shot 'date -s' that"
    warn "advances the clock for THIS install run. It does NOT enable NTP and"
    warn "does NOT persist."
    if [[ -t 0 ]]; then
      confirm "  Apply one-time clock fix and retry apt?" y \
        || die "clock skew unresolved — fix the clock and re-run"
    else
      die "clock skew detected and stdin is not a TTY — fix and re-run"
    fi
    local new_epoch new_time
    new_epoch=$(( $(date -u +%s) + total ))
    new_time="$(date -u -d "@$new_epoch" '+%Y-%m-%d %H:%M:%S UTC')"
    if ! $SUDO date -u -s "$new_time" >/dev/null 2>&1; then
      die "failed to set clock with 'sudo date -s' — fix manually and re-run"
    fi
    ok "clock advanced to $new_time (one-time, no persistent change)"
    log "retrying apt-get update"
    if $SUDO apt-get update; then
      return 0
    else
      return $?
    fi
  fi
  return $rc
}

# Run `apt-get update` once per install run via the LLMSYS_APT_STAMP file
# (spans sub-installer processes), or once per process when that's unset.
: "${LLMSYS_APT_STAMP:=}"
_APT_UPDATED=0
apt_update_once() {
  if [[ -n "$LLMSYS_APT_STAMP" && -e "$LLMSYS_APT_STAMP" ]] \
     || [[ "$_APT_UPDATED" == "1" ]]; then
    return 0
  fi
  apt_update_with_clock_recovery || return $?
  _APT_UPDATED=1
  [[ -n "$LLMSYS_APT_STAMP" ]] && : > "$LLMSYS_APT_STAMP" 2>/dev/null || true
}
export _APT_UPDATED LLMSYS_APT_STAMP

# ── GitHub fetch prerequisite ───────────────────────────────────────────────
require_git() {
  # The repo is public — a plain HTTPS git clone/pull fetches it with no
  # authentication. Only git is required.
  if have git; then
    return 0
  fi
  die "git is required to fetch the repo. Install git and re-run."
}

# ── User/group management ───────────────────────────────────────────────────
ensure_runas_user() {
  local user="${1:-$LLMSYS_RUN_USER}"
  if id "$user" >/dev/null 2>&1; then
    log "user '$user' already exists (uid=$(id -u "$user"))"
    return 0
  fi
  log "creating system user '$user'"
  detect_sudo
  # No -p, so the shadow password field is created as '!' — the account is
  # password-locked by default. systemd (User=), sudo -u, sudo -i -u, SSH
  # key auth, and file ownership all still work; only password-based su /
  # SSH password login are blocked. That's the correct posture for a service
  # account. Operator who wants an interactive shell uses sudo -i -u, or
  # runs `sudo passwd $user` later to unlock.
  $SUDO useradd --system --create-home --shell /bin/bash "$user"
  ok "created user '$user' (password-locked service account)"
  log "  interactive shell:  sudo -i -u $user"
  log "  set a password:     sudo passwd $user   (if you actually want password login)"
}

# ── Repo clone (public, via git over HTTPS) ─────────────────────────────────
clone_repo() {
  local dest="${1:-$LLMSYS_CLONE_TMP}"
  if [[ -d "$dest/.git" ]]; then
    log "repo already cloned at $dest — pulling latest"
    git -C "$dest" pull --ff-only -q >/dev/null 2>&1 || warn "git pull failed; using existing checkout"
    return 0
  fi
  if [[ -e "$dest" ]]; then
    if [[ -t 0 ]]; then
      if confirm "  $dest exists but isn't a git repo. Remove and re-clone?" n; then
        rm -rf "$dest"
      else
        die "Refusing to clone over $dest"
      fi
    else
      die "$dest exists and isn't a git repo (non-interactive — aborting)"
    fi
  fi
  local slug="${LLMSYS_REPO_SLUG:-llmsyscore/llm-systems-manager}"
  have git || die "git is required to clone $slug. Install git and re-run."
  log "cloning $slug via public HTTPS → $dest"
  git clone -q "https://github.com/$slug.git" "$dest" >/dev/null 2>&1 \
    || die "git clone https://github.com/$slug.git → $dest failed"
  ok "cloned to $dest"
}

# ── Deploy clone into INSTALL_DIR (with backup of existing config) ──────────
deploy_into_install_dir() {
  local src="$1" dest="${2:-$LLMSYS_INSTALL_DIR}"
  shift 2 || true
  local extra_excludes=("$@")   # caller-supplied paths to omit (e.g. unused service tree)
  detect_sudo

  if [[ -d "$dest" ]]; then
    if [[ -t 0 ]]; then
      warn "$dest already exists."
      confirm "  Overwrite (rsync new files in; existing config backed up)?" n \
        || die "Aborted by user."
    else
      warn "$dest exists; non-interactive — proceeding with rsync (config backed up)"
    fi
    # Back up live config if present
    if [[ -f "$dest/config/llm-systems.toml" ]]; then
      local stamp
      stamp="$(date +%Y%m%d-%H%M%S)"
      $SUDO cp -a "$dest/config/llm-systems.toml" \
                  "$dest/config/llm-systems.toml.bak.$stamp"
      ok "backed up existing config → llm-systems.toml.bak.$stamp"
    fi
  fi

  have rsync || offer_apt_install rsync || die "rsync required"
  $SUDO mkdir -p "$dest"
  # Strip dev-only artifacts. .git is the full repo, .claude is Claude Code
  # session state, .github is CI config — none belong in a production deploy.
  local rsync_extra=()
  if (( ${#extra_excludes[@]} > 0 )); then
    for e in "${extra_excludes[@]}"; do rsync_extra+=("--exclude=$e"); done
    log "deploy excludes (mode-specific): ${extra_excludes[*]}"
  fi
  # If we're re-deploying over an existing tree and now excluding a subdir
  # that was deployed previously, rsync won't remove it. Wipe stale
  # service trees explicitly so the install matches the mode.
  for e in "${extra_excludes[@]}"; do
    local stale="$dest/${e%/}"
    if $SUDO test -d "$stale"; then
      $SUDO rm -rf "$stale"
      ok "removed stale tree $stale (excluded by current mode)"
    fi
  done
  $SUDO rsync -a \
              --exclude='.git' --exclude='.git/' \
              --exclude='.gitignore' --exclude='.gitattributes' \
              --exclude='.github' --exclude='.github/' \
              --exclude='.claude' --exclude='.claude/' \
              --exclude='venv/' --exclude='__pycache__/' \
              --exclude='data/' --exclude='backups/' \
              --exclude='plans/' \
              --exclude='tests/' --exclude='pytest.ini' \
              --exclude='requirements-dev.txt' \
              --exclude='.pytest_cache/' \
              "${rsync_extra[@]}" \
              "$src/" "$dest/"
  $SUDO chown -R "$LLMSYS_RUN_USER:$LLMSYS_RUN_GROUP" "$dest"
  ok "deployed $src → $dest (owner $LLMSYS_RUN_USER:$LLMSYS_RUN_GROUP)"
}

# ── URL sanitization ────────────────────────────────────────────────────────
# Normalize operator input into a fully-qualified URL. Accepts:
#   1.1.1.1                    → http://1.1.1.1:<default_port>
#   1.1.1.1:1234               → http://1.1.1.1:1234
#   http://1.1.1.1             → http://1.1.1.1:<default_port>
#   http://1.1.1.1:5000        → http://1.1.1.1:5000
#   https://host.tld           → https://host.tld     (no port forced; HTTPS=443)
#   host.example.com           → http://host.example.com:<default_port>
# Trims trailing slashes. Empty input echoes empty.
#
# Usage: sanitized="$(sanitize_url "$input" 5000)"
sanitize_url() {
  local raw="${1:-}" default_port="${2:-}"
  [[ -z "$raw" ]] && { printf ''; return 0; }
  # trim leading + trailing whitespace
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  raw="${raw%/}"                        # trim one trailing slash
  # Add scheme if missing. Anything starting with http:// or https:// keeps it.
  if [[ ! "$raw" =~ ^https?:// ]]; then
    raw="http://$raw"
  fi
  # Already has a port? Match scheme://host:NNNN[/...]
  if [[ "$raw" =~ ^https?://[^/]+:[0-9]+(/.*)?$ ]]; then
    printf '%s' "$raw"; return 0
  fi
  # No port — append default if caller supplied one. HTTPS without default is
  # left alone (browser uses 443).
  if [[ -z "$default_port" ]]; then
    printf '%s' "$raw"; return 0
  fi
  local scheme rest
  scheme="${raw%%://*}"; rest="${raw#*://}"
  # rest may contain a path after the host; split it.
  local host path=""
  if [[ "$rest" == *"/"* ]]; then
    host="${rest%%/*}"; path="/${rest#*/}"
  else
    host="$rest"
  fi
  printf '%s://%s:%s%s' "$scheme" "$host" "$default_port" "$path"
}

# Transient token-handoff between install-influxdb.sh / resolve-influxdb.sh
# and install-config-bootstrap.sh. install.sh sets this to a mktemp path
# with a trap that unlinks on exit; ad-hoc re-runs of the sub-scripts fall
# back to a stable /tmp name so they still produce something consumable.
: "${LLMSYS_INFLUXDB_TOKEN_FILE:=/tmp/llmsys-influxdb-tokens.env}"
export LLMSYS_INFLUXDB_TOKEN_FILE


# ── URL helpers ────────────────────────────────────────────────────────────
# Pull host or port out of a sanitize_url-shaped URL (scheme://host[:port][/...]).
# Both return empty when the URL doesn't match the expected shape.
url_host() {
  printf '%s' "${1:-}" | sed -nE 's#^https?://([^:/]+).*#\1#p'
}
url_port() {
  printf '%s' "${1:-}" | sed -nE 's#^https?://[^:/]+:([0-9]+).*#\1#p'
}


# Write the five-key INFLUX_* env handoff file at mode 0600. Shared by
# install-influxdb.sh and resolve-influxdb.sh so the file shape stays in
# one place. Arguments: PATH URL ORG OP_TOKEN METRICS_TOKEN ROLLUP_TOKEN.
write_influx_token_file() {
  local path="$1" url="$2" org="$3" op="$4" metrics="$5" rollup="$6"
  install -m 0600 /dev/null "$path"
  cat > "$path" <<EOF
# Transient — consumed by install-config-bootstrap.sh, unlinked by
# install.sh on exit. Do not edit by hand.
INFLUX_HOST=$url
INFLUX_ORG=$org
INFLUX_OPERATOR_TOKEN=$op
INFLUX_METRICS_TOKEN=$metrics
INFLUX_METRICS_ROLLUP_TOKEN=$rollup
EOF
}


# ── Hostname resolution ────────────────────────────────────────────────────
# Bare IP literals pass through; hostnames go through getent (which honors
# both /etc/hosts and DNS). On miss, surface a clear suggestion + offer to
# patch /etc/hosts in interactive mode. Returns 0 if resolvable (or the
# operator opted to add the entry), non-zero otherwise. Safe to call in
# non-interactive mode — never prompts there, just warns and returns 1.
#
# Usage: check_resolves <host_or_ip> [<label_for_messages>]
check_resolves() {
  local host="$1" label="${2:-host}"
  [[ -z "$host" ]] && return 0
  # IPv4 literal or anything that looks IPv6-ish (contains a colon) short-
  # circuits — getent would accept these too, but skipping the resolver
  # call avoids any /etc/nsswitch surprises on hosts with funky DNS setups.
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ "$host" == *:* ]]; then
    return 0
  fi
  # `timeout 2` caps DNS stalls: glibc's resolver defaults to 5s × 2 tries,
  # which compounds across the 3+ check_resolves sites in the install flow.
  if timeout 2 getent ahosts "$host" >/dev/null 2>&1; then
    return 0
  fi
  warn "$label '$host' does not resolve via /etc/hosts or DNS"
  warn "  → manager → AE → InfluxDB calls and the install's cross-host"
  warn "    health probes will all fail with NXDOMAIN until this is fixed."
  if [[ ! -t 0 ]]; then
    return 1
  fi
  confirm "  Add an /etc/hosts entry for '$host' now?" n || return 1
  read -rp "  IP address for $host: " ip
  ip="${ip## }"; ip="${ip%% }"
  if [[ ! "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    warn "  '$ip' is not a valid IPv4 address — skipping"
    return 1
  fi
  # grep -F + word-boundary via the surrounding spaces is overly strict
  # (would miss tab-separated entries), so keep -E. Hostname metachar
  # collisions in /etc/hosts in practice are vanishingly rare.
  if grep -qE "[[:space:]]$host([[:space:]]|\$)" /etc/hosts 2>/dev/null; then
    warn "  /etc/hosts already mentions $host — refusing to add a duplicate"
    return 1
  fi
  printf '%s\t%s\n' "$ip" "$host" | ${SUDO:-} tee -a /etc/hosts >/dev/null \
    || { warn "  failed to append to /etc/hosts"; return 1; }
  ok "  added '$ip $host' to /etc/hosts"
  timeout 2 getent ahosts "$host" >/dev/null 2>&1 || {
    warn "  entry written but still not resolving — nsswitch / nscd issue?"
    return 1
  }
  return 0
}

# ── HTTP probe ──────────────────────────────────────────────────────────────
# -4 forces IPv4: uvicorn `--host 0.0.0.0` only binds IPv4, and on some
# distros `localhost` resolves to ::1 first. curl's documented fallback
# to A records sometimes loses within our 5s budget, producing HTTP 000
# for a service that's actually healthy on 127.0.0.1.
probe_url() {
  local url="$1" code
  code="$(curl -4 -s -m 5 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null)" || code="000"
  echo "${code:-000}"
}

# TCP-listener probe via bash's built-in /dev/tcp — no curl, no app stack.
# Returns 0 if the kernel accepts a TCP connect to host:port within ~1s,
# 1 otherwise. We don't care about the HTTP response here; the application
# health surface is exposed on the Admin tab's system-health card.
_tcp_open() {
  local host="$1" port="$2"
  # Subshell so the 2>/dev/null applies to bash's own "Connection
  # refused" / "No route to host" output. Inline `exec ... 2>/dev/null`
  # in the current shell leaks those errors on some bash builds —
  # bash prints them via the parent shell's stderr before honoring
  # the redirection on the exec line.
  ( exec 9<>/dev/tcp/"$host"/"$port" ) 2>/dev/null
}

# Used by the universal installer's final summary.
# Combines two signals: systemd thinks the unit is active AND the kernel
# accepts a TCP connection to the configured port. That answers "the
# service is up and bound" without depending on any app-layer endpoint
# response — which is the only thing flaky enough to falsely fail this
# probe on snapshot-fresh boxes.
report_service_health() {
  local label="$1" url="$2" _expect="${3:-200}" unit="${4:-}"
  local host port active=true bound=false attempts=5 i
  # Extract host:port from http://host:port[/path]. Defaults: 80 / 443.
  host="$(url_host "$url")"
  port="$(url_port "$url")"
  case "$url" in
    https://*) port="${port:-443}" ;;
    *)         port="${port:-80}"  ;;
  esac
  for ((i=1; i<=attempts; i++)); do
    if _tcp_open "$host" "$port"; then bound=true; break; fi
    (( i < attempts )) && sleep 2
  done
  if [[ -n "$unit" ]]; then
    if ! systemctl is-active --quiet "$unit"; then active=false; fi
  fi
  if $bound && $active; then
    ok "$label  → $url  (port $port open${unit:+, $unit active})"
    return 0
  else
    local why
    if   ! $active && ! $bound; then why="unit inactive AND port $port unreachable"
    elif ! $active;              then why="unit inactive"
    else                              why="port $port unreachable after $attempts attempts"; fi
    err "$label  → $url  ($why)"
    return 1
  fi
}
