#!/usr/bin/env bash
# =============================================================================
# tools/installer/brew-seed-config.sh — seed llm-systems.toml for Homebrew
#
# Called from the llm-systems-manager / llm-systems-alarm-engine formulas'
# post_install (either may run first — both call this, first writer wins).
# Non-interactive, no sudo, safe on macOS bash 3.2.
#
# Env:
#   LSM_BREW_EXAMPLE  path to config/llm-systems.toml.example (required)
#   LSM_BREW_CONFIG   target config path, e.g.
#                     $(brew --prefix)/etc/llm-systems-manager/llm-systems.toml (required)
#   LSM_BREW_LOG_DIR  log dir written to [paths].log_dir, e.g.
#                     $(brew --prefix)/var/log/llm-systems-manager (required)
#
# Does:
#   - If LSM_BREW_CONFIG already exists (an upgrade), keeps it: adds keys the
#     example introduced (operator values kept), adds the https entries to
#     [alarm_engine].cors_origins, keeps the previous file as .bak.<stamp>
#     next to it when anything changed, then exits 0.
#   - Copies the example, rewrites [paths].log_dir to LSM_BREW_LOG_DIR, and
#     generates [alarm_engine] ingest_token + management_token (the co-located
#     default the script installer also applies).
#   - chmod 0600 — the file holds secrets.
#
# Does NOT touch [influxdb] host/tokens — brew-influx-setup.sh (installed as
# llm-systems-influx-setup) fills those (REPLACE_ME is ignored/warned at runtime).
# =============================================================================
set -euo pipefail

die() { echo "brew-seed-config: ERROR: $*" >&2; exit 1; }

EXAMPLE="${LSM_BREW_EXAMPLE:-}"
TARGET="${LSM_BREW_CONFIG:-}"
LOG_DIR="${LSM_BREW_LOG_DIR:-}"
[ -n "$EXAMPLE" ] || die "LSM_BREW_EXAMPLE is not set"
[ -n "$TARGET" ]  || die "LSM_BREW_CONFIG is not set"
[ -n "$LOG_DIR" ] || die "LSM_BREW_LOG_DIR is not set"
[ -f "$EXAMPLE" ] || die "example config not found: $EXAMPLE"

# Python with tomllib: the keg venv first, then the PATH interpreter.
find_python() {
  local here cand
  here="$(cd "$(dirname "$0")" && pwd)"
  for cand in "$here/../../venv/bin/python3" python3; do
    if "$cand" -c 'import tomllib' >/dev/null 2>&1; then printf '%s\n' "$cand"; return 0; fi
  done
  return 1
}

# Copies the config to <config>.bak.<stamp> once per run, before the first rewrite.
BACKUP_DONE=0
backup_once() {
  local bak
  [ "$BACKUP_DONE" -eq 1 ] && return 0
  bak="$TARGET.bak.$(date +%Y%m%d-%H%M%S)"
  (umask 077; cp "$TARGET" "$bak") || return 1
  chmod 0600 "$bak"
  BACKUP_DONE=1
  echo "brew-seed-config: previous config kept at $bak"
}

# Replaces the config with <tmp> after a backup; keeps the config on any failure.
install_rewrite() {
  local tmp="$1"
  if backup_once && mv "$tmp" "$TARGET"; then
    chmod 0600 "$TARGET"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# Adds keys the example introduced to the existing config, keeping operator
# values; any failure leaves the config as it is.
merge_new_keys() {
  local py here tmp err added
  py="$(find_python)" || return 0
  here="$(cd "$(dirname "$0")" && pwd)"
  tmp="$TARGET.seed.$$"
  err="$TARGET.seed.$$.err"
  if (umask 077; "$py" -B "$here/toml_reconcile.py" merge "$TARGET" "$EXAMPLE" > "$tmp" 2> "$err") \
     && [ -s "$tmp" ]; then
    added="$(awk -F= '/^ADDED=/{print $2}' "$err")"
    rm -f "$err"
    if [ "${added:-0}" = "0" ]; then
      rm -f "$tmp"
    elif install_rewrite "$tmp"; then
      echo "brew-seed-config: merged $added new key(s) from the example into $TARGET"
    else
      echo "brew-seed-config: could not write the merged config — $TARGET kept as is; compare it with $EXAMPLE" >&2
    fi
  else
    rm -f "$tmp" "$err"
    echo "brew-seed-config: could not merge new keys into $TARGET — kept as is; compare it with $EXAMPLE" >&2
  fi
}

# Adds the https twin of each http origin to the existing config's
# [alarm_engine].cors_origins; any failure leaves the config as it is.
fix_origins() {
  local py here tmp
  py="$(find_python)" || return 0
  here="$(cd "$(dirname "$0")" && pwd)"
  tmp="$TARGET.seed.$$"
  if (umask 077; "$py" -B "$here/toml_reconcile.py" origins "$TARGET" "" 0 > "$tmp" 2>/dev/null) \
     && [ -s "$tmp" ] && ! cmp -s "$tmp" "$TARGET"; then
    if install_rewrite "$tmp"; then
      echo "brew-seed-config: added https entries to the alarm engine's allowed origins"
    else
      echo "brew-seed-config: could not write the allowed-origins fix — $TARGET kept as is" >&2
    fi
  else
    rm -f "$tmp"
  fi
  return 0
}

if [ -f "$TARGET" ]; then
  echo "brew-seed-config: $TARGET already exists — keeping it"
  merge_new_keys
  fix_origins
  exit 0
fi

# Tokens: openssl when present (always on macOS + linuxbrew), urandom fallback.
gen_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    # head reads first so no downstream stage triggers SIGPIPE under pipefail.
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    echo
  fi
}
INGEST_TOKEN="$(gen_token)"
MGMT_TOKEN="$(gen_token)"
[ ${#INGEST_TOKEN} -eq 64 ] || die "token generation failed"
[ ${#MGMT_TOKEN} -eq 64 ]   || die "token generation failed"

mkdir -p "$(dirname "$TARGET")" "$LOG_DIR"

# Line-by-line rewrite with printf %s — no sed/parameter-expansion, so the
# substituted values can never be corrupted by &, |, or backslashes.
TMP="$TARGET.seed.$$"
umask 077
: > "$TMP"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    'log_dir '*|'log_dir='*)
      printf 'log_dir = "%s"                # manager + alarm engine log files\n' "$LOG_DIR" >> "$TMP" ;;
    'ingest_token = "REPLACE_ME"'*)
      printf 'ingest_token = "%s"\n' "$INGEST_TOKEN" >> "$TMP" ;;
    'management_token = ""'*)
      printf 'management_token = "%s"\n' "$MGMT_TOKEN" >> "$TMP" ;;
    *)
      printf '%s\n' "$line" >> "$TMP" ;;
  esac
done < "$EXAMPLE"

# All three rewrites must have landed — a drifted .example must fail loudly.
grep -q "^log_dir = \"$LOG_DIR\"" "$TMP"          || { rm -f "$TMP"; die "log_dir rewrite failed — .example drifted?"; }
grep -q "^ingest_token = \"$INGEST_TOKEN\"" "$TMP" || { rm -f "$TMP"; die "ingest_token rewrite failed — .example drifted?"; }
grep -q "^management_token = \"$MGMT_TOKEN\"" "$TMP" || { rm -f "$TMP"; die "management_token rewrite failed — .example drifted?"; }

mv "$TMP" "$TARGET"
chmod 0600 "$TARGET"
echo "brew-seed-config: seeded $TARGET (log_dir=$LOG_DIR, AE tokens generated)"
echo "brew-seed-config: fill [influxdb.tokens] with llm-systems-influx-setup (needs: brew install influxdb@2 influxdb-cli)"
