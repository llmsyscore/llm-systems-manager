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
#   - Exits 0 untouched if LSM_BREW_CONFIG already exists (upgrades keep config).
#   - Copies the example, rewrites [paths].log_dir to LSM_BREW_LOG_DIR, and
#     generates [alarm_engine] ingest_token + management_token (the co-located
#     default the script installer also applies).
#   - chmod 0600 — the file holds secrets.
#
# Does NOT touch [influxdb] host/tokens — the operator fills those in after
# `influx setup` (REPLACE_ME placeholders are ignored/warned at runtime).
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

if [ -f "$TARGET" ]; then
  echo "brew-seed-config: $TARGET already exists — keeping it"
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
echo "brew-seed-config: set [influxdb] host/port + [influxdb.tokens] after 'influx setup'"
