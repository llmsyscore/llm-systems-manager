#!/usr/bin/env bash
# Scan the macOS-reachable installer scripts for bash >=4 constructs that fail
# on macOS's stock bash 3.2 (e.g. ${var,,} "bad substitution"). bash -n on a
# modern host does NOT catch these — they're runtime expansion errors — so this
# static check is the pre-ship gate. Exits non-zero on any hit. Default targets
# are the agent installer (the only installer that runs on macOS); pass paths to
# override.
set -uo pipefail

TARGETS=("$@")
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  TARGETS=("$_here/../../agent/install/install.sh")
fi

# label<TAB>extended-regex — each matches a bash 4+ feature absent in 3.2.
CHECKS=(
  $'case-mod ${v,,}/${v^^}\t\\$\\{[A-Za-z0-9_]+(\\[[^]]*\\])?(,,|,|\\^\\^|\\^)'
  $'param-transform ${v@U/@L}\t\\$\\{[A-Za-z0-9_]+(\\[[^]]*\\])?@[A-Za-z]'
  $'assoc array (declare/local -A)\t\\b(declare|local|typeset)[[:space:]]+-[A-Za-z]*A'
  $'declare -g\t\\bdeclare[[:space:]]+-[A-Za-z]*g'
  $'mapfile/readarray\t\\b(mapfile|readarray)\\b'
  $'coproc\t\\bcoproc\\b'
  $'wait -n\t\\bwait[[:space:]]+-n\\b'
  $'&>> append redirect\t&>>'
  $'negative array index ${a[-1]}\t\\$\\{[A-Za-z0-9_]+\\[[[:space:]]*-[0-9]'
  $'[[ -v var ]]\t\\[\\[[[:space:]]+-v[[:space:]]'
  $'printf %(...)T\t%\\([^)]*\\)T'
)

hits=0
for f in "${TARGETS[@]}"; do
  [[ -f "$f" ]] || { echo "  skip (not found): $f" >&2; continue; }
  # Strip comments so documentation of a construct isn't flagged.
  stripped="$(sed 's/#.*$//' "$f")"
  for entry in "${CHECKS[@]}"; do
    label="${entry%%$'\t'*}"; rx="${entry#*$'\t'}"
    while IFS= read -r line; do
      [[ -n "$line" ]] && { echo "  [$label] $f: $line"; hits=$((hits + 1)); }
    done < <(printf '%s\n' "$stripped" | grep -nE "$rx" || true)
  done
done

if [[ "$hits" -gt 0 ]]; then
  echo "FAIL: $hits bash 4+ construct(s) — rewrite for bash 3.2 before shipping." >&2
  exit 1
fi
echo "OK: macOS bash 3.2 compatible (no bash 4+ constructs in ${#TARGETS[@]} file(s))."
