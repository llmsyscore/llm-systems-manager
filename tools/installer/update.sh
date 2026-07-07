#!/usr/bin/env bash
# =============================================================================
# tools/installer/update.sh — in-place update for an existing installation
#
# Reached via:
#   sudo bash install.sh --update
#   sudo bash install.sh                     # menu → option 7
#   sudo bash tools/installer/update.sh      # direct
#
# What it does:
#   1. Detects which components are installed (manager / alarm-engine /
#      InfluxDB / local agent) by checking systemd units + install tree.
#   2. Fetches the latest agent/ + manager/ + alarm-engine/ + installer
#      from upstream into /tmp/llm-systems-manager-install.
#   3. For each detected component, lists what would change (rsync dry-run
#      with --checksum so a re-deploy of identical files is a no-op),
#      shows the diff, prompts the operator, backs up critical files,
#      then syncs.
#   4. Re-runs `pip install -r requirements.txt` for any component whose
#      requirements.txt actually changed.
#   5. Compares /etc/systemd/system/*.service against the new repo copies;
#      on mismatch, diffs, backs up the running unit, and copies the new
#      one only after explicit confirmation.
#   6. Optionally md5sum-verifies every copied file.
#   7. Asks before restarting each affected service.
#   8. Probes /health on each affected service and confirms it self-reports
#      the version we just deployed. The full smoke tests in tools/*.sh
#      are NOT run here — they're scoped at the UI/DOM/CDP layer (logged-in
#      browser session required) and are for end-of-feature validation,
#      not for upgrades. Run them by hand after the upgrade if you want
#      them.
#
# What it does NOT do:
#   - Never touches `config/llm-systems.toml` or
#     `agent/agent_config.yaml`. Those are rsync-excluded in
#     deploy_into_install_dir AND re-excluded here. If the example file
#     (`config/llm-systems.toml.example`) gained new keys, the operator
#     is told to diff manually — we don't auto-merge into a live secrets
#     file from this entry point.
#   - Never auto-installs InfluxDB or upgrades it. InfluxDB updates are
#     out of band (`apt upgrade influxdb2`).
#   - Never touches the per-agent install on remote hosts — those use
#     the admin tab's "Update" button which proxies SSE to the agent's
#     own self-update endpoint. We DO update the local agent on this
#     host (agents on the manager/AE host are common) using the same
#     install.sh --update flow under the hood.
# =============================================================================
set -euo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-common.sh
. "$THIS_DIR/lib-common.sh"
detect_os
require_linux
detect_sudo

LLMSYS_INSTALL_DIR="${LLMSYS_INSTALL_DIR:-/opt/llm-systems-manager}"
AGENT_INSTALL_DIR="${AGENT_INSTALL_DIR:-/opt/llm-systems-agent}"
STAMP="$(date +%Y%m%d-%H%M%S)"
# BACKUP_ROOT is finalized after HAVE_* detection so DB-only / agent-only
# hosts don't end up creating an empty $LLMSYS_INSTALL_DIR/backups/ tree.
# Track whether the operator overrode it via --backup-dir; if not, we
# retarget below.
BACKUP_ROOT="${BACKUP_ROOT:-}"
BACKUP_ROOT_DEFAULT=1
[[ -n "$BACKUP_ROOT" ]] && BACKUP_ROOT_DEFAULT=0
DRY_RUN=0
ASSUME_YES=0
SKIP_VENV=0
SKIP_RESTART=0
SKIP_TESTS=0
PARANOID_VERIFY=0    # rsync's --checksum already validates; only md5-verify on demand
COMPONENT_FILTER=""

usage() {
  cat <<'HELP'
Update an existing LLM Systems Manager install in place.

Usage:
  sudo bash update.sh [options]

Options:
  --dry-run            Show what would change; copy nothing, restart nothing.
  --yes, -y            Auto-confirm prompts (still backs up before any change).
  --only <names>       Comma-separated subset: manager,alarm-engine,agent,installer
                       (default: every detected component).
  --skip-venv          Don't refresh venvs even if requirements.txt changed.
  --skip-restart       Don't restart services after the update.
  --skip-tests         Don't probe /health at the end (upgrade ends after
                       the last service restart). The full repo smoke tests
                       are never run from here — invoke them by hand from
                       tools/llm-systems-{manager,alarm-engine}_smoke_test.sh
                       when you want them.
  --paranoid           Re-md5sum every copied file post-sync (rsync already
                       checksum-validates; default off).
  --backup-dir PATH    Override backup root (default: ${LLMSYS_INSTALL_DIR}/backups/update-<ts>).
  -h, --help           Show this message.

Examples:
  sudo bash update.sh --dry-run
  sudo bash update.sh --only manager,alarm-engine
  sudo bash update.sh -y --skip-restart        # apply, but I'll restart later
HELP
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)       DRY_RUN=1; shift ;;
    -y|--yes)        ASSUME_YES=1; shift ;;
    --only)          COMPONENT_FILTER="${2:-}"; shift 2 ;;
    --only=*)        COMPONENT_FILTER="${1#*=}"; shift ;;
    --skip-venv)     SKIP_VENV=1; shift ;;
    --skip-restart)  SKIP_RESTART=1; shift ;;
    --skip-tests)    SKIP_TESTS=1; shift ;;
    --paranoid)      PARANOID_VERIFY=1; shift ;;
    --skip-verify)   shift ;;   # accepted for back-compat; now the default
    --backup-dir)    BACKUP_ROOT="${2:-}"; BACKUP_ROOT_DEFAULT=0; shift 2 ;;
    --backup-dir=*)  BACKUP_ROOT="${1#*=}"; BACKUP_ROOT_DEFAULT=0; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) die "unknown flag: $1 (see --help)" ;;
  esac
done

# Honor the existing install's run-as user rather than the lib-common default
# (`llmsys`). Without this, an `--update` on a host installed with
# `install.sh --user foo` would chown every refreshed file back to llmsys
# and break the running services. Detect from the deployed manager systemd
# unit first, fall back to AE unit, then to the install dir owner.
_detected_user=""
for _unit in /etc/systemd/system/llm-systems-manager.service \
             /etc/systemd/system/llm-systems-alarm-engine.service; do
  if [[ -f "$_unit" ]]; then
    _detected_user="$(awk -F= '/^User=/{print $2; exit}' "$_unit" 2>/dev/null || true)"
    [[ -n "$_detected_user" ]] && break
  fi
done
if [[ -z "$_detected_user" && -d "$LLMSYS_INSTALL_DIR" ]]; then
  _detected_user="$(stat -c %U "$LLMSYS_INSTALL_DIR" 2>/dev/null || true)"
fi
if [[ -n "$_detected_user" && "$_detected_user" != "$LLMSYS_RUN_USER" ]]; then
  log "detected existing run-as user '$_detected_user' (lib-common default was '$LLMSYS_RUN_USER')"
  LLMSYS_RUN_USER="$_detected_user"
  LLMSYS_RUN_GROUP="$(id -gn "$LLMSYS_RUN_USER" 2>/dev/null || echo "$LLMSYS_RUN_USER")"
  export LLMSYS_RUN_USER LLMSYS_RUN_GROUP
fi
unset _detected_user _unit

# BACKUP_ROOT itself is created lazily by _ensure_backup_root on the
# first backup_path() call so dry-runs and no-op updates don't leave
# behind an empty directory tree.
_backup_root_made=0
_ensure_backup_root() {
  (( _backup_root_made )) && return 0
  mkdir -p "$BACKUP_ROOT" 2>/dev/null || $SUDO mkdir -p "$BACKUP_ROOT"
  _backup_root_made=1
}

# ── Helpers ────────────────────────────────────────────────────────────────

# _extract_version <python_file>
#   Pulls VERSION or __version__ out of a Python source file (manager, AE,
#   agent all use one of those two constants). Empty echo on miss — caller
#   formats it as "<unknown>" so the operator sees a literal mismatch.
_extract_version() {
  local f="$1"
  [[ -f "$f" ]] || { echo ""; return; }
  # `grep || true` keeps a no-match (exit 1) or SIGPIPE from `head` from
  # failing the pipeline under `set -o pipefail` and aborting the caller.
  { $SUDO grep -E '^(VERSION|__version__)[[:space:]]*=' "$f" 2>/dev/null || true; } \
    | head -1 | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/'
}

# _vercmp <a> <b> → echoes -1 / 0 / 1 (a vs b). Format: vYYYY.MM.DD-N.
_vercmp() {
  local a="${1#v}" b="${2#v}"
  local ad="${a%-*}" an="${a##*-}"
  local bd="${b%-*}" bn="${b##*-}"
  # Absent/non-numeric build suffix (no "-N") → 0, so a dotted token never
  # reaches (( )) and misclassifies the compare as equal.
  [[ "$an" =~ ^[0-9]+$ ]] || an=0
  [[ "$bn" =~ ^[0-9]+$ ]] || bn=0
  if [[ "$ad" < "$bd" ]]; then echo -1; return; fi
  if [[ "$ad" > "$bd" ]]; then echo  1; return; fi
  if (( an < bn )); then echo -1; return; fi
  if (( an > bn )); then echo  1; return; fi
  echo 0
}

DOWNGRADE_DETECTED=0

# _show_version_delta <label> <old> <new>  → "X → Y" or "X (no change)"
# Flips DOWNGRADE_DETECTED when new < old; caller prompts at end of block.
_show_version_delta() {
  local label="$1" old="${2:-<unknown>}" new="${3:-<unknown>}"
  if [[ "$old" == "$new" ]]; then
    ok "  $label: version $old (no change)"
  else
    log "  $label: $old  →  $new"
    if [[ "$old" != "<unknown>" && "$new" != "<unknown>" ]]; then
      if (( $(_vercmp "$new" "$old") < 0 )); then
        warn "    ⚠ DOWNGRADE — $new is older than installed $old"
        DOWNGRADE_DETECTED=1
      fi
    fi
  fi
}

# prompt_yn <question> [default=y|n]
#   Returns 0 for yes, 1 for no. Auto-yes when --yes is set.
prompt_yn() {
  local q="$1" def="${2:-n}" ans
  if (( ASSUME_YES )); then
    log "$q [auto-yes]"
    return 0
  fi
  local hint
  hint=$([[ "$def" == "y" ]] && echo "[Y/n]" || echo "[y/N]")
  if [[ ! -t 0 ]]; then
    log "$q $hint [non-TTY → default $def]"
    [[ "$def" == "y" ]]
    return $?
  fi
  read -rp "  $q $hint " ans
  ans="$(printf '%s' "${ans:-$def}" | tr '[:upper:]' '[:lower:]')"
  [[ "$ans" == "y" || "$ans" == "yes" ]]
}

# backup_path <src> — copies <src> to $BACKUP_ROOT mirroring its full path.
# Echoes the backup path. Idempotent: skips when src missing.
backup_path() {
  local src="$1"
  if ! $SUDO test -e "$src"; then return 0; fi
  _ensure_backup_root
  local rel="${src#/}"
  local dest="$BACKUP_ROOT/$rel"
  $SUDO mkdir -p "$(dirname "$dest")"
  $SUDO cp -a "$src" "$dest"
  echo "$dest"
}

# md5_of <path> — emits md5 hash of a file (sudo-safe). Empty on missing file.
md5_of() {
  local p="$1"
  $SUDO test -f "$p" || return 0
  $SUDO md5sum "$p" 2>/dev/null | awk '{print $1}'
}

# Shared rsync exclude set for files_changed + sync_dir (kept in one place so
# the dry-run preview and the real copy never diverge).
RSYNC_EXCLUDES=(
  --exclude='.git' --exclude='.git/'
  --exclude='.gitignore' --exclude='.gitattributes'
  --exclude='.github' --exclude='.github/'
  --exclude='.claude' --exclude='.claude/'
  --exclude='venv/' --exclude='__pycache__/'
  --exclude='data/' --exclude='backups/'
  --exclude='plans/'
  --exclude='tests/' --exclude='pytest.ini'
  --exclude='requirements-dev.txt'
  --exclude='.pytest_cache/'
  --exclude='node_modules/' --exclude='test/'
)

# files_changed <src_dir> <dest_dir> [rsync_extra_args...]
#   Returns a newline-separated list of files that WOULD change
#   (rsync --dry-run --checksum --itemize-changes). Empty when up-to-date.
#   Uses --checksum so identical content with a touched mtime doesn't
#   show as "changed".
files_changed() {
  local src="$1" dest="$2"; shift 2
  $SUDO rsync -a --checksum --dry-run --itemize-changes \
      "${RSYNC_EXCLUDES[@]}" \
      "$@" "$src/" "$dest/" 2>/dev/null \
    | awk '/^[<>ch]/ && $2 !~ /\/$/ {print $2}'
}

# backup_changed_files <dest_dir> <changed_rel_list>
#   Backs up every existing file in <dest_dir> matching the newline-separated
#   list of relative paths (the same list `files_changed` produces). Without
#   this, sync_dir would overwrite files in place and the operator would have
#   no recovery copy — exactly the failure mode reported in the field.
backup_changed_files() {
  local dest="$1" changed="$2"
  [[ -z "$changed" ]] && return 0
  while IFS= read -r rel; do
    [[ -z "$rel" ]] && continue
    $SUDO test -e "$dest/$rel" || continue
    backup_path "$dest/$rel" >/dev/null
  done <<< "$changed"
}

# sync_dir <src> <dest> [rsync_extra_args...]
#   Honors --checksum so unchanged files are skipped. Owner reset to
#   $LLMSYS_RUN_USER:$LLMSYS_RUN_GROUP after copy. Returns 0 on success.
sync_dir() {
  # files_changed already ran a --checksum dry-run for the prompt — the
  # actual copy can rely on rsync's default size+mtime, avoiding a second
  # full-tree md5 pass.
  local src="$1" dest="$2"; shift 2
  $SUDO mkdir -p "$dest"
  $SUDO rsync -a --itemize-changes \
      "${RSYNC_EXCLUDES[@]}" \
      "$@" "$src/" "$dest/" \
    | awk '/^[<>ch]/ && $2 !~ /\/$/ {print "    " $2}'
  $SUDO chown -R "$LLMSYS_RUN_USER:$LLMSYS_RUN_GROUP" "$dest"
}

# verify_md5_pairs <src> <dest> <files...>  (relative paths under both)
#   Re-md5sums each file on both sides post-copy and warns on mismatch.
verify_md5_pairs() {
  local src="$1" dest="$2"; shift 2
  local fails=0
  for rel in "$@"; do
    [[ -z "$rel" ]] && continue
    local s d a b
    s="$src/$rel"; d="$dest/$rel"
    [[ -f "$s" && -f "$d" ]] || continue
    a="$(md5_of "$s")"; b="$(md5_of "$d")"
    if [[ "$a" != "$b" ]]; then
      err "md5 mismatch: $rel  (src=$a dst=$b)"
      fails=$((fails+1))
    fi
  done
  if (( fails > 0 )); then
    err "$fails file(s) failed md5 verification — investigate before restarting services"
    return 1
  fi
  return 0
}

# render_unit_template (src, out) lives in lib-common.sh and is shared with the
# per-component installers.

# unit_replace <unit_name> <template_path>
#   Renders the .service.example into a temp file, diffs against the
#   running unit at /etc/systemd/system/<unit>, prompts, backs up, copies.
#   Returns 0 if changed, 1 if unchanged or refused.
unit_replace() {
  local unit="$1" tpl="$2"
  local dest="/etc/systemd/system/$unit"
  if [[ ! -f "$tpl" ]]; then
    warn "  no new unit template at $tpl — skipping $unit"
    return 1
  fi
  local rendered; rendered="$(mktemp)"
  render_unit_template "$tpl" "$rendered"
  if [[ ! -f "$dest" ]]; then
    log "  $unit not installed yet — copying"
    if (( DRY_RUN )); then echo "    [dry-run] would install $unit"; rm -f "$rendered"; return 0; fi
    $SUDO install -m 0644 "$rendered" "$dest"
    rm -f "$rendered"
    NEED_DAEMON_RELOAD=1
    return 0
  fi
  if cmp -s "$rendered" "$dest" 2>/dev/null; then
    log "  $unit already up-to-date"
    rm -f "$rendered"
    return 1
  fi
  # Count changed lines without dumping the diff itself — operators just
  # need to know "yes, it changed" + how much. Backup is unconditional
  # below, so the actual diff is recoverable from the .bak file.
  local _ndiff
  _ndiff="$(diff "$dest" "$rendered" 2>/dev/null | grep -cE '^[<>]' || true)"
  warn "  $unit differs from the new template ($_ndiff line(s) changed); backup will be saved"
  if ! prompt_yn "Replace running unit $unit?" "n"; then
    warn "  $unit left untouched per operator choice"
    rm -f "$rendered"
    return 1
  fi
  if (( DRY_RUN )); then echo "    [dry-run] would replace $unit"; rm -f "$rendered"; return 0; fi
  local bak; bak="$(backup_path "$dest")"
  [[ -n "$bak" ]] && ok "  backed up running unit → $bak"
  $SUDO install -m 0644 "$rendered" "$dest"
  rm -f "$rendered"
  NEED_DAEMON_RELOAD=1
  ok "  $unit replaced (daemon-reload deferred until end-of-run)"
  return 0
}

# refresh_venv <component_dir>  (e.g. .../llm-systems-manager)
#   Re-runs pip install -r requirements.txt only if requirements.txt changed
#   relative to what's already in the venv. Cheap heuristic: md5 the file
#   into the venv after install, compare on next run.
refresh_venv() {
  # Split locals — bash evaluates `local a="$x" b="$a/foo"` against the
  # OUTER scope's $a, which under `set -u` errors with "a: unbound".
  local cdir="$1"
  # Per-component requirements lives in different places (manager →
  # backend/requirements.txt, AE → top-level). Caller passes the relative
  # path; the previous "$cdir/requirements.txt"-only default silently
  # no-op'd manager venv refreshes — that's how websockets ended up
  # missing on disk while the manager code that imports it shipped.
  local reqs_rel="${2:-requirements.txt}"
  local reqs="$cdir/$reqs_rel"
  local venv="$cdir/venv"
  if (( SKIP_VENV )); then log "  --skip-venv set; not touching venv"; return 0; fi
  if [[ ! -f "$reqs" ]]; then log "  no $reqs_rel under $cdir — skipping venv"; return 0; fi
  if [[ ! -x "$venv/bin/pip" ]]; then
    warn "  venv missing at $venv — recreate? (this drops installed packages)"
    if ! prompt_yn "Recreate venv at $venv?" "n"; then return 0; fi
    if (( DRY_RUN )); then echo "    [dry-run] would recreate $venv"; return 0; fi
    as_run_user python3 -m venv "$venv"
  fi
  # Stamp file in the venv records the md5 of requirements.txt at last install.
  local stamp="$venv/.requirements.md5" cur prev=""
  cur="$(md5_of "$reqs")"
  [[ -f "$stamp" ]] && prev="$(cat "$stamp" 2>/dev/null || true)"
  if [[ "$cur" == "$prev" ]]; then log "  requirements.txt unchanged — venv left alone"; return 0; fi
  log "  requirements.txt changed (was=${prev:-<unknown>}, now=$cur)"
  if (( DRY_RUN )); then echo "    [dry-run] would pip install -r $reqs"; return 0; fi
  # --quiet (-q) drops one verbosity level: pip stops echoing the
  # "Requirement already satisfied: …" line per pinned dep (~40 lines of
  # noise per run on a fully-installed venv) but WARNING + ERROR output
  # and tracebacks still go to stderr. Errors aren't hidden by --quiet —
  # they were hidden by update_component's `|| true`, which PR #41 already
  # removed. Keep --quiet to keep the output readable.
  if ! pip_filter as_run_user "$venv/bin/pip" \
        install --quiet --no-cache-dir --upgrade pip; then
    err "  pip self-upgrade FAILED in $venv — see output above"
    return 1
  fi
  if ! pip_filter as_run_user "$venv/bin/pip" \
        install --quiet --no-cache-dir -r "$reqs"; then
    err "  pip install -r $reqs FAILED — see output above"
    err "  the venv at $venv is now in a half-installed state and"
    err "  the running service WILL ModuleNotFoundError on missing deps"
    return 1
  fi
  echo "$cur" | as_run_user tee "$stamp" >/dev/null
  ok "  venv refreshed against new requirements.txt"
}

# verify_venv_imports <venv_dir> <requirements_file>
#   Confirm every top-level import name in requirements.txt actually loads
#   inside the venv. pip install can exit 0 while leaving the venv broken
#   when a previous run was interrupted half-way through and the failed
#   wheel left a partial install behind — `pip list` shows the package as
#   installed even though its metadata is missing. The only reliable check
#   is to actually import each module the service is going to need.
#
#   Mapping requirements line → import name is heuristic but covers the
#   manager/AE set: strip version markers, lowercase, and apply the known
#   PEP 503 normalization (- → _) plus a few explicit exceptions.
verify_venv_imports() {
  local venv="$1" reqs="$2"
  local py="$venv/bin/python3"
  [[ -x "$py" ]] || { warn "  no python3 at $py — skipping import verify"; return 0; }
  [[ -f "$reqs" ]] || return 0
  log "  verifying imports against $(basename "$reqs")"
  local fail=0
  local distname importname
  # Read requirements one line at a time. Skip blanks, comments, and any
  # line that doesn't look like a package spec (-e, --index-url, etc.).
  while IFS= read -r line; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    [[ "$line" == -* ]] && continue
    # Drop everything from the first version specifier or extras bracket.
    distname="${line%%[<>=!~;[ ]*}"
    [[ -z "$distname" ]] && continue
    # PyPI distname → import name. Hand-list the few that diverge in our
    # current deps; default = lowercase + s/-/_/.
    case "$(printf '%s' "$distname" | tr '[:upper:]' '[:lower:]')" in
      pydantic-settings) importname="pydantic_settings" ;;
      pydantic)          importname="pydantic" ;;
      pyyaml)            importname="yaml" ;;
      pyjwt)             importname="jwt" ;;
      pillow)            importname="PIL" ;;
      protobuf)          importname="google.protobuf" ;;
      # opentelemetry-X-Y is a NAMESPACE package — import path keeps dots
      # ('opentelemetry.X.Y'), unlike PEP 503's "- → _" default. Without
      # this branch every opentelemetry-* dep registers as a false-positive
      # import failure even when the package is correctly installed.
      opentelemetry-*)   importname="$(printf '%s' "$distname" | tr '[:upper:]-' '[:lower:].')" ;;
      *)                 importname="$(printf '%s' "$distname" | tr '[:upper:]-' '[:lower:]_')" ;;
    esac
    if ! as_run_user "$py" -c "import $importname" 2>/dev/null; then
      err "    import $importname  FAILED  (from requirement: $line)"
      fail=$((fail+1))
    fi
  done < "$reqs"
  if (( fail > 0 )); then
    err "  $fail import(s) failed — the service WILL crash on startup"
    err "  diagnose with: sudo -u $LLMSYS_RUN_USER $py -c 'import <name>'"
    return 1
  fi
  ok "  all imports load cleanly"
  return 0
}

# component_wanted <name> — honors --only filter (default: all wanted)
component_wanted() {
  [[ -z "$COMPONENT_FILTER" ]] && return 0
  printf '%s' ",$COMPONENT_FILTER," | grep -q ",$1,"
}

# is_installed_<component> — return 0/1. Reads world-readable paths only,
# so detection works in a dry-run / non-TTY context without sudo elevation.
is_installed_manager()       { [[ -f /etc/systemd/system/llm-systems-manager.service \
                                || -d "$LLMSYS_INSTALL_DIR/llm-systems-manager" ]]; }
is_installed_alarm_engine()  { [[ -f /etc/systemd/system/llm-systems-alarm-engine.service \
                                || -d "$LLMSYS_INSTALL_DIR/llm-systems-alarm-engine" ]]; }
is_installed_influxdb()      { have influx \
                              || systemctl list-unit-files --type=service 2>/dev/null \
                                 | grep -q '^influxdb\.service'; }
is_installed_agent()         { [[ -f /etc/systemd/system/llm-systems-agent.service \
                                || -d "$AGENT_INSTALL_DIR" ]]; }

# ── Discovery ──────────────────────────────────────────────────────────────
banner "Discovering installed components"

HAVE_MANAGER=false; HAVE_AE=false; HAVE_INFLUX=false; HAVE_AGENT=false
is_installed_manager       && HAVE_MANAGER=true
is_installed_alarm_engine  && HAVE_AE=true
is_installed_influxdb      && HAVE_INFLUX=true
is_installed_agent         && HAVE_AGENT=true

$HAVE_MANAGER && ok "manager       installed"  || log "manager       not installed"
$HAVE_AE      && ok "alarm-engine  installed"  || log "alarm-engine  not installed"
$HAVE_INFLUX  && ok "influxdb      installed"  || log "influxdb      not installed"
$HAVE_AGENT   && ok "local agent   installed"  || log "local agent   not installed"

if ! $HAVE_MANAGER && ! $HAVE_AE && ! $HAVE_AGENT; then
  die "nothing to update — run install.sh to install components first"
fi

# Pick the default backup root now that HAVE_* are known. On DB-only or
# agent-only hosts the only thing the updater touches is the agent, so
# anchor backups inside AGENT_INSTALL_DIR rather than carving out an
# /opt/llm-systems-manager/backups/ tree on a host that otherwise has
# no manager install at all.
if (( BACKUP_ROOT_DEFAULT )); then
  if ! $HAVE_MANAGER && ! $HAVE_AE && $HAVE_AGENT; then
    BACKUP_ROOT="$AGENT_INSTALL_DIR/backups/update-$STAMP"
  else
    BACKUP_ROOT="$LLMSYS_INSTALL_DIR/backups/update-$STAMP"
  fi
fi

# ── Source freshness ──────────────────────────────────────────────────────
banner "Staging fresh upstream code"
# REPO_SRC is honored from the environment so install.sh --update can
# pre-stage the clone and skip a redundant re-clone here. When unset,
# we clone/pull from upstream ourselves (the standalone-invocation path).
REPO_SRC="${REPO_SRC:-}"
if [[ -z "$REPO_SRC" ]]; then
  REPO_SRC="${LLMSYS_CLONE_TMP:-/tmp/llm-systems-manager-install}"
  require_git
  clone_repo "$REPO_SRC"
fi
ok "source tree at $REPO_SRC"

# ── Per-component plan ─────────────────────────────────────────────────────
RESTART_UNITS=()
UPDATED_COMPONENTS=()
FAILED_UNITS=()
VERIFY_FAILURES=0
NEED_DAEMON_RELOAD=0

# True when $1's component update failed (unit must not be [re]started).
_unit_failed() {
  local u
  for u in ${FAILED_UNITS[@]+"${FAILED_UNITS[@]}"}; do
    [[ "$u" == "$1" ]] && return 0
  done
  return 1
}

banner "Version comparison"
MGR_V_OLD=""; MGR_V_NEW=""
AE_V_OLD="";  AE_V_NEW=""
AGT_V_OLD=""; AGT_V_NEW=""
if $HAVE_MANAGER; then
  MGR_V_OLD="$(_extract_version "$LLMSYS_INSTALL_DIR/llm-systems-manager/backend/llm-systems-manager.py")"
  MGR_V_NEW="$(_extract_version "$REPO_SRC/llm-systems-manager/backend/llm-systems-manager.py")"
  _show_version_delta "Manager      " "$MGR_V_OLD" "$MGR_V_NEW"
fi
if $HAVE_AE; then
  AE_V_OLD="$(_extract_version "$LLMSYS_INSTALL_DIR/llm-systems-alarm-engine/backend/alarm_engine.py")"
  AE_V_NEW="$(_extract_version "$REPO_SRC/llm-systems-alarm-engine/backend/alarm_engine.py")"
  _show_version_delta "Alarm engine " "$AE_V_OLD" "$AE_V_NEW"
fi
if $HAVE_AGENT; then
  AGT_V_OLD="$(_extract_version "$AGENT_INSTALL_DIR/llm-systems-agent.py")"
  AGT_V_NEW="$(_extract_version "$REPO_SRC/agent/llm-systems-agent.py")"
  _show_version_delta "Local agent  " "$AGT_V_OLD" "$AGT_V_NEW"
fi

if (( DOWNGRADE_DETECTED )); then
  warn "One or more components would be DOWNGRADED. Likely cause: REPO_SRC checked out to an older commit."
  if ! prompt_yn "Proceed with downgrade?" "n"; then
    log "Aborting per operator response."
    exit 1
  fi
fi

# ── Optional pre-flight: stop our services before file sync ───────────────
# rsync over .py and pip rewrites of site-packages while a service is
# running is racy — Python caches source on first import so late edits
# show up as half-applied state, and pip rewriting the venv under a live
# import is the source of intermittent ImportErrors. Opt-in because the
# operator may have inference running. Covers manager, AE, and the agent
# (the agent has the same rsync / pip race as manager + AE — install/
# rewrites buffered_metric_client.py + llm-systems-agent.py while the
# running process still has them mapped). InfluxDB is left alone, it's
# orthogonal to the sync.
STOPPED_UNITS=()
# EXIT handler: remove the TOML-merge temp file, and restart pre-flight-stopped
# services if the run aborts before the restart phase (so a mid-update failure
# can't leave manager/AE/agent down).
_restart_phase_reached=0
_TOML_MERGE_TMP=""
_on_exit() {
  local rc=$?
  [[ -n "$_TOML_MERGE_TMP" ]] && rm -f "$_TOML_MERGE_TMP" 2>/dev/null
  (( rc == 0 )) && return 0
  (( DRY_RUN )) && return 0
  (( _restart_phase_reached )) && return 0
  (( ${#STOPPED_UNITS[@]} == 0 )) && return 0
  warn "update aborted (exit $rc) after services were stopped — restarting them:"
  local u
  for u in "${STOPPED_UNITS[@]}"; do
    if _unit_failed "$u"; then
      warn "  NOT starting $u — its component update failed (broken venv/code); fix it, then: sudo systemctl start $u"
      continue
    fi
    warn "  systemctl start $u"
    $SUDO systemctl start "$u" || warn "    $u failed to start — check: journalctl -u $u -n 50"
  done
}
trap _on_exit EXIT
if (( ! DRY_RUN )) && ( $HAVE_MANAGER || $HAVE_AE || $HAVE_AGENT ); then
  banner "Pre-flight: stop services before sync?"
  log "Stopping the services before file/venv sync avoids race conditions"
  log "(half-applied source reloads, pip rewrites under a live import)."
  log "They will be queued for restart at the end with the unified prompt."
  if prompt_yn "Stop services now?" "y"; then
    $HAVE_MANAGER && {
      log "stopping llm-systems-manager.service"
      $SUDO systemctl stop llm-systems-manager.service \
        && { STOPPED_UNITS+=("llm-systems-manager.service"); RESTART_UNITS+=("llm-systems-manager.service"); } \
        || warn "  stop returned non-zero"
    }
    $HAVE_AE && {
      log "stopping llm-systems-alarm-engine.service"
      $SUDO systemctl stop llm-systems-alarm-engine.service \
        && { STOPPED_UNITS+=("llm-systems-alarm-engine.service"); RESTART_UNITS+=("llm-systems-alarm-engine.service"); } \
        || warn "  stop returned non-zero"
    }
    $HAVE_AGENT && {
      log "stopping llm-systems-agent.service"
      $SUDO systemctl stop llm-systems-agent.service \
        && { STOPPED_UNITS+=("llm-systems-agent.service"); RESTART_UNITS+=("llm-systems-agent.service"); } \
        || warn "  stop returned non-zero"
    }
  else
    log "leaving services running; sync will happen in-place"
  fi
fi

update_component() {
  local label="$1" src_subdir="$2" dest_subdir="$3" unit="$4" component_key="$5"
  # reqs_rel="" (passed explicitly) means "this component is a file-sync
  # only, not a Python package with its own venv" — used for "Agent deploy
  # source" which mirrors agent/ into the manager-host repo copy but where
  # the running agent's actual venv lives at $AGENT_INSTALL_DIR/venv. Default
  # of "requirements.txt" is the historical behavior for AE.
  local reqs_rel="${6-requirements.txt}"
  if ! component_wanted "$component_key"; then
    log "skipping $label per --only filter"; return 0
  fi
  banner "Component: $label"
  local src="$REPO_SRC/$src_subdir" dest="$LLMSYS_INSTALL_DIR/$dest_subdir"
  if [[ ! -d "$src" ]]; then
    warn "upstream tree missing at $src — skipping $label"; return 0
  fi
  # Compute changeset
  local changed; changed="$(files_changed "$src" "$dest" 2>/dev/null || true)"
  if [[ -z "$changed" ]]; then
    ok "no file changes for $label"
  else
    log "files that would change ($label):"
    printf '%s\n' "$changed" | sed 's/^/    /'
    if ! prompt_yn "Apply $label code update?" "y"; then
      warn "skipped $label per operator choice"; return 0
    fi
    if (( DRY_RUN )); then
      echo "  [dry-run] would sync $src → $dest"
    else
      log "backing up files that will change"
      backup_changed_files "$dest" "$changed"
      log "syncing $label code"
      sync_dir "$src" "$dest"
      if (( PARANOID_VERIFY )); then
        # shellcheck disable=SC2086
        verify_md5_pairs "$src" "$dest" $changed \
          || VERIFY_FAILURES=$((VERIFY_FAILURES+1))
      fi
      UPDATED_COMPONENTS+=("$label")
      [[ -n "$unit" ]] && RESTART_UNITS+=("$unit")
    fi
  fi
  # Venv refresh (manager and alarm-engine both have per-component venvs).
  # No `|| true` here: a silent pip failure is precisely how the websockets
  # outage shipped — the operator never saw the error message that would
  # have explained why the service crashes on next start. If pip can't
  # resolve requirements.txt, that needs to surface immediately.
  # Skip both refresh + verify when reqs_rel is empty (file-sync-only
  # component like the "Agent deploy source" mirror).
  if (( ! DRY_RUN )) && [[ -n "$reqs_rel" ]]; then
    if ! refresh_venv "$dest" "$reqs_rel"; then
      VERIFY_FAILURES=$((VERIFY_FAILURES+1))
      err "$label: venv refresh failed — service will not pick up new deps"
      return 1
    fi
    if ! verify_venv_imports "$dest/venv" "$dest/$reqs_rel"; then
      VERIFY_FAILURES=$((VERIFY_FAILURES+1))
      err "$label: post-install import verify failed — see lines above"
      return 1
    fi
  fi
}

# Map: label / repo-subdir / install-subdir / unit / --only-key
# A component failure lands in FAILED_UNITS and the run continues;
# the if-form keeps a non-zero return from tripping set -e.
if $HAVE_MANAGER; then
  update_component "Manager" \
    "llm-systems-manager" "llm-systems-manager" "llm-systems-manager.service" "manager" \
    "backend/requirements.txt" \
    || { FAILED_UNITS+=("llm-systems-manager.service")
         err "Manager update failed — continuing with remaining components; llm-systems-manager.service will NOT be auto-restarted"; }
fi
if $HAVE_AE; then
  update_component "Alarm engine" \
    "llm-systems-alarm-engine" "llm-systems-alarm-engine" "llm-systems-alarm-engine.service" "alarm-engine" \
    || { FAILED_UNITS+=("llm-systems-alarm-engine.service")
         err "Alarm engine update failed — continuing with remaining components; llm-systems-alarm-engine.service will NOT be auto-restarted"; }
fi

# Manager-host-only: keep the repo's agent/ dir in sync so the manager's
# /api/agent-tarball endpoint (which remote agents pull from during
# self-update) serves the same version this manager was just upgraded
# to. Without this, deploy-source drifts behind the running agent.
if $HAVE_MANAGER; then
  update_component "Agent deploy source" "agent" "agent" "" "agent" "" \
    || err "Agent deploy source sync failed — /api/agent-tarball may serve stale code"
fi

# Keep the installer entry points in sync so a future --update,
# --uninstall, or self-run of any installer sub-script doesn't execute
# stale code. We only refresh what's directly executed by the operator:
# tools/installer/ and the top-level install.sh. The config/example +
# unified_config.py files are handled by the "Config reconcile" step
# below; docs (README.md, HANDOFF.md, CLAUDE.md, docs/) are repo-author
# artifacts not consulted at runtime, so syncing them just churns the
# backup tree on every update.
#
# Skipped on agent-only and DB-only hosts — neither runs the installer
# from /opt; `install.sh --update` re-clones into /tmp anyway.
if ! $HAVE_MANAGER && ! $HAVE_AE; then
  log "skipping installer sync — no manager or AE on this host (DB-only or agent-only)"
elif component_wanted "installer"; then
  banner "Component: installer scripts"
  for sub in tools/installer install.sh; do
    [[ -e "$REPO_SRC/$sub" ]] || continue
    sub_src="$REPO_SRC/$sub"
    sub_dest="$LLMSYS_INSTALL_DIR/$sub"
    if [[ -d "$sub_src" ]]; then
      changed="$(files_changed "$sub_src" "$sub_dest" 2>/dev/null || true)"
      if [[ -z "$changed" ]]; then
        ok "$sub  (no changes)"
        continue
      fi
      log "files changing under $sub:"
      printf '%s\n' "$changed" | sed 's/^/    /'
      if prompt_yn "Apply changes to $sub?" "y"; then
        if (( DRY_RUN )); then
          echo "  [dry-run]"
        else
          backup_changed_files "$sub_dest" "$changed"
          sync_dir "$sub_src" "$sub_dest"
        fi
      fi
    else
      if cmp -s "$sub_src" "$sub_dest" 2>/dev/null; then
        ok "$sub  (no changes)"
      else
        _ndiff="$(diff "$sub_dest" "$sub_src" 2>/dev/null | grep -cE '^[<>]' || true)"
        log "$sub differs ($_ndiff line(s) changed); backup will be saved"
        if prompt_yn "Replace $sub?" "y"; then
          if (( DRY_RUN )); then
            echo "  [dry-run]"
          else
            backup_path "$sub_dest" >/dev/null
            $SUDO cp -a "$sub_src" "$sub_dest"
            ok "  copied $sub"
          fi
        fi
      fi
    fi
  done
fi

# ── Systemd unit refresh ───────────────────────────────────────────────────
banner "Systemd unit files"
if $HAVE_MANAGER && component_wanted "manager"; then
  unit_replace "llm-systems-manager.service" \
    "$REPO_SRC/systemd/llm-systems-manager.service.example" \
    && RESTART_UNITS+=("llm-systems-manager.service") || true
  # Refresh the admin-tab service-restart sudoers grant. Without this step an
  # in-place upgrade would expose the restart buttons + route with no grant,
  # so every restart would 403 at sudo. Idempotent; validates before install.
  if (( DRY_RUN )); then
    log "[dry-run] would refresh /etc/sudoers.d/llm-systems-manager (admin-tab restart grant)"
  else
    install_sudoers_fragment "$REPO_SRC/systemd/llm-systems-manager.sudoers.tmpl" \
      /etc/sudoers.d/llm-systems-manager || true
  fi
fi
if $HAVE_AE && component_wanted "alarm-engine"; then
  unit_replace "llm-systems-alarm-engine.service" \
    "$REPO_SRC/llm-systems-alarm-engine/systemd/llm-systems-alarm-engine.service.example" \
    && RESTART_UNITS+=("llm-systems-alarm-engine.service") || true
fi
# The agent unit is templated (substitutes ${AGENT_USER}/${AGENT_INSTALL_DIR}).
# We don't auto-replace from update.sh — that's what the agent's own
# install.sh --update path handles (and the admin tab triggers it remotely).
if $HAVE_AGENT && component_wanted "agent"; then
  # Detect change by what the agent installer would actually deploy
  # (llm-systems-agent.py + buffered_metric_client.py — the subset that
  # lands in AGENT_INSTALL_DIR). Comparing the full agent/ tree against
  # AGENT_INSTALL_DIR always reports drift (install/, agent_config.yaml.example
  # never get deployed), and gating on the VERSION constant alone missed
  # any change that didn't bump VERSION (buffered_metric_client.py edits,
  # systemd unit template tweaks, etc.).
  _agent_changed=0
  for _f in llm-systems-agent.py buffered_metric_client.py _utils.py agent_context.py; do
    [[ -f "$REPO_SRC/agent/$_f" && -f "$AGENT_INSTALL_DIR/$_f" ]] || { _agent_changed=1; break; }
    if ! cmp -s "$REPO_SRC/agent/$_f" "$AGENT_INSTALL_DIR/$_f"; then
      _agent_changed=1; break
    fi
  done
  # collectors/ (Tier 3 A1a) and providers/ (Tier 3 A2) are also deployed.
  # Drift in just a sub-module would otherwise skip the re-run.
  # Exclude __pycache__/*.pyc — runtime-generated bytecode always differs
  # between the repo tree and the deployed tree, so diffing it would report
  # a change on every run (the deploy itself rsync-excludes it too) (#135).
  for _pkg in collectors providers; do
    if (( ! _agent_changed )) && [[ -d "$REPO_SRC/agent/$_pkg" ]]; then
      if ! diff -rq -x '__pycache__' -x '*.pyc' \
            "$REPO_SRC/agent/$_pkg" "$AGENT_INSTALL_DIR/$_pkg" >/dev/null 2>&1; then
        _agent_changed=1
      fi
    fi
  done
  if (( ! _agent_changed )); then
    ok "Local agent: deployed files match upstream (version $AGT_V_OLD) — skipping installer rerun"
  else
    log "Local agent: $AGT_V_OLD  →  $AGT_V_NEW (files changed)"
    if (( DRY_RUN )); then
      echo "  [dry-run] would stage $REPO_SRC/agent → $AGENT_INSTALL_DIR/src/agent"
      echo "  [dry-run] would run: bash $AGENT_INSTALL_DIR/src/agent/install/install.sh --update --no-pull --skip-service-restart"
    elif prompt_yn "Re-run agent installer in --update mode?" "y"; then
      _agent_run_user="$(stat -c %U "$AGENT_INSTALL_DIR" 2>/dev/null \
                         || echo "$LLMSYS_RUN_USER")"
      $SUDO mkdir -p "$AGENT_INSTALL_DIR/src"
      $SUDO rm -rf "$AGENT_INSTALL_DIR/src/agent"
      $SUDO cp -a "$REPO_SRC/agent" "$AGENT_INSTALL_DIR/src/agent"
      $SUDO chown -R "$_agent_run_user:$_agent_run_user" "$AGENT_INSTALL_DIR/src"
      # Pass --skip-service-restart so the sub-installer doesn't restart
      # mid-run; we queue the agent on RESTART_UNITS so the unified end-of-run
      # restart loop handles it (with the same prompt + journalctl-on-fail
      # diagnostics the manager/AE get). Previously a non-zero exit anywhere
      # in the sub-installer bypassed its tail-end systemctl restart and the
      # caller never noticed.
      if bash "$AGENT_INSTALL_DIR/src/agent/install/install.sh" \
            --update --no-pull --skip-service-restart; then
        UPDATED_COMPONENTS+=("Local agent")
        RESTART_UNITS+=("llm-systems-agent.service")
      else
        warn "agent --update returned non-zero — review its output above; queuing restart anyway"
        UPDATED_COMPONENTS+=("Local agent")
        RESTART_UNITS+=("llm-systems-agent.service")
      fi
    fi
  fi
fi

# ── Config reconcile (unified_config.py + TOML key-merge) ─────────────────
# Both are required for the services to come back up cleanly:
#   1. unified_config.py is the typed schema the services import. New
#      manager/AE code that references a field absent from the live schema
#      crashes on startup — so we always overwrite it (and the .example
#      reference copy) from upstream. No secrets live here; safe to clobber.
#   2. llm-systems.toml carries operator values + secrets — we never
#      overwrite. Instead, we merge new keys from upstream's
#      llm-systems.toml.example into the live file (keeping existing values
#      and their inline comments untouched) after backing it up. Mirrors the
#      agent installer's agent_config.yaml reconciler. The .example is also
#      force-synced so future diffs are anchored against the new template.
banner "Config reconcile"
_cfg_dir="$LLMSYS_INSTALL_DIR/config"
if $HAVE_MANAGER || $HAVE_AE; then
  # Force-sync the schema/example files. unified_config.py itself isn't
  # tracked in git — install-config-bootstrap.sh derives it by copying
  # unified_config.py.example over on first install. The updater does the
  # same: the source for the live unified_config.py is *the upstream
  # example*, not "$REPO_SRC/config/unified_config.py" (which doesn't
  # exist and would silently no-op past the [[ -f $src ]] guard, leaving
  # the manager/AE with a stale schema after an upgrade — exactly the
  # outage mode reported in the field).
  #
  # Pairs are <dest_basename>:<src_basename>:
  #   - unified_config.py            ← upstream unified_config.py.example
  #   - unified_config.py.example    ← same (refresh the reference copy)
  #   - llm-systems.toml.example     ← same
  for pair in "unified_config.py:unified_config.py.example" \
              "unified_config.py.example:unified_config.py.example" \
              "llm-systems.toml.example:llm-systems.toml.example"; do
    dst_name="${pair%%:*}"
    src_name="${pair##*:}"
    src="$REPO_SRC/config/$src_name"
    dst="$_cfg_dir/$dst_name"
    if [[ ! -f "$src" ]]; then
      warn "upstream $src missing — skipping $dst_name refresh"
      continue
    fi
    if cmp -s "$src" "$dst" 2>/dev/null; then
      ok "$dst_name  (no change)"
      continue
    fi
    if (( DRY_RUN )); then
      log "[dry-run] would refresh $dst_name from upstream $src_name"
      continue
    fi
    [[ -f "$dst" ]] && { bak="$(backup_path "$dst")"; [[ -n "$bak" ]] && ok "  backed up $dst_name → $bak"; }
    $SUDO install -m 0644 -o "$LLMSYS_RUN_USER" -g "$LLMSYS_RUN_GROUP" "$src" "$dst"
    ok "refreshed $dst_name from upstream $src_name"
    # unified_config.py landing means the services that read it need a restart
    # to pick up the new schema/defaults. Queue them like a code change would.
    if [[ "$dst_name" == "unified_config.py" ]]; then
      $HAVE_MANAGER && RESTART_UNITS+=("llm-systems-manager.service")
      $HAVE_AE      && RESTART_UNITS+=("llm-systems-alarm-engine.service")
    fi
  done

  # Merge new keys from upstream's example into the live TOML.
  _live_toml="$_cfg_dir/llm-systems.toml"
  _example_toml="$REPO_SRC/config/llm-systems.toml.example"
  if [[ -f "$_live_toml" && -f "$_example_toml" ]]; then
    log "merging new keys from $(basename "$_example_toml") → $(basename "$_live_toml")"
    if (( DRY_RUN )); then
      log "[dry-run] would back up live TOML and append any missing keys"
    else
      # Backup is deferred until we know the merge produces changes — a no-op
      # run shouldn't litter the backups dir with copies of the unchanged file.
      # The merger walks both files line-by-line. For each [section] in the
      # example: if absent from live, the whole block is appended; if present,
      # any keys missing from live are appended at the end of the matching
      # section in live, together with the contiguous comment block above
      # each new key from the example. Existing values + inline comments are
      # never rewritten. Output goes back through $SUDO tee so a non-root
      # caller can still write to the 0600 file owned by $LLMSYS_RUN_USER.
      # Redirection MUST live inside the $() — placing `2> /tmp/file` after
      # the closing `"` applies it to the bash assignment (`_merged=…`),
      # not to the python invocation. Without this fix the python's
      # `ADDED=N` + key list leaked to the operator's terminal, the temp
      # file stayed empty, awk read no count, the default `:-0` kicked in,
      # and the "no new keys" branch fired — leaving the merged TOML
      # computed but never written despite the log claiming success.
      _TOML_MERGE_TMP="$(mktemp)"
      _merged="$($SUDO python3 - "$_live_toml" "$_example_toml" 2>"$_TOML_MERGE_TMP" <<'PYEOF'
import re, sys, tomllib

live_path, example_path = sys.argv[1:3]
live_text = open(live_path).read()
example_text = open(example_path).read()

# Semantic detection: parse both files with tomllib so we know what keys
# actually exist regardless of TOML's flexible surface syntax. The previous
# regex-based detector missed dotted top-level keys (`manager.auth.mode = …`
# at [manager] level — equivalent to a separate [manager.auth] section by
# TOML semantics) and section headers with trailing comments. Either could
# make the merger conclude a key was "missing" when it was already present
# under a different surface form, then append a duplicate section — which
# tomllib rejects, breaking the service on next start.
try:
    live_dict = tomllib.loads(live_text)
except Exception as e:
    sys.stderr.write(f"PARSE_FAILED: live TOML at {live_path} doesn't parse: {e}\n")
    sys.exit(2)
try:
    ex_dict = tomllib.loads(example_text)
except Exception as e:
    sys.stderr.write(f"PARSE_FAILED: example TOML at {example_path} doesn't parse: {e}\n")
    sys.exit(2)

def flatten(d, prefix=""):
    """Yield 'a.b.c' for every leaf in the parsed dict. Inline arrays-of-
    tables aren't treated as leaves here because tomllib expands them — but
    those are vanishingly rare in our config schema."""
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            yield from flatten(v, path)
        else:
            yield path

live_paths = set(flatten(live_dict))
ex_paths_ordered = list(flatten(ex_dict))
missing_paths = [p for p in ex_paths_ordered if p not in live_paths]

if not missing_paths:
    sys.stderr.write("ADDED=0\n")
    sys.stdout.write(live_text)
    sys.exit(0)

# Insertion: use line-based scanning ONLY to find section headers and the
# textual span of each key (so comments above the key are preserved on the
# splice). All "is this missing?" decisions came from tomllib above; this
# layer just turns those decisions into edits.
#
# SECTION_RE now tolerates a trailing comment after the closing `]` (TOML
# permits `[section]  # description` and the previous parser misclassified
# such lines as body content).
SECTION_RE = re.compile(r'^\s*\[([^\]]+?)\]\s*(?:#.*)?$')
# KEY_RE accepts dotted top-level keys (`a.b.c = …`) so we can locate them
# in the example text. The example's surface form is what we splice — so
# whatever syntax the example uses survives into the merged live file.
KEY_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*=')

def split_sections(text):
    """Returns [(section_name, header_line_or_None, [body_lines])]."""
    out = []
    cur = ['', None, []]
    for ln in text.splitlines():
        m = SECTION_RE.match(ln)
        if m:
            out.append(tuple(cur))
            cur = [m.group(1).strip(), ln, []]
        else:
            cur[2].append(ln)
    out.append(tuple(cur))
    return out

def keys_with_spans(body):
    """Yield (key_name, start_idx, end_idx_exclusive, comment_block_start)
    for every top-level key in this section body, in source order. Spans
    cover multi-line array / inline-table values via bracket balance, and
    `comment_block_start` points at the first contiguous comment line
    immediately above the key (no blank-line gap)."""
    i = 0
    while i < len(body):
        m = KEY_RE.match(body[i])
        if not m:
            i += 1
            continue
        key = m.group(1)
        start = i
        val = body[i].split('=', 1)[1] if '=' in body[i] else ''
        depth_sq = val.count('[') - val.count(']')
        depth_br = val.count('{') - val.count('}')
        i += 1
        while i < len(body) and (depth_sq > 0 or depth_br > 0):
            depth_sq += body[i].count('[') - body[i].count(']')
            depth_br += body[i].count('{') - body[i].count('}')
            i += 1
        # Walk upward over the comment block above this key (stops at blank).
        cb = start - 1
        while cb >= 0 and body[cb].lstrip().startswith('#'):
            cb -= 1
        yield (key, start, i, cb + 1)

live_secs = split_sections(live_text)
ex_secs   = split_sections(example_text)

# Lookup helpers
live_idx = {}
for i, (name, _, _) in enumerate(live_secs):
    if name and name not in live_idx:
        live_idx[name] = i
ex_by_name = {}
for name, header, body in ex_secs:
    if name:
        ex_by_name[name] = (header, body)

def section_for_path(path):
    """Return (section_name, key_basename) by walking the path's prefixes
    from longest to shortest and picking the longest one that's defined as
    a section in the example. e.g. 'a.b.c' with [a.b] → ('a.b', 'c')."""
    parts = path.split('.')
    for i in range(len(parts) - 1, 0, -1):
        section = '.'.join(parts[:i])
        if section in ex_by_name:
            return section, '.'.join(parts[i:])
    return None, path

# Group missing paths by their containing example section, so we can do one
# positional splice per section.
missing_by_section = {}  # section_name -> set of key basenames
for path in missing_paths:
    section, key_basename = section_for_path(path)
    if section is None or section not in ex_by_name:
        # Top-level scalar in the preamble — rare; skip rather than guess.
        continue
    missing_by_section.setdefault(section, set()).add(key_basename)

added = []
appended_sections = []

def splice_at_positions(live_body, ex_body, missing_keys):
    """Return a new live body with the missing keys inserted at the position
    each occupies in the example: each missing key goes immediately AFTER the
    nearest preceding example key that's also present in live. Keys whose
    natural position is above every live key land at the top of the body.

    The point of this dance is to preserve the example's intended ordering
    (e.g. tls_port = 5443 should sit between port = 5000 and poll_interval,
    not at the very bottom of the section) without disturbing live's
    existing keys or rewriting their inline comments."""
    ex_keys = list(keys_with_spans(ex_body))
    live_end_of = {k: e for k, _s, e, _c in keys_with_spans(live_body)}

    # Insertion plan: list of (live_pos, lines_to_insert) tuples. live_pos is
    # the position in live_body's line list (exclusive end of an anchor key,
    # or 0 for "before everything").
    plan = []
    last_anchor_end = 0
    for key, s, e, cb in ex_keys:
        if key in live_end_of:
            last_anchor_end = live_end_of[key]
        elif key in missing_keys:
            plan.append((last_anchor_end, ex_body[cb:e], key))

    # Apply in reverse so earlier inserts don't shift later anchor positions.
    out = list(live_body)
    for pos, lines, _key in reversed(plan):
        addition = []
        # Blank-line separator: leave one above the new block when the
        # previous line isn't already blank. Below the new block we only
        # add a separator when the NEXT line isn't already blank — this is
        # the line at `pos` in the unmodified list, which we lookup before
        # the splice.
        if pos > 0 and out[pos - 1].strip():
            addition.append('')
        addition.extend(lines)
        if pos < len(out) and out[pos].strip():
            addition.append('')
        out[pos:pos] = addition
    return out, [k for _p, _l, k in plan]

# Splice into existing live sections (positional, not appended).
for section, missing_set in missing_by_section.items():
    if section not in live_idx:
        continue
    _, ex_body = ex_by_name[section]
    li = live_idx[section]
    lname, lheader, lbody = live_secs[li]
    new_body, inserted_keys = splice_at_positions(lbody, ex_body, missing_set)
    live_secs[li] = (lname, lheader, new_body)
    for k in inserted_keys:
        added.append(f"{section}.{k}")

# Sections that aren't in live at all — append, but only the keys we actually
# need (in their example order, with their leading comment blocks). This
# avoids dragging in unrelated keys the example happens to define in the
# same section but that aren't part of the missing-paths set.
for section, missing_set in missing_by_section.items():
    if section in live_idx:
        continue
    header, ex_body = ex_by_name[section]
    body_lines = []
    inserted_keys = []
    for key, s, e, cb in keys_with_spans(ex_body):
        if key not in missing_set:
            continue
        if body_lines:
            body_lines.append('')
        body_lines.extend(ex_body[cb:e])
        inserted_keys.append(key)
    appended_sections.append((section, header, body_lines))
    for k in inserted_keys:
        added.append(f"{section}.{k}")

# Re-assemble.
out = []
for name, header, body in live_secs:
    if header is not None:
        out.append(header)
    out.extend(body)
for name, header, body in appended_sections:
    if out and out[-1].strip() != '':
        out.append('')
    out.append(header)
    out.extend(body)
result = '\n'.join(out)
if not result.endswith('\n'):
    result += '\n'

# Validate before declaring success — refuse to emit a file that won't parse.
# This is the safety net that keeps a degenerate merge from breaking the
# service on next start: if the structural insertion produced an invalid
# TOML for any reason (duplicate sections, malformed value splice, etc.),
# bail out with a clear error and leave the live file untouched.
try:
    tomllib.loads(result)
except Exception as e:
    sys.stderr.write(f"VALIDATE_FAILED: merged TOML doesn't parse: {e}\n")
    sys.exit(3)

sys.stderr.write(f"ADDED={len(added)}\n")
for p in added:
    sys.stderr.write(f"  + {p}\n")
sys.stdout.write(result)
PYEOF
)" || {
    _rc=$?
    if (( _rc == 2 )); then
      err "TOML merge: one of the input files doesn't parse as TOML — see $_TOML_MERGE_TMP"
    elif (( _rc == 3 )); then
      err "TOML merge: merger produced an invalid TOML — live config untouched. Diff $_TOML_MERGE_TMP"
    else
      err "TOML merge failed (exit $_rc) — see $_TOML_MERGE_TMP"
    fi
    die "live config left untouched"
  }
      _added_count="$(awk -F= '/^ADDED=/{print $2}' "$_TOML_MERGE_TMP")"
      if [[ "${_added_count:-0}" == "0" ]]; then
        ok "no new keys to merge — live TOML already in sync with upstream example"
        rm -f "$_TOML_MERGE_TMP"
      else
        # Now that we know we're actually rewriting, back up the live file.
        _bak="$(backup_path "$_live_toml")"
        [[ -n "$_bak" ]] && ok "  backed up live TOML → $_bak"
        # Stream-write the merged content back through sudo so file ownership
        # and the 0600 mode are preserved. The trailing \n is re-added here
        # because $(...) strips trailing newlines from command substitution.
        printf '%s\n' "$_merged" | $SUDO tee "$_live_toml" >/dev/null
        $SUDO chmod 0600 "$_live_toml"
        $SUDO chown "$LLMSYS_RUN_USER:$LLMSYS_RUN_GROUP" "$_live_toml"
        ok "merged $_added_count new key(s) into $_live_toml:"
        grep -E '^\s*\+' "$_TOML_MERGE_TMP" | sed 's/^/    /'
        rm -f "$_TOML_MERGE_TMP"
        $HAVE_MANAGER && RESTART_UNITS+=("llm-systems-manager.service")
        $HAVE_AE      && RESTART_UNITS+=("llm-systems-alarm-engine.service")
      fi
    fi
  else
    log "no live config/llm-systems.toml found — nothing to merge"
  fi
else
  log "skipping config reconcile — no manager or AE on this host"
fi

# ── Restart services ───────────────────────────────────────────────────────
if (( NEED_DAEMON_RELOAD )) && (( ! DRY_RUN )); then
  log "running deferred systemctl daemon-reload"
  $SUDO systemctl daemon-reload
fi

banner "Service restart"
_restart_phase_reached=1
# De-dup up front (pre-flight stop, unit_replace, sync_dir, config-reconcile all
# push the same names) so every branch below sees a clean list.
if (( ${#RESTART_UNITS[@]} > 0 )); then
  declare -A _seen=(); _uniq=()
  for u in "${RESTART_UNITS[@]}"; do
    [[ -n "${_seen[$u]:-}" ]] && continue
    _seen[$u]=1
    # Units whose component update failed are excluded from restart.
    if _unit_failed "$u"; then
      warn "skipping restart of $u — its component update failed; fix it, then: sudo systemctl restart $u"
      continue
    fi
    _uniq+=("$u")
  done
  RESTART_UNITS=(${_uniq[@]+"${_uniq[@]}"})
fi
if (( ${#RESTART_UNITS[@]} == 0 )); then
  log "nothing to restart"
elif (( SKIP_RESTART )); then
  warn "--skip-restart set; restart manually with:"
  for u in "${RESTART_UNITS[@]}"; do warn "    sudo systemctl restart $u"; done
elif (( DRY_RUN )); then
  log "[dry-run] would restart: ${RESTART_UNITS[*]}"
else
  if prompt_yn "Restart now: ${RESTART_UNITS[*]} ?" "y"; then
    for u in "${RESTART_UNITS[@]}"; do
      log "restarting $u"
      $SUDO systemctl restart "$u" || warn "$u restart returned non-zero"
      sleep 2
      if $SUDO systemctl is-active --quiet "$u"; then
        ok "$u active"
      else
        err "$u not active after restart — recent log:"
        $SUDO journalctl -u "$u" -n 20 --no-pager 2>&1 | sed 's/^/    /'
      fi
    done
  else
    warn "left services as-is; restart manually when ready"
  fi
fi

# ── Health probes ───────────────────────────────────────────────────────────
# Upgrade-time verification is intentionally NARROW: confirm each affected
# service responds to /health AND self-reports a version matching what we
# just deployed. The full smoke tests in tools/*.sh are scoped at the
# repo's UI/DOM/CDP layer — they assume a logged-in browser session, fail
# noisily on the auth gate, and are intended for end-of-feature validation,
# not for upgrades. Run those by hand after the upgrade if you want them.
banner "Health checks"
PASS=0; FAIL=0

# _probe_version_match <label> <url> <expected_version> [unit]
#
# Probes the service's /health endpoint, parses the JSON, and confirms
# `.version` matches what we just deployed. Bare 200 (without a version
# field) counts as a pass — older builds may not emit one — but a version
# mismatch is a HARD failure since it means systemd is still serving the
# pre-upgrade binary (Restart=on-failure can keep a crashed-on-new-code
# process running on the cached old image until it gives up).
_probe_version_match() {
  local label="$1" url="$2" expected="$3" unit="${4:-}"
  local response body code reported attempts=30 i
  # Self-heal the URL: `[alarm_engine].tls_enabled = true` is the default
  # now, so the AE serves HTTPS on its port — a hardcoded http:// probe
  # always sees curl get a TLS handshake from a non-TLS request, returns
  # an empty body + HTTP 000, then dumps the AE's normal-startup journal
  # output as a "failure." Try the URL as-is first; if it never answers,
  # auto-flip http→https with --insecure (localhost liveness, the cert is
  # self-signed by the internal CA which curl won't trust by default) and
  # try again. This is a probe-with-fallback, not a config rewrite —
  # nothing on disk changes.
  local probe_url="$url" curl_extra=()
  case "$probe_url" in
    https://*) curl_extra+=(--insecure) ;;
  esac
  # Poll /health for up to ~30s before declaring failure. The AE takes
  # 5–10s to come up (Influx connect + warm-up + WS broadcaster start);
  # a single immediate curl after systemctl restart races startup and
  # falsely reports HTTP 000, then dumps a noisy journalctl banner of
  # what was actually normal boot output. Quiet on success — no
  # journalctl spam when the probe actually answers.
  for (( i=1; i<=attempts; i++ )); do
    response="$(curl -sS --max-time 2 -w '\n%{http_code}' "${curl_extra[@]+"${curl_extra[@]}"}" "$probe_url" 2>/dev/null || true)"
    code="${response##*$'\n'}"
    body="${response%$'\n'*}"
    [[ -z "$code" ]] && code="000"
    [[ "$code" == "200" ]] && break
    # Half-way through the polling budget, if the URL is http:// and we're
    # still seeing 000 (the most likely signal that the server is actually
    # HTTPS), try flipping the scheme so the remaining attempts probe the
    # correct port.
    if (( i == 8 )) && [[ "$probe_url" == http://* && "$code" == "000" ]]; then
      probe_url="https://${probe_url#http://}"
      curl_extra=(--insecure)
      log "  $label: no answer on http — retrying with https (TLS default-on)"
    fi
    (( i < attempts )) && sleep 1
  done
  if [[ "$code" != "200" ]]; then
    err "  $label: HTTP $code from $url (after ${attempts}s)"
    FAIL=$((FAIL+1))
    [[ -n "$unit" ]] && $SUDO journalctl -u "$unit" -n 15 --no-pager 2>&1 \
                          | sed "s/^/      /"
    return
  fi
  reported="$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("version", ""))
except Exception:
    pass
' 2>/dev/null)"
  if [[ -z "$reported" ]]; then
    ok "  $label: HTTP 200 (no version reported)"
    PASS=$((PASS+1))
    return
  fi
  if [[ -n "$expected" && "$reported" != "$expected" ]]; then
    err "  $label: running $reported, expected $expected — service may not have picked up the new code"
    FAIL=$((FAIL+1))
    [[ -n "$unit" ]] && $SUDO journalctl -u "$unit" -n 15 --no-pager 2>&1 \
                          | sed "s/^/      /"
    return
  fi
  ok "  $label: $reported"
  PASS=$((PASS+1))
}

# _component_expected <label> <old> <new> — expect the NEW version only if this
# component was actually deployed (in UPDATED_COMPONENTS), else OLD (a declined
# code sync that still gets restarted for config shouldn't flag a mismatch).
_component_expected() {
  local label="$1" oldv="$2" newv="$3"
  if (( ${#UPDATED_COMPONENTS[@]} )) && printf '%s\n' "${UPDATED_COMPONENTS[@]}" | grep -qx "$label"; then
    printf '%s' "$newv"
  else
    printf '%s' "$oldv"
  fi
}

if (( SKIP_TESTS )); then
  warn "--skip-tests set; not probing /health on upgraded services"
else
  $HAVE_MANAGER && _probe_version_match "Manager     " "http://127.0.0.1:5000/health" \
                       "$(_component_expected "Manager" "$MGR_V_OLD" "$MGR_V_NEW")" "llm-systems-manager.service"
  # AE TLS defaults to ON ([alarm_engine].tls_enabled = true), so the
  # canonical /health endpoint is HTTPS unless the operator explicitly
  # opted out. Read the live TOML to pick the right scheme up front.
  # _probe_version_match itself has an http→https fallback as a belt-and-
  # suspenders for fresh installs where this read fails (no python3 in
  # the right place, no toml file yet, etc.).
  _ae_scheme="https"
  if [[ -f "$LLMSYS_INSTALL_DIR/config/llm-systems.toml" ]]; then
    _tls_on="$(as_run_user python3 - <<'PYEOF' "$LLMSYS_INSTALL_DIR/config/llm-systems.toml" 2>/dev/null || true
import sys, tomllib
try:
    cfg = tomllib.loads(open(sys.argv[1]).read())
    print("1" if cfg.get("alarm_engine", {}).get("tls_enabled", True) else "0")
except Exception:
    print("1")
PYEOF
    )"
    [[ "$_tls_on" == "0" ]] && _ae_scheme="http"
  fi
  $HAVE_AE      && _probe_version_match "Alarm engine" "${_ae_scheme}://127.0.0.1:8081/health" \
                       "$(_component_expected "Alarm engine" "$AE_V_OLD" "$AE_V_NEW")"  "llm-systems-alarm-engine.service"
  # InfluxDB and the local agent don't carry our VERSION constant — fall back
  # to plain HTTP 200 via the existing reporting helper.
  $HAVE_INFLUX  && {
    if report_service_health "InfluxDB    " "http://127.0.0.1:8086/health" 200 "influxdb.service"; then
      PASS=$((PASS+1))
    else
      FAIL=$((FAIL+1))
      $SUDO journalctl -u influxdb.service -n 15 --no-pager 2>&1 | sed 's/^/      /'
    fi
  }
  $HAVE_AGENT   && {
    if report_service_health "Local agent " "https://127.0.0.1:8082/health" 200 "llm-systems-agent.service"; then
      PASS=$((PASS+1))
    else
      FAIL=$((FAIL+1))
      $SUDO journalctl -u llm-systems-agent.service -n 15 --no-pager 2>&1 | sed 's/^/      /'
    fi
  }
fi

# ── Summary ───────────────────────────────────────────────────────────────
banner "Update summary"
if (( ${#UPDATED_COMPONENTS[@]} > 0 )); then
  ok "updated: ${UPDATED_COMPONENTS[*]}"
  # Recap version transitions for components that actually got touched.
  _did_update() { printf '%s\n' "${UPDATED_COMPONENTS[@]}" | grep -qx "$1"; }
  _did_update "Manager"      && log "  Manager:      ${MGR_V_OLD:-<unknown>} → ${MGR_V_NEW:-<unknown>}"
  _did_update "Alarm engine" && log "  Alarm engine: ${AE_V_OLD:-<unknown>}  → ${AE_V_NEW:-<unknown>}"
  _did_update "Local agent"  && log "  Local agent:  ${AGT_V_OLD:-<unknown>} → ${AGT_V_NEW:-<unknown>}"
else
  log "no code changes applied"
fi
(( _backup_root_made )) && log "backups under: $BACKUP_ROOT"
log "health: $PASS pass / $FAIL fail"
(( PARANOID_VERIFY )) && log "md5 verify: $VERIFY_FAILURES mismatch(es)"
if (( VERIFY_FAILURES > 0 )); then
  err "$VERIFY_FAILURES md5 verification failure(s) above — investigate"
fi
if (( ${#FAILED_UNITS[@]} > 0 )); then
  err "component update failure(s) — these services were NOT restarted: ${FAILED_UNITS[*]}"
fi
if (( FAIL > 0 || ${#FAILED_UNITS[@]} > 0 )); then
  err "Update finished with failures."
  err "Staging clone preserved at $REPO_SRC for inspection."
  exit 1
fi

# ── Cleanup: drop the staging clone + any /tmp launcher artifacts ──────────
# Mirrors install.sh's end-of-run cleanup. Only runs on clean success so
# operators can poke around on failure without re-cloning.
banner "Cleanup"
if [[ -d "$REPO_SRC" && "$REPO_SRC" == /tmp/* ]]; then
  $SUDO rm -rf "$REPO_SRC"
  ok "removed staging clone $REPO_SRC"
fi
# A curl-piped `install.sh --update` may have left install.sh in /tmp.
for stray in /tmp/install.sh /tmp/llm-systems-manager-install.sh; do
  if [[ -f "$stray" ]]; then
    $SUDO rm -f "$stray"
    ok "removed $stray"
  fi
done

ok "Update complete."
