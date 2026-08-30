#!/usr/bin/env bash
# ci-libcommon-skew.sh <tarball lib-common.sh> [install.sh] [main lib-common.sh]
# Fails when install.sh calls a lib-common function the tarball's copy lacks
# and install.sh defines no shim for it (a main install.sh pairs with the
# latest release tarball's lib-common on the default --source release path).
set -euo pipefail

old_lib="${1:?tarball lib-common.sh}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_sh="${2:-$here/install.sh}"
new_lib="${3:-$here/lib-common.sh}"

_defs() { grep -oE '^[A-Za-z_][A-Za-z0-9_]*\(\)' "$1" | tr -d '()' | LC_ALL=C sort -u; }

old_defs="$(_defs "$old_lib")"
new_defs="$(_defs "$new_lib")"
# Shims: functions install.sh defines itself (any indentation).
shims="$(grep -oE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*\(\)[[:space:]]*\{' "$install_sh" | sed -E 's/[[:space:]]*\(\).*//; s/^[[:space:]]*//' | LC_ALL=C sort -u)"
# Body after the real lib-common is sourced.
start="$(grep -nF '. "$REPO_SRC/tools/installer/lib-common.sh"' "$install_sh" | head -1 | cut -d: -f1)"
[[ -n "$start" ]] || { echo "lib-common source line not found in $install_sh" >&2; exit 2; }
body="$(tail -n +"$start" "$install_sh")"

missing=()
while IFS= read -r fn; do
  [[ -n "$fn" ]] || continue
  grep -qxF "$fn" <<<"$old_defs" && continue
  grep -qxF "$fn" <<<"$shims" && continue
  if grep -qE "(^|[^A-Za-z0-9_])${fn}([^A-Za-z0-9_]|$)" <<<"$body"; then
    missing+=("$fn")
  fi
done <<<"$new_defs"

if (( ${#missing[@]} )); then
  echo "install.sh calls lib-common function(s) missing from $old_lib with no shim:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi
echo "ok: every lib-common function install.sh calls exists in $old_lib (or is shimmed)"
