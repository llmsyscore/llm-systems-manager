#!/usr/bin/env bash
# =============================================================================
# install.sh — universal LLM Systems Manager installer
#
# This is the curl-target entry point for setting up any of the six
# deployment shapes from a fresh box, plus update / uninstall / quit. The
# repo is public; it's fetched over HTTPS with git — no authentication needed.
#
# Quick start:
#   bash <(curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh)
#   # or non-interactively:
#   curl -fsSL https://raw.githubusercontent.com/llmsyscore/llm-systems-manager/main/tools/installer/install.sh \
#     | bash -s -- --mode 1
#
# Modes:
#   1) Full system        — manager + alarm engine + agent + InfluxDB
#   2) Manager + alarm    — assumes InfluxDB already runs somewhere
#   3) Manager only
#   4) Alarm engine only
#   5) Agent only         — delegates to agent/install/install.sh
#   6) InfluxDB only      — provisions InfluxDB + scoped tokens, nothing else
#   7) Update             — maps to --update (detect + diff + sync + restart)
#   8) Uninstall          — maps to --uninstall
#   9) Quit               — exit with no changes
#
# Uninstall:
#   sudo bash install.sh --uninstall
#     Stops services, removes unit files, deletes the install tree,
#     prompts before deleting the runtime user and InfluxDB itself.
# =============================================================================
set -euo pipefail

# Capture the original argv before any parsing — the self-update
# trampoline re-execs the upstream copy with these exact args, so a
# manual rebuild of --mode/--update/--uninstall/-- ... isn't needed
# (and can't silently drop newly-added flags).
_ORIG_ARGV=("$@")

# Revision integer. Format: YYYYMMDDNNN (date + same-day counter), matching
# the agent VERSION convention in CLAUDE.md operating rule #1. Bump on any
# substantive change to this file. The self-update trampoline only re-execs
# when the upstream copy carries a STRICTLY GREATER number, so locally-
# modified scripts (or unpushed commits) are never silently downgraded.
_INSTALL_SH_REVISION=20260613001

# Fallback bootstrap helpers — used until we source lib-common.sh.
# TTY-aware colors so OK/WARN/ERR markers stand out in interactive runs and
# stay plain in pipes / journalctl.
if [[ -t 1 ]]; then
  _B_GRN=$'\033[32m'; _B_YLW=$'\033[33m'; _B_RED=$'\033[31m'
  _B_BLU=$'\033[34m'; _B_RST=$'\033[0m'
else
  _B_GRN=""; _B_YLW=""; _B_RED=""; _B_BLU=""; _B_RST=""
fi
_b_ok()   { printf '%s[ OK ]%s  %s\n' "$_B_GRN" "$_B_RST" "$*"; }
_b_log()  { printf '%s[INFO]%s  %s\n' "$_B_BLU" "$_B_RST" "$*"; }
_b_warn() { printf '%s[WARN]%s  %s\n' "$_B_YLW" "$_B_RST" "$*" >&2; }
_b_err()  { printf '%s[ERR ]%s  %s\n' "$_B_RED" "$_B_RST" "$*" >&2; }
_b_die()  { _b_err "$*"; exit 1; }

# Fetch a repo-relative file from GitHub into $2 over public HTTPS with curl.
# The repo is public, so no authentication is needed. Returns 0 only when the
# resulting file is non-empty.
_b_fetch_from_github() {
  local repo_path="$1" dest="$2"
  local slug="${LLMSYS_REPO_SLUG:-llmsyscore/llm-systems-manager}"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSL "https://raw.githubusercontent.com/$slug/main/$repo_path" \
         -o "$dest" 2>/dev/null \
       && [[ -s "$dest" ]]; then
      return 0
    fi
  fi
  rm -f "$dest" 2>/dev/null
  return 1
}

# Locate one of the tools/installer/*.sh helpers — looks in the running
# checkout first, then the deployed tree. Echoes the path on stdout; non-zero
# return when neither exists.
_b_find_helper() {
  # install.sh always lives in tools/installer/ alongside its siblings
  # (uninstall.sh, update.sh, lib-common.sh, etc.), so THIS_DIR *is* the
  # helper directory — look for siblings, not for a nested tools/installer.
  # Fallback hits the canonical deployed tree for curl-piped invocations
  # where THIS_DIR is empty.
  local name="$1" cand
  for cand in \
      "${THIS_DIR:-}/$name" \
      /opt/llm-systems-manager/tools/installer/$name; do
    [[ -n "$cand" && -f "$cand" ]] && { echo "$cand"; return 0; }
  done
  return 1
}

MODE=""
UNINSTALL=0
UPDATE=0
NO_SELF_UPDATE=0
RUN_USER_OVERRIDE=""
FORWARD_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --mode=*) MODE="${1#*=}"; shift ;;
    --user) RUN_USER_OVERRIDE="${2:-}"; shift 2 ;;
    --user=*) RUN_USER_OVERRIDE="${1#*=}"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --update)    UPDATE=1; shift ;;
    --no-self-update) NO_SELF_UPDATE=1; shift ;;
    -h|--help)
      cat <<'HELP'
LLM Systems Manager — universal installer

Usage:
  install.sh [--mode N | --update | --uninstall] [-- <agent-install-args>...]

Options:
  --mode N        Skip the interactive menu and install mode N directly.
  --user USER     Run-as user for the manager + alarm-engine systemd units
                  (default: llmsys). Created as a password-locked system user
                  if it doesn't already exist. Agent mode 5 has its own --user
                  forwarded via '-- --user USER'.
  --update        Detect installed components and update them in place.
                  Forwards remaining args to tools/installer/update.sh.
                  Examples:
                    sudo bash install.sh --update -- --dry-run
                    sudo bash install.sh --update -- --only manager --skip-restart
  --uninstall     Remove services, unit files, install tree, sudoers fragment,
                  cached clone, runtime user, and (optionally) InfluxDB itself.
                  Asks before each destructive step.
  --no-self-update
                  Skip the upstream-install.sh check that normally fetches a
                  newer installer revision and re-execs it. Useful for offline
                  hosts, pinned testing, or while iterating on a local edit.
  -h, --help      Show this message and exit.
  --              End installer args; everything after is forwarded to the
                  agent installer (only relevant in mode 5 and the agent step
                  of mode 1).

Install modes:
  1   Full system        — manager + alarm engine + agent + InfluxDB
  2   Manager + alarm    — assumes InfluxDB already exists
  3   Manager only
  4   Alarm engine only
  5   Agent only         — Linux + macOS; delegates to agent/install/install.sh
  6   InfluxDB only      — provisions InfluxDB + scoped tokens, nothing else
  7   Update             — detect + diff + backup + sync only-changed files,
                           restart affected services, run smoke tests
  8   Uninstall          — remove services, unit files, install tree, sudoers fragment,
                           cached clone, runtime user, and (optionally) InfluxDB itself.
                           Asks before each destructive step
  9   Quit               — exit with no changes

Uninstall:
  sudo bash /opt/llm-systems-manager/tools/installer/install.sh --uninstall
HELP
      exit 0
      ;;
    --) shift; FORWARD_ARGS=("$@"); break ;;
    -*)
      _b_err "Unknown flag: $1"
      _b_err "  Run '$0 --help' to see accepted flags."
      _b_err "  To forward args to the agent installer, put them after a literal '--'."
      exit 2
      ;;
    *)
      _b_err "Unexpected positional argument: $1"
      _b_err "  Run '$0 --help' for usage."
      exit 2
      ;;
  esac
done

# ── Bootstrap: locate or fetch the lib-common.sh + sub-installers ──────────
# When run from a curl pipe, $0 is bash and we have no repo on disk yet.
# We need to clone first to get tools/installer/*.sh, then source lib-common
# from the clone. When run from a repo checkout, we can source directly.

THIS_DIR=""
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
  THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

cat <<'BANNER'


  ╭───────────────────────────────────────────────────────────────────╮
  │                                                                   │
  │              L L M   S Y S T E M S   M A N A G E R                │
  │                                                                   │
  │                       Universal installer                         │
  │                                                                   │
  ╰───────────────────────────────────────────────────────────────────╯

         Repo  ·  github.com/llmsyscore/llm-systems-manager


BANNER

printf '\n──────────────────────────  Preflight  ──────────────────────────\n\n'

# ── OS check ────────────────────────────────────────────────────────────────
OS_KERNEL="$(uname -s)"
case "$OS_KERNEL" in
  Linux)  OS=linux ;;
  Darwin) OS=macos ;;
  *) _b_die "Unsupported OS: $OS_KERNEL (Linux / macOS only)" ;;
esac
_b_ok "OS detected: $OS"

# ── Prereq check ────────────────────────────────────────────────────────────
NEEDED=()
have() { command -v "$1" >/dev/null 2>&1; }

if ! have python3; then NEEDED+=(python3 python3-venv); fi
if have python3 && ! python3 -m venv --help >/dev/null 2>&1; then NEEDED+=(python3-venv); fi
if ! have git;  then NEEDED+=(git);  fi
if ! have jq;   then NEEDED+=(jq);   fi
if ! have curl; then NEEDED+=(curl); fi
if ! have rsync; then NEEDED+=(rsync); fi

if (( ${#NEEDED[@]} > 0 )); then
  _b_err "Missing prerequisites: ${NEEDED[*]}"
  if [[ "$OS" == "linux" ]] && have apt-get; then
    if [[ -t 0 ]]; then
      read -rp "  Install with apt-get now? [Y/n] " ans
      case "$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]')" in
        ""|y|yes)
          SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
          $SUDO apt-get update -qq
          export _APT_UPDATED=1
          $SUDO apt-get install -y --no-install-recommends "${NEEDED[@]}"
          ;;
        *) _b_die "Refused — install manually and re-run." ;;
      esac
    else
      _b_die "Run: sudo apt-get install -y ${NEEDED[*]}"
    fi
  else
    # Non-apt host. Print a hint with the per-package-manager command we'd
    # use if we knew this distro. The installer itself stays apt-only for
    # now (Tiers 1-4 of the install pipeline call apt-get directly), so on
    # non-apt distros the operator installs prereqs by hand and re-runs.
    _b_err ""
    _b_err "  This installer's package step is apt-based (Debian/Ubuntu)."
    _b_err "  Install the missing packages manually for your distro, then re-run."
    _b_err ""
    if have dnf; then
      _b_err "  Detected dnf — try:"
      _b_err "    sudo dnf install -y ${NEEDED[*]/python3-venv/python3}"
    elif have pacman; then
      _b_err "  Detected pacman — try:"
      _b_err "    sudo pacman -S --needed ${NEEDED[*]/python3-venv/python}"
    elif have zypper; then
      _b_err "  Detected zypper — try:"
      _b_err "    sudo zypper install -y ${NEEDED[*]/python3-venv/python3}"
    elif have apk; then
      _b_err "  Detected apk — try:"
      _b_err "    sudo apk add ${NEEDED[*]/python3-venv/python3}"
    else
      _b_err "  Required: python3 (>= 3.10), python3-venv, git, jq, curl, rsync."
    fi
    _b_err ""
    _b_err "  Then re-run this installer. Modes 1-4/6 still require an apt-based"
    _b_err "  host because the per-component installers call apt-get directly for"
    _b_err "  build/runtime dependencies. Mode 5 (agent) works on any Linux."
    _b_die "missing prerequisites — install them and re-run"
  fi
fi
_b_ok "prerequisites present"

# Python version check
if ! python3 - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
then
  _b_die "python3 >= 3.10 is required (have $(python3 --version 2>&1))"
fi
_b_ok "python3 >= 3.10 ($(python3 --version 2>&1))"

# ── Self-update trampoline ─────────────────────────────────────────────────
# Compare _INSTALL_SH_REVISION (local vs upstream). When upstream is STRICTLY
# GREATER, fetch a fresh copy into /tmp and re-exec with the EXACT original
# argv. Skips when:
#   - --no-self-update was passed
#   - LLMSYS_SELF_UPDATE_DONE is set (we already re-exec'd)
#   - the operator invoked --update or --uninstall: both already fetch
#     fresh code from GitHub later in the script, so self-updating here
#     would be a redundant fetch on the hot path
#   - the upstream copy can't be fetched (warn + continue with local)
#
# A revision integer is used instead of byte equality so a locally-modified
# install.sh (or a copy carrying unpushed fixes) is never silently
# downgraded to whatever's on origin/main.
# Meta modes (7=update, 8=uninstall, 9=quit) skip the self-update trampoline:
# the operation fetches fresh code (or makes no changes) on its own. MODE is
# empty on interactive runs at this point, so only an explicit --mode 7/8/9
# matches here.
_MODE_IS_META=0
case "$MODE" in 7|8|9) _MODE_IS_META=1 ;; esac
if [[ "${LLMSYS_SELF_UPDATE_DONE:-0}" != "1" \
   && "$NO_SELF_UPDATE" != "1" \
   && "$UPDATE" != "1" \
   && "$UNINSTALL" != "1" \
   && "$_MODE_IS_META" != "1" ]]; then
  _self="${BASH_SOURCE[0]:-$0}"
  if [[ "$_self" != /* ]]; then
    _self="$(cd "$(dirname "$_self")" 2>/dev/null && pwd || echo "")/$(basename "$_self")"
  fi
  if [[ -f "$_self" ]]; then
    _upstream_tmp="/tmp/llm-systems-install.upstream.$$.sh"
    if _b_fetch_from_github "tools/installer/install.sh" "$_upstream_tmp"; then
      # Missing revision marker parses as 0 — old copies still get updated.
      # `|| true` so a missing marker doesn't fail the pipeline (pipefail +
      # set -e would otherwise abort the whole assignment).
      _local_rev="$(grep -E '^_INSTALL_SH_REVISION=[0-9]+' "$_self"          2>/dev/null | head -1 | sed 's/.*=//' || true)"
      _upstream_rev="$(grep -E '^_INSTALL_SH_REVISION=[0-9]+' "$_upstream_tmp" 2>/dev/null | head -1 | sed 's/.*=//' || true)"
      _local_rev="${_local_rev:-0}"
      _upstream_rev="${_upstream_rev:-0}"
      if (( _upstream_rev > _local_rev )); then
        _b_log "newer install.sh upstream (rev $_local_rev → $_upstream_rev) — re-executing (pass --no-self-update to skip)"
        chmod +x "$_upstream_tmp"
        export LLMSYS_SELF_UPDATE_DONE=1
        export LLMSYS_ORIGINAL_SELF="$_self"
        # The re-exec'd child becomes /tmp/.../upstream.$$.sh and would
        # otherwise leak (success path never deletes it). Have the child
        # self-delete on EXIT by leaving a marker; the trampoline in the
        # new copy honors LLMSYS_SELF_UPDATE_DONE=1 and reads $_self at
        # script start, so a quick trap suffices.
        exec bash -c '
          trap "rm -f \"\$0\"" EXIT
          exec bash "$0" "$@"
        ' "$_upstream_tmp" "${_ORIG_ARGV[@]+"${_ORIG_ARGV[@]}"}"
      fi
      rm -f "$_upstream_tmp"
    else
      _b_warn "self-update check skipped — couldn't reach GitHub (curl failed; running local rev $(grep -E '^_INSTALL_SH_REVISION=[0-9]+' "$_self" 2>/dev/null | head -1 | sed 's/.*=//' || echo "?"))"
    fi
    unset _upstream_tmp _local_rev _upstream_rev
  fi
  unset _self
fi

# ── Mode selection ─────────────────────────────────────────────────────────
# Resolve MODE (flag or interactive menu) BEFORE the update/uninstall
# short-circuits so menu modes 7/8/9 can map onto the same code paths:
#   7 → --update     (re-uses the update short-circuit below)
#   8 → --uninstall  (re-uses the uninstall short-circuit below)
#   9 → quit, no changes
# This keeps ONE code path per operation and stops 8/9 from cloning,
# provisioning the run-as user, and deploying the tree before bailing.
# The --update / --uninstall flags skip the menu entirely (MODE stays "").
if [[ "$UPDATE" != "1" && "$UNINSTALL" != "1" ]]; then
  case "$MODE" in
    "")
      if [[ ! -t 0 ]]; then
        _b_die "No --mode N given and stdin is not a TTY for interactive prompt."
      fi
      cat <<'MENU'


──────────────────────────  Deployment mode  ────────────────────────────

  Select the deployment option:

    1)  Full system          manager + alarm engine + agent + InfluxDB
    2)  Manager + alarm      manager + alarm engine (existing InfluxDB)
    3)  Manager only         Flask manager + dashboard
    4)  Alarm engine only    standalone FastAPI alarm engine
    5)  Agent only           Linux + macOS host agent
    6)  InfluxDB only        InfluxDB v2 + scoped tokens (DB host)
    7)  Update installed     detect, diff, backup, sync-only-changed, restart
    8)  Uninstall            remove all services and files, with confirmation prompts
    9)  Quit                 exit with no changes

─────────────────────────────────────────────────────────────────────────

MENU
      read -rp "  Mode [1-9]: " MODE
      echo
      ;;
  esac

  case "$MODE" in
    1|2|3|4|5|6) _b_ok "Selected mode $MODE" ;;
    7) _b_ok "Selected mode 7 — update installed system"; UPDATE=1 ;;
    8) _b_ok "Selected mode 8 — uninstall"; UNINSTALL=1 ;;
    9) _b_log "Quit selected — exiting with no changes."; exit 0 ;;
    *) _b_die "Invalid mode '$MODE' — must be 1-9." ;;
  esac
fi

# ── Update short-circuit ───────────────────────────────────────────────────
# --update always runs the LATEST update.sh from a fresh clone, not the
# deployed copy. Reason: an update operation is precisely "use the new
# code", and the deployed helper on this host may be older than what
# we're updating to. Bootstrapping from /opt/.../update.sh has bitten
# us with stale bugs that were already fixed upstream.
#
# Sequence:
#   1. Stage /tmp/llm-systems-manager-install with a self-contained git
#      clone/pull. We deliberately do NOT source the on-disk lib-common here:
#      a self-updated install.sh can be newer than the deployed lib-common, so
#      calling its helpers risks a renamed/missing function. update.sh (exec'd
#      below) sources the FRESH lib-common from the clone itself.
#   2. exec the staged update.sh, passing REPO_SRC so update.sh skips
#      its own re-clone step.
if [[ "$UPDATE" == "1" ]]; then
  LLMSYS_CLONE_TMP="${LLMSYS_CLONE_TMP:-/tmp/llm-systems-manager-install}"
  command -v git >/dev/null 2>&1 \
    || { _b_err "git is required to fetch the repo. Install git and re-run."; exit 1; }
  if [[ -d "$LLMSYS_CLONE_TMP/.git" ]]; then
    git -C "$LLMSYS_CLONE_TMP" pull --ff-only \
      || _b_log "git pull failed; using existing checkout"
  elif [[ -e "$LLMSYS_CLONE_TMP" ]]; then
    _b_die "$LLMSYS_CLONE_TMP exists and isn't a git repo — remove it first"
  else
    git clone "https://github.com/${LLMSYS_REPO_SLUG:-llmsyscore/llm-systems-manager}.git" "$LLMSYS_CLONE_TMP"
  fi
  UPDATE_HELPER="$LLMSYS_CLONE_TMP/tools/installer/update.sh"
  [[ -f "$UPDATE_HELPER" ]] \
    || _b_die "update.sh missing in staging clone at $UPDATE_HELPER — repo HEAD too old?"
  _b_ok "running update helper: $UPDATE_HELPER"
  REPO_SRC="$LLMSYS_CLONE_TMP" exec bash "$UPDATE_HELPER" \
    "${FORWARD_ARGS[@]:+${FORWARD_ARGS[@]}}"
fi

# ── Uninstall short-circuit ────────────────────────────────────────────────
# Uninstall is purely local: no clone, no auth. If a sibling helper isn't
# on disk (operator dropped install.sh standalone on an agent box), fetch the
# latest uninstall.sh from GitHub into /tmp and exec it. The helper is fully
# self-contained — auto-detects what's installed and removes only what the
# operator confirms.
if [[ "$UNINSTALL" == "1" ]]; then
  UNINSTALL_HELPER="$(_b_find_helper uninstall.sh || true)"
  if [[ -z "$UNINSTALL_HELPER" ]]; then
    _b_log "no local uninstall.sh — fetching from GitHub"
    UNINSTALL_HELPER="/tmp/llm-systems-uninstall.$$.sh"
    if ! _b_fetch_from_github "tools/installer/uninstall.sh" "$UNINSTALL_HELPER"; then
      _b_die "failed to fetch uninstall.sh from GitHub — check network/curl, or drop uninstall.sh next to install.sh"
    fi
    chmod +x "$UNINSTALL_HELPER"
    _b_ok "fetched $UNINSTALL_HELPER"
    _UNINSTALL_FETCHED=true
  else
    _UNINSTALL_FETCHED=false
  fi
  # Don't exec — we want to clean up our own trace files afterwards.
  bash "$UNINSTALL_HELPER"
  _rc=$?
  $_UNINSTALL_FETCHED && rm -f "$UNINSTALL_HELPER" 2>/dev/null
  # Remove this install.sh too. Linux+macOS keep the open file handle
  # valid until the script finishes, so unlinking the running file is
  # safe. We only delete copies the operator clearly dropped for
  # bootstrap — anything under /tmp/ or inside the cached clone — and
  # leave a checked-out repo (e.g. ~/code/llm-systems-manager/...) alone.
  _self="${BASH_SOURCE[0]:-$0}"
  # Resolve to an absolute path so the /tmp/* glob actually matches when
  # the operator invoked us with a relative path like ./install.sh.
  if [[ "$_self" != /* ]]; then
    _self="$(cd "$(dirname "$_self")" 2>/dev/null && pwd || echo "")/$(basename "$_self")"
  fi
  case "$_self" in
    /tmp/*|*/llm-systems-manager-install/*)
      rm -f "$_self" 2>/dev/null || true
      ;;
  esac
  # If the self-update trampoline re-exec'd us, the operator's ORIGINAL
  # install.sh is at a different path — remove that too so no trace is
  # left. Same temp-only guard so a repo copy is preserved.
  if [[ -n "${LLMSYS_ORIGINAL_SELF:-}" && "$LLMSYS_ORIGINAL_SELF" != "$_self" ]]; then
    case "$LLMSYS_ORIGINAL_SELF" in
      /tmp/*|*/llm-systems-manager-install/*)
        rm -f "$LLMSYS_ORIGINAL_SELF" 2>/dev/null || true
        ;;
    esac
  fi
  exit "$_rc"
fi

# ── Source acquisition: public HTTPS git clone ──────────────────────────────
# The repo is public, so a plain `git clone` over HTTPS fetches it with no auth.
if ! command -v git >/dev/null 2>&1; then
  _b_err "Need git to fetch the repo. Install git and re-run."
  exit 1
fi

# Modes 1-4 + 6 need Linux. Mode 5 supports macOS too.
case "$MODE" in
  5) ;;
  *) [[ "$OS" == "linux" ]] || _b_die "Mode $MODE is Linux-only (manager/alarm engine/InfluxDB don't run on $OS). Use mode 5 for the agent." ;;
esac

printf '\n%s── Source ─────────────────────────────────────────────────────────────%s\n' "$_B_BLU" "$_B_RST"

# ── Clone repo (or use the local checkout if available) ─────────────────────
LLMSYS_CLONE_TMP="${LLMSYS_CLONE_TMP:-/tmp/llm-systems-manager-install}"
REPO_SLUG="llmsyscore/llm-systems-manager"

# Only treat $THIS_DIR as a valid checkout if it's a real git working tree.
# Running install.sh from the deployed snapshot (/opt/llm-systems-manager,
# which has no .git after deploy_into_install_dir's rsync excludes it) would
# otherwise reuse stale installer scripts from the previous run.
# THIS_DIR == <root>/tools/installer, so the repo root is two levels up.
# Only treat it as a valid checkout if .git is there AND a sibling helper
# (lib-common.sh) resolves — guards against stray "tools/installer" trees.
_REPO_ROOT_FROM_THIS=""
if [[ -n "$THIS_DIR" ]]; then
  _REPO_ROOT_FROM_THIS="$(cd "$THIS_DIR/../.." 2>/dev/null && pwd || true)"
fi
# _REPO_IS_STAGING flags whether REPO_SRC is a temp clone we created (safe
# to remove after the install completes) or the operator's own working tree
# (must be preserved).
if [[ -n "$_REPO_ROOT_FROM_THIS" \
      && -e "$_REPO_ROOT_FROM_THIS/.git" \
      && -f "$_REPO_ROOT_FROM_THIS/tools/installer/lib-common.sh" ]]; then
  REPO_SRC="$_REPO_ROOT_FROM_THIS"
  _REPO_IS_STAGING=false
  _b_ok "using local checkout: $REPO_SRC"
else
  if [[ -d "$LLMSYS_CLONE_TMP/.git" ]]; then
    git -C "$LLMSYS_CLONE_TMP" pull --ff-only >/dev/null 2>&1 || true
    _b_ok "refreshed cached clone at $LLMSYS_CLONE_TMP"
  elif [[ -e "$LLMSYS_CLONE_TMP" ]]; then
    _b_die "$LLMSYS_CLONE_TMP exists and isn't a git repo — remove or rename it first"
  else
    git clone -q "https://github.com/$REPO_SLUG.git" "$LLMSYS_CLONE_TMP" >/dev/null 2>&1 \
      || _b_die "git clone https://github.com/$REPO_SLUG.git failed"
    _b_ok "cloned $REPO_SLUG → $LLMSYS_CLONE_TMP"
  fi
  REPO_SRC="$LLMSYS_CLONE_TMP"
  _REPO_IS_STAGING=true
fi

# Source the real helpers now
# shellcheck source=tools/installer/lib-common.sh
. "$REPO_SRC/tools/installer/lib-common.sh"

# Apply --user override now that LLMSYS_RUN_USER has its lib-common default.
# Export so every sub-installer (install-manager.sh, install-alarm-engine.sh,
# install-config-bootstrap.sh) and ensure_runas_user pick it up.
if [[ -n "$RUN_USER_OVERRIDE" ]]; then
  if [[ ! "$RUN_USER_OVERRIDE" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    die "--user '$RUN_USER_OVERRIDE' is not a valid POSIX username"
  fi
  LLMSYS_RUN_USER="$RUN_USER_OVERRIDE"
  # If the user already exists, honor its real primary group; otherwise
  # default to a same-named group (useradd --system --create-home creates
  # one when no -g is passed).
  if id "$LLMSYS_RUN_USER" >/dev/null 2>&1; then
    LLMSYS_RUN_GROUP="$(id -gn "$LLMSYS_RUN_USER" 2>/dev/null || echo "$LLMSYS_RUN_USER")"
  else
    LLMSYS_RUN_GROUP="$LLMSYS_RUN_USER"
  fi
  export LLMSYS_RUN_USER LLMSYS_RUN_GROUP
  ok "run-as user override: $LLMSYS_RUN_USER (group: $LLMSYS_RUN_GROUP)"
fi

# ── Mode 5 (agent only) short-circuits before user/deploy steps ─────────────
case "$MODE" in
5)
  banner "Mode 5 — Agent installer"
  AGENT_INSTALL="$REPO_SRC/agent/install/install.sh"
  [[ -f "$AGENT_INSTALL" ]] || die "Agent installer missing: $AGENT_INSTALL"
  chmod +x "$AGENT_INSTALL" || true
  # Run as a child (not exec) so we can clean up the staging clone after
  # the agent installer returns. Without this, exec would replace this
  # process and /tmp/llm-systems-manager-install would be left behind on
  # every agent install.
  if (( ${#FORWARD_ARGS[@]} > 0 )); then
    bash "$AGENT_INSTALL" "${FORWARD_ARGS[@]}"
  else
    bash "$AGENT_INSTALL"
  fi
  _rc=$?
  # $SUDO isn't set up yet (detect_sudo is in the Modes 1-4 branch),
  # so fall back to literal `sudo` if a non-root rm leaves files behind.
  if $_REPO_IS_STAGING && [[ -d "$LLMSYS_CLONE_TMP" ]]; then
    if rm -rf "$LLMSYS_CLONE_TMP" 2>/dev/null && [[ ! -e "$LLMSYS_CLONE_TMP" ]]; then
      :
    elif command -v sudo >/dev/null 2>&1; then
      sudo rm -rf "$LLMSYS_CLONE_TMP" 2>/dev/null || true
    fi
    if (( _rc == 0 )) && [[ ! -e "$LLMSYS_CLONE_TMP" ]]; then
      ok "removed staging clone $LLMSYS_CLONE_TMP"
    fi
  fi
  exit "$_rc"
  ;;
esac

# ── Modes 1-4: ensure llmsys user, then deploy ──────────────────────────────
detect_os
require_linux
detect_sudo

banner "Provisioning $LLMSYS_RUN_USER user"
ensure_runas_user "$LLMSYS_RUN_USER"

case "$MODE" in
  6)
    # Mode 6 (InfluxDB only) doesn't need any of the app code on disk —
    # InfluxDB runs from the system package and install-influxdb.sh prints
    # the tokens / admin password to stdout for the operator to record.
    # Nothing lands under $LLMSYS_INSTALL_DIR.
    ok "Mode 6 — no app code deployed; install-influxdb.sh will print credentials to stdout"
    ;;
  *)
    banner "Deploying repo → $LLMSYS_INSTALL_DIR"
    # Trim the other service's tree on solo installs — manager-only
    # doesn't need the alarm engine's code on disk, and vice versa.
    DEPLOY_EXCLUDES=()
    case "$MODE" in
      3) DEPLOY_EXCLUDES=("llm-systems-alarm-engine/") ;;
      4) DEPLOY_EXCLUDES=("llm-systems-manager/") ;;
    esac
    deploy_into_install_dir "$REPO_SRC" "$LLMSYS_INSTALL_DIR" "${DEPLOY_EXCLUDES[@]+"${DEPLOY_EXCLUDES[@]}"}"
    ;;
esac

# Make all installer scripts executable in the deployed tree
$SUDO chmod +x \
  "$LLMSYS_INSTALL_DIR/tools/installer/"*.sh \
  "$LLMSYS_INSTALL_DIR/agent/install/install.sh" 2>/dev/null || true

# ── Run mode-specific sub-installers ────────────────────────────────────────
HEALTH_TARGETS=()

# LLMSYS_INSTALL_MODE is read by install-config-bootstrap.sh so it knows
# which prompts and substitutions apply for this deployment shape (e.g.
# Mode 3 must ask for a remote alarm-engine URL; Mode 4 for a remote
# manager URL; Modes 1-2 default both to localhost).
export LLMSYS_INSTALL_MODE="$MODE"

# Tokens generated by install-influxdb.sh are written to this transient
# /tmp file and consumed by install-config-bootstrap.sh; install.sh
# unlinks it on exit so secrets never persist outside the live TOML.
export LLMSYS_INFLUXDB_TOKEN_FILE="$(mktemp -t llmsys-influxdb-tokens.XXXXXX.env)"
chmod 0600 "$LLMSYS_INFLUXDB_TOKEN_FILE"
# Per-run apt-update sentinel; shared across the sub-installer processes so
# the package index refreshes once per run, not once per sub-script.
export LLMSYS_APT_STAMP="$(mktemp -u -t llmsys-apt-updated.XXXXXX)"
trap 'rm -f "$LLMSYS_INFLUXDB_TOKEN_FILE" "$LLMSYS_APT_STAMP"' EXIT

case "$MODE" in
  1)
    banner "Mode 1 — Full system"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-influxdb.sh"
    if [[ ! -s "$LLMSYS_INFLUXDB_TOKEN_FILE" ]]; then
      die "install-influxdb.sh completed but did not write tokens to \$LLMSYS_INFLUXDB_TOKEN_FILE — scroll up for the [ERR] line and fix before continuing"
    fi
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-alarm-engine.sh"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-manager.sh"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-config-bootstrap.sh"
    HEALTH_TARGETS=(
      "Manager       http://127.0.0.1:5000/        200"
      "Alarm-engine  http://127.0.0.1:8081/       200"
      "InfluxDB      http://127.0.0.1:8086/health  200"
    )
    ;;
  2)
    banner "Mode 2 — Manager + alarm engine"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-alarm-engine.sh"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-manager.sh"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/resolve-influxdb.sh"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-config-bootstrap.sh"
    HEALTH_TARGETS=(
      "Manager       http://127.0.0.1:5000/        200"
      "Alarm-engine  http://127.0.0.1:8081/       200"
    )
    ;;
  3)
    banner "Mode 3 — Manager only"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-manager.sh"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-config-bootstrap.sh"
    HEALTH_TARGETS=(
      "Manager       http://127.0.0.1:5000/        200"
    )
    ;;
  4)
    banner "Mode 4 — Alarm engine only"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-alarm-engine.sh"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/resolve-influxdb.sh"
    bash "$LLMSYS_INSTALL_DIR/tools/installer/install-config-bootstrap.sh"
    HEALTH_TARGETS=(
      "Alarm-engine  http://127.0.0.1:8081/       200"
    )
    ;;
  6)
    # InfluxDB-only host: no manager toml is needed locally — the operator
    # copies the generated tokens to the alarm-engine host's config. We
    # deliberately do NOT call install-config-bootstrap.sh here, and we
    # run install-influxdb.sh straight from the staging clone since
    # there's no deployed copy under $LLMSYS_INSTALL_DIR/tools/.
    banner "Mode 6 — InfluxDB only"
    bash "$REPO_SRC/tools/installer/install-influxdb.sh"
    HEALTH_TARGETS=(
      "InfluxDB      http://127.0.0.1:8086/health  200"
    )
    ;;
  *)  _b_die "Invalid mode '$MODE' reached" ;;
esac

# ── Optional service start ──────────────────────────────────────────────────
# Mode 1's agent install runs AFTER these services are up — the agent's
# installer probes the manager URL during registration and bails if the
# manager isn't reachable yet.
case "$MODE" in
  1) SERVICES_TO_START=(llm-systems-alarm-engine llm-systems-manager) ;;
  2) SERVICES_TO_START=(llm-systems-alarm-engine llm-systems-manager) ;;
  3) SERVICES_TO_START=(llm-systems-manager) ;;
  4) SERVICES_TO_START=(llm-systems-alarm-engine) ;;
  *) SERVICES_TO_START=() ;;   # Modes 5 and 6 don't manage app units here
esac

STARTED_SERVICES=0

# InfluxDB is started by its own package postinst — verify rather than start.
case "$MODE" in
  1|6)
    banner "Verifying InfluxDB"
    if $SUDO systemctl is-active --quiet influxdb; then
      ok "influxdb active"
    else
      err "influxdb is not running — starting it"
      $SUDO systemctl start influxdb || true
      sleep 2
      if $SUDO systemctl is-active --quiet influxdb; then
        ok "influxdb started"
      else
        err "influxdb failed to start — recent log:"
        $SUDO journalctl -u influxdb -n 30 --no-pager 2>&1 | sed 's/^/    /'
      fi
    fi
    ;;
esac

banner "Start services now?"
if (( ${#SERVICES_TO_START[@]} > 0 )); then
  echo "  The following systemd units were installed and enabled but not started:"
  echo "    ${SERVICES_TO_START[*]}"
  echo
  START_NOW=""
  if [[ -t 0 ]]; then
    read -rp "  Start them now? [Y/n] " START_NOW
  else
    log "non-interactive — skipping start prompt; run systemctl manually."
  fi
  case "$(printf '%s' "${START_NOW:-y}" | tr '[:upper:]' '[:lower:]')" in
    n|no)
      log "leaving services stopped per operator choice"
      ;;
    *)
      for svc in "${SERVICES_TO_START[@]}"; do
        log "starting $svc"
        $SUDO systemctl start "$svc" || true
        # systemctl start can return 0 even when the unit crashed during
        # startup (the start command succeeded; the process died moments
        # later). Wait a beat, then verify the unit is actually active.
        sleep 2
        if $SUDO systemctl is-active --quiet "$svc"; then
          ok "$svc active"
        else
          err "$svc is not running — recent log:"
          $SUDO journalctl -u "$svc" -n 30 --no-pager 2>&1 | sed 's/^/    /'
          err "(full log: journalctl -u $svc -n 200 --no-pager)"
        fi
      done
      STARTED_SERVICES=1
      ;;
  esac
fi

# ── Local-agent MANAGER_URL ─────────────────────────────────────────────────
# The agent's --manager-url governs TWO things that look unrelated but aren't:
#   1. What address it dials the manager on.
#   2. What address it advertises back as its own (_advertise_host opens a UDP
#      socket "toward" MANAGER_URL and returns the kernel-chosen source IP).
# When --manager-url is loopback, the agent registers as 127.0.0.1 — which is
# correct for its outbound but breaks every browser-served URL pointing at
# the agent (SSE log tail, PTY, direct-to-agent calls) when the browser isn't
# on this host. Detect this host's primary LAN IP and prefer that; fall back
# to 127.0.0.1 only when no LAN address is configured.
_AGENT_DETECTED_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$_AGENT_DETECTED_IP" ]] && _AGENT_DETECTED_IP="127.0.0.1"
_LOCAL_MGR_URL="http://${_AGENT_DETECTED_IP}:5000"

# ── Mode 1: install the local agent now that the manager is up ─────────────
case "$MODE" in
1)
  banner "Mode 1 — Local agent"
  if [[ "$STARTED_SERVICES" != "1" ]]; then
    warn "Skipping agent install — manager wasn't started, agent registration would fail."
    warn "After you start the manager, run: bash $LLMSYS_INSTALL_DIR/agent/install/install.sh --manager-url $_LOCAL_MGR_URL"
  else
    log "waiting for the manager to accept connections on http://127.0.0.1:5000"
    MANAGER_UP=0
    for _ in {1..30}; do
      if [[ "$(probe_url http://127.0.0.1:5000/api/agents)" =~ ^(200|401|403)$ ]]; then
        MANAGER_UP=1
        break
      fi
      sleep 1
    done
    if [[ "$MANAGER_UP" != "1" ]]; then
      warn "manager never came up on http://127.0.0.1:5000 — skipping agent install"
      warn "Diagnose with: sudo journalctl -u llm-systems-manager -n 100 --no-pager"
      warn "After fixing, run: sudo bash $LLMSYS_INSTALL_DIR/agent/install/install.sh --manager-url $_LOCAL_MGR_URL"
      MANAGER_UP_AGENT_INSTALL=0
    else
      MANAGER_UP_AGENT_INSTALL=1
    fi
    # --no-start so the agent installer enables the unit but does NOT
    # call `systemctl ... --now`. install.sh's own "Start the agent now?"
    # prompt below is the authoritative start gate; otherwise the
    # service was racing the prompt and starting before the operator
    # answered.
    AGENT_CMD=(bash "$LLMSYS_INSTALL_DIR/agent/install/install.sh"
               --manager-url "$_LOCAL_MGR_URL"
               --no-start)
    # Mode 1 installs everything on this host; if --user was passed to the
    # outer installer, the local agent runs as the same user. Operators
    # wanting a different agent user can still override via FORWARD_ARGS.
    [[ -n "$RUN_USER_OVERRIDE" ]] && AGENT_CMD+=(--user "$RUN_USER_OVERRIDE")
    if (( ${#FORWARD_ARGS[@]} > 0 )); then
      AGENT_CMD+=("${FORWARD_ARGS[@]}")
    fi
    if [[ "$MANAGER_UP_AGENT_INSTALL" == "1" ]] && "${AGENT_CMD[@]}"; then
      ok "agent installed"
      if [[ -t 0 ]]; then
        read -rp "  Start the agent now? [Y/n] " START_AGENT
      else
        START_AGENT="y"
      fi
      case "$(printf '%s' "${START_AGENT:-y}" | tr '[:upper:]' '[:lower:]')" in
        n|no) log "leaving llm-systems-agent stopped" ;;
        *)
          if $SUDO systemctl start llm-systems-agent 2>/dev/null; then
            ok "llm-systems-agent started — approve it from the Admin tab in the dashboard"
          else
            warn "agent unit not yet present or failed to start — check 'journalctl -u llm-systems-agent -n 50'"
          fi
          ;;
      esac
    elif [[ "$MANAGER_UP_AGENT_INSTALL" == "1" ]]; then
      warn "Agent install failed — re-run manually with: bash $LLMSYS_INSTALL_DIR/agent/install/install.sh --manager-url $_LOCAL_MGR_URL"
    fi
  fi
  ;;
esac

# ── Mode 3 only: tell the operator what to do with the AE TLS cert ────────
# On a manager-only (split) install the manager auto-issues ae-tls.{crt,key}
# at its first startup into its own data dir ($INSTALL_DIR/data — the runtime
# DATA_DIR), since the AE data dir doesn't exist on this host. Those files
# then need to land on the AE host. Until that copy happens, the AE fails to
# serve HTTPS and falls back to plain HTTP, which breaks the manager's https
# probe of the AE and forces agents to push metrics over plain HTTP (no TLS
# protection on the ingest token they're carrying).
case "$MODE" in
3)
  _MGR_AE_SRC="$LLMSYS_INSTALL_DIR/data"
  _AE_DEST="$LLMSYS_INSTALL_DIR/llm-systems-alarm-engine/data"
  _AE_HOST=""
  _live_toml="$LLMSYS_INSTALL_DIR/config/llm-systems.toml"
  if $SUDO test -f "$_live_toml"; then
    _AE_URL_RAW="$(read_toml_key alarm_engine_url "$_live_toml")"
    if [[ -n "$_AE_URL_RAW" ]]; then
      _AE_HOST="$(printf '%s' "$_AE_URL_RAW" | sed -E 's|^https?://([^:/]+).*|\1|')"
    fi
  fi
  _AE_HOST_FOR_HINT="${_AE_HOST:-<ae-host>}"
  banner "AE TLS cert — copy to the alarm-engine host"
  echo "  The manager issues the alarm-engine's TLS cert on its first startup."
  echo "  Once started, both files live on THIS (manager) host at:"
  echo "    $_MGR_AE_SRC/ae-tls.crt"
  echo "    $_MGR_AE_SRC/ae-tls.key"
  echo
  echo "  Copy them to the alarm-engine host ($_AE_HOST_FOR_HINT) at:"
  echo "    $_AE_DEST/"
  echo "  before the alarm engine can serve HTTPS. Until they do:"
  echo "    - AE falls back to plain HTTP on port 8081"
  echo "    - manager's https probe of the AE fails (admin tab → red dot)"
  echo "    - agents push metrics + their ingest_token over plain HTTP"
  echo
  echo "  Copy the files using whatever channel you have to that host"
  echo "  (scp from your workstation, ansible, manual paste — the files are"
  echo "  short PEM blobs, key is 0600 / cert is 0644). Then on $_AE_HOST_FOR_HINT:"
  echo "    sudo chown $LLMSYS_RUN_USER:$LLMSYS_RUN_GROUP $_AE_DEST/ae-tls.{crt,key}"
  echo "    sudo systemctl restart llm-systems-alarm-engine"
  echo
  ;;
esac

# ── Agent install offer (Modes 2/3/4/6 — Mode 1 already handled it) ───────
# Installing a local agent lets this host be monitored by the dashboard:
# CPU/RAM/disk/network metrics in real time, llama-server / LM Studio
# probes when those run here, log-watch + process watchlist alerts, and
# system-health cards visible to anyone with admin access. Without an
# agent the manager has no data for this host's row on the dashboard.
# Offer a local agent only for modes that don't already place one (Mode 1
# installs its own above; Mode 5 IS the agent installer) AND only when no
# agent is installed on this host yet.
_OFFER_AGENT=0
case "$MODE" in
  1|5) ;;
  *) [[ ! -f /etc/systemd/system/llm-systems-agent.service \
        && ! -d /opt/llm-systems-agent ]] && _OFFER_AGENT=1 ;;
esac
if (( _OFFER_AGENT )); then
  banner "Install a local agent on this host?"
  echo "  An agent reports this host's CPU/RAM/disk/network/GPU + any"
  echo "  llama-server / LM Studio activity back to the manager so it"
  echo "  shows up on the dashboard, in alarm rules, and in alerts."
  echo "  Without one, this host has no data on the dashboard."
  echo
  # Resolve the manager URL we'd point the agent at.
  AGENT_MGR_URL=""
  case "$MODE" in
    2|3) AGENT_MGR_URL="$_LOCAL_MGR_URL" ;;
    4|6)
      # No local manager — try to read manager_url from the live config
      # (Mode 4 sets [alarm_engine].manager_url; Mode 6 ships no TOML).
      # The TOML is mode 0600 owned by llmsys, so read it via $SUDO.
      _live_toml="$LLMSYS_INSTALL_DIR/config/llm-systems.toml"
      if $SUDO test -f "$_live_toml"; then
        AGENT_MGR_URL="$(read_toml_key manager_url "$_live_toml")"
      fi
      ;;
  esac
  PROCEED_AGENT=0
  if [[ -t 0 ]]; then
    # Default Yes — agents are how the host gets onto the dashboard. Skipping
    # is the unusual choice, so the empty-Enter reply now matches the common case.
    read -rp "  Install agent now? [Y/n] " _ans
    case "$(printf '%s' "${_ans:-y}" | tr '[:upper:]' '[:lower:]')" in
      n|no) PROCEED_AGENT=0 ;;
      *)    PROCEED_AGENT=1 ;;
    esac
  else
    log "non-TTY — skipping agent install offer"
  fi
  if (( PROCEED_AGENT )); then
    while :; do
      if [[ -z "$AGENT_MGR_URL" ]]; then
        read -rp "  Manager URL (e.g. http://192.0.2.10:5000): " _raw
      else
        read -rp "  Manager URL [$AGENT_MGR_URL]: " _raw
        _raw="${_raw:-$AGENT_MGR_URL}"
      fi
      _candidate="$(sanitize_url "$_raw" 5000)"
      _host="$(url_host "$_candidate")"
      _bad=0
      [[ -z "$_candidate" || -z "$_host" ]] && _bad=1
      [[ "$_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[xX]$ ]] && _bad=1
      if (( _bad )); then
        warn "  Manager URL is required (e.g. http://192.0.2.10:5000) — try again."
        AGENT_MGR_URL=""
        continue
      fi
      AGENT_MGR_URL="$_candidate"
      break
    done
    # Resolve the AE URL the same way: if [manager].alarm_engine_url is
    # in the live config (split install where Mode 3 wrote a remote URL
    # there), pass it explicitly so the agent doesn't fall back to
    # <manager-host>:8081 — which would point at this host's missing
    # AE on a split install.
    AGENT_AE_URL=""
    _live_toml="$LLMSYS_INSTALL_DIR/config/llm-systems.toml"
    if $SUDO test -f "$_live_toml" 2>/dev/null; then
      AGENT_AE_URL="$(read_toml_key alarm_engine_url "$_live_toml")"
    fi
    # Prefer the deployed installer (so re-runs work after Mode 1/2/3/4),
    # fall back to the staging clone (Mode 6 doesn't deploy agent/).
    AGENT_INSTALL_BIN="$LLMSYS_INSTALL_DIR/agent/install/install.sh"
    [[ -f "$AGENT_INSTALL_BIN" ]] || AGENT_INSTALL_BIN="$REPO_SRC/agent/install/install.sh"
    AGENT_CMD=(bash "$AGENT_INSTALL_BIN" --manager-url "$AGENT_MGR_URL")
    [[ -n "$AGENT_AE_URL" ]] && AGENT_CMD+=(--alarm-engine-url "$AGENT_AE_URL")
    # Same as Mode 1: when --user was given to the outer installer, the local
    # agent (if installed) runs under it too.
    [[ -n "$RUN_USER_OVERRIDE" ]] && AGENT_CMD+=(--user "$RUN_USER_OVERRIDE")
    # Mode 6 is a DB-only host — no inference runtime, no GPU/sensors
    # probing. Force system_only so the agent installer skips provider
    # auto-detect AND the Host hardware collectors prompts. Also enable the
    # InfluxDB disk probe: this host runs InfluxDB without a co-located AE,
    # so the agent is the only thing that can report InfluxDB disk usage
    # (system_only skips the auto-detect that would otherwise enable it).
    case "$MODE" in
      6) AGENT_CMD+=(--role system_only --enable-monitor-influxdb-disk) ;;
    esac
    if [[ -f "$AGENT_INSTALL_BIN" ]]; then
      chmod +x "$AGENT_INSTALL_BIN" 2>/dev/null || true
      log "running agent installer — manager=$AGENT_MGR_URL${AGENT_AE_URL:+ alarm-engine=$AGENT_AE_URL}"
      "${AGENT_CMD[@]}" || warn "agent installer returned non-zero — review its output above"
    else
      err "agent installer missing at $AGENT_INSTALL_BIN — rerun mode 5 or"
      err "  ${AGENT_CMD[*]}"
    fi
  else
    log "skipped agent install — to install later:"
    log "  sudo bash $LLMSYS_INSTALL_DIR/agent/install/install.sh --manager-url ${AGENT_MGR_URL:-http://<manager-host>:5000}"
  fi
fi

# ── Final summary ───────────────────────────────────────────────────────────
banner "Install complete — health checks"
echo "  Probing now (services may still be warming up):"
PASS=0; FAIL=0
# Map "Manager" / "Alarm engine" / "InfluxDB" labels back to systemd unit
# names so we can inline a journal dump on probe failure — operators were
# seeing 'HTTP 000' with no context for what to do next.
_unit_for_label() {
  case "$1" in
    Manager*)   printf 'llm-systems-manager' ;;
    Alarm*)     printf 'llm-systems-alarm-engine' ;;
    InfluxDB*)  printf 'influxdb' ;;
    *) printf '' ;;
  esac
}
for entry in "${HEALTH_TARGETS[@]}"; do
  # shellcheck disable=SC2086
  read -r label url expect <<<"$entry"
  unit="$(_unit_for_label "$label")"
  if report_service_health "$label" "$url" "$expect" "$unit"; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1))
    if [[ -n "$unit" ]]; then
      err "  $unit recent log:"
      $SUDO journalctl -u "$unit" -n 20 --no-pager 2>&1 | sed 's/^/      /'
    fi
  fi
done
echo

# ── Cleanup: remove staging clone + the launcher script in /tmp ─────────────
# Only purge when the install completed cleanly (every probed service
# returned its expected status). On any failure, keep the staging tree
# around so the operator can poke at it without re-cloning.
if (( FAIL == 0 )); then
  banner "Cleanup"
  if [[ -d "$LLMSYS_CLONE_TMP" ]]; then
    $SUDO rm -rf "$LLMSYS_CLONE_TMP"
    ok "removed staging clone $LLMSYS_CLONE_TMP"
  fi
  # The install.sh launcher itself may have been curled into /tmp by the
  # one-liner — wipe it too so /tmp ends up free of repo artifacts.
  for stray in /tmp/install.sh /tmp/llm-systems-manager-install.sh; do
    if [[ -f "$stray" ]]; then
      $SUDO rm -f "$stray"
      ok "removed $stray"
    fi
  done
  # Belt-and-suspenders: scrub any dev artifacts that might still be under
  # the deployed tree (rsync excludes should already have stripped these,
  # but a stale earlier install could have leaked them). Includes the agent
  # install dir for Mode 1 / Mode 5 paths.
  for tree in "$LLMSYS_INSTALL_DIR" /opt/llm-systems-agent; do
    [[ -d "$tree" ]] || continue
    for art in .git .github .claude .gitignore .gitattributes; do
      if $SUDO test -e "$tree/$art"; then
        $SUDO rm -rf "$tree/$art"
        ok "scrubbed $tree/$art"
      fi
    done
  done
else
  warn "skipping /tmp cleanup — $FAIL service(s) failed health checks; staging at $LLMSYS_CLONE_TMP is preserved"
fi

# ── Closing banner ──────────────────────────────────────────────────────────
PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$PRIMARY_IP" ]] && PRIMARY_IP="127.0.0.1"
DASHBOARD_URL="http://${PRIMARY_IP}:5000/"
ALARM_URL="http://${PRIMARY_IP}:8081/"

if (( FAIL == 0 )); then
  STATUS_TITLE="Installation complete"
  STATUS_LINE="All ${PASS} service(s) passed health checks."
else
  STATUS_TITLE="Installation finished with errors"
  STATUS_LINE="${FAIL} of $((PASS + FAIL)) service(s) failed health checks — see above."
fi

cat <<BANNER


  ╭───────────────────────────────────────────────────────────────────╮
  │                                                                   │
  │              L L M   S Y S T E M S   M A N A G E R                │
  │                                                                   │
  │$(printf '%*s%s%*s' $(( (67 - ${#STATUS_TITLE}) / 2 )) '' "$STATUS_TITLE" $(( (67 - ${#STATUS_TITLE} + 1) / 2 )) '')│
  │                                                                   │
  ╰───────────────────────────────────────────────────────────────────╯

         Mode  ·  ${MODE}
         Host  ·  $(hostname)  (${PRIMARY_IP})
         Result ·  ${STATUS_LINE}

BANNER

echo "  Next steps"
echo "  ─────────────────────────────────────────────────────────────────"
case "$MODE" in
  1)
    echo "    1. Open the dashboard:       ${DASHBOARD_URL}"
    echo "    2. Approve this host's agent from the Admin tab → Agents"
    echo "    3. Review configuration:     ${LLMSYS_INSTALL_DIR}/config/llm-systems.toml"
    echo "       (SMTP creds, InfluxDB tokens; file is mode 0600)"
    ;;
  2)
    echo "    1. Open the dashboard:       ${DASHBOARD_URL}"
    echo "    2. Review configuration:     ${LLMSYS_INSTALL_DIR}/config/llm-systems.toml"
    echo "       (set real IPs, SMTP creds, InfluxDB tokens; file is mode 0600)"
    ;;
  3)
    echo "    1. Open the dashboard:       ${DASHBOARD_URL}"
    echo "    2. Confirm the remote alarm-engine URL set in [manager].alarm_engine_url"
    echo "    3. Review configuration:     ${LLMSYS_INSTALL_DIR}/config/llm-systems.toml"
    ;;
  4)
    # Pull the manager host out of [alarm_engine].manager_url so the scp
    # example points the operator at the right machine.
    _MGR_HOST_FOR_HINT=""
    _live_toml_hint="$LLMSYS_INSTALL_DIR/config/llm-systems.toml"
    if $SUDO test -f "$_live_toml_hint" 2>/dev/null; then
      _MGR_URL_RAW="$(read_toml_key manager_url "$_live_toml_hint")"
      _MGR_HOST_FOR_HINT="$(printf '%s' "$_MGR_URL_RAW" | sed -E 's|^https?://([^:/]+).*|\1|')"
    fi
    _MGR_HOST_FOR_HINT="${_MGR_HOST_FOR_HINT:-<manager-host>}"
    echo "    1. Review configuration:     ${LLMSYS_INSTALL_DIR}/config/llm-systems.toml"
    echo "       (SMTP creds, InfluxDB host + tokens; file is mode 0600)"
    echo "    2. Uncomment [alarm_engine].ingest_token in this file, then copy"
    echo "       the SAME value into [alarm_engine].ingest_token on the manager"
    echo "       host. Restart BOTH services afterwards:"
    echo "         here:        sudo systemctl restart llm-systems-alarm-engine"
    echo "         on manager:  sudo systemctl restart llm-systems-manager"
    echo "    3. Copy the AE TLS cert + key FROM THE MANAGER HOST to this host."
    echo "       The manager issues them on its first startup; they live on the"
    echo "       manager at:"
    echo "         ${LLMSYS_INSTALL_DIR}/data/ae-tls.{crt,key}"
    echo "       Drop them here at (use whatever channel works for you —"
    echo "       manual paste, scp from your workstation, ansible, etc.):"
    echo "         ${LLMSYS_INSTALL_DIR}/llm-systems-alarm-engine/data/"
    echo "       Then on this host:"
    echo "         sudo chown ${LLMSYS_RUN_USER}:${LLMSYS_RUN_GROUP} ${LLMSYS_INSTALL_DIR}/llm-systems-alarm-engine/data/ae-tls.{crt,key}"
    echo "         sudo systemctl restart llm-systems-alarm-engine"
    ;;
  6)
    echo "    1. Probe InfluxDB:           curl -fsS http://${PRIMARY_IP}:8086/health"
    echo "    2. Verify you recorded the credentials printed above —"
    echo "       they are NOT saved anywhere on this host."
    echo "    3. On the alarm-engine host, set [influxdb] host/port and"
    echo "       [influxdb.tokens].metrics / .metrics_rollup / .admin from"
    echo "       the values you recorded."
    ;;
esac
echo
echo "  Operations"
echo "  ─────────────────────────────────────────────────────────────────"
if (( ${#SERVICES_TO_START[@]} > 0 )); then
  echo "    Status:     sudo systemctl status ${SERVICES_TO_START[*]}"
  echo "    Logs:       sudo journalctl -u ${SERVICES_TO_START[0]} -f"
fi
echo "    Uninstall:  sudo bash ${LLMSYS_INSTALL_DIR}/tools/installer/install.sh --uninstall"
echo
echo "  Documentation"
echo "  ─────────────────────────────────────────────────────────────────"
echo "    Repository:       github.com/llmsyscore/llm-systems-manager"
echo

if (( FAIL == 0 )); then
  ok "Installation succeeded."
else
  err "Installation completed with ${FAIL} failed health check(s)."
  exit 1
fi
