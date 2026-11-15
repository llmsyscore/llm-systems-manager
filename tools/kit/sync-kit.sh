#!/usr/bin/env bash
# Copies the shared panel kit from a pinned manager commit, or checks a copy for drift.
set -euo pipefail
check=0
if [[ "${1:-}" == "--check" ]]; then check=1; shift; fi
sha="${1:-}"; dest="${2:-}"
if (( $# != 2 )) || [[ ! "$sha" =~ ^[0-9a-f]{40}$ || -z "$dest" ]]; then
  echo "usage: sync-kit.sh [--check] <40-hex manager sha> <dest-dir>" >&2; exit 2
fi
base="${KIT_BASE:-https://raw.githubusercontent.com/llmsyscore/llm-systems-manager}/$sha/llm-systems-manager/frontend/kit"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
for f in kit.js kit.css; do
  curl -fsSL --retry 3 --max-time 30 "$base/$f" -o "$tmp/$f" || { echo "fetch failed: $f" >&2; exit 3; }
done
ver="$(head -n1 "$tmp/kit.js" | sed -n 's|^// Panel kit \([0-9][0-9.]*\)$|\1|p')"
if [[ -z "$ver" ]]; then echo "no kit version in kit.js" >&2; exit 4; fi
printf 'sha=%s\nversion=%s\n' "$sha" "$ver" > "$tmp/kit.pin"
if (( check )); then
  rc=0
  for f in kit.js kit.css kit.pin; do
    cmp -s "$tmp/$f" "$dest/$f" || { echo "kit drift: $f" >&2; rc=1; }
  done
  exit "$rc"
fi
mkdir -p "$dest"
cp "$tmp/kit.js" "$tmp/kit.css" "$tmp/kit.pin" "$dest/"
