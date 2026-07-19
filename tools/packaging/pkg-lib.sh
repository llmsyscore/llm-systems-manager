#!/bin/bash
# Shared post-build helpers sourced by build-packages.sh and
# build-agent-package.sh.

# Deletes the bare ./opt/ member from a .deb's data.tar so dpkg never records
# /opt as package-owned — purge would rmdir an emptied /opt (#432).
strip_deb_opt_entry() {
  local deb="$1" work
  deb="$(cd "$(dirname "$deb")" && pwd)/$(basename "$deb")"
  work="$(mktemp -d)"
  (
    cd "$work" || exit 1
    ar x "$deb" data.tar.gz
    gunzip data.tar.gz
    tar --no-recursion --delete -f data.tar ./opt/
    gzip -9n data.tar
    ar r "$deb" data.tar.gz
  )
  rm -rf "$work"
  # Listing captured once: grep -q on a live dpkg-deb pipe would SIGPIPE it
  # under pipefail.
  if command -v dpkg-deb >/dev/null 2>&1; then
    local listing
    listing="$(dpkg-deb -c "$deb")"
    if grep -qE ' \./opt/$' <<<"$listing"; then
      echo "ERROR: $deb still owns the bare /opt directory" >&2
      return 1
    fi
    if ! grep -qE ' \./opt/[^/]+/$' <<<"$listing"; then
      echo "ERROR: $deb lost its /opt payload while stripping" >&2
      return 1
    fi
  fi
}

# Fails when a .deb ships development/CI files with no runtime use (#432).
assert_deb_payload_clean() {
  local deb="$1" bad
  command -v dpkg-deb >/dev/null 2>&1 || return 0
  bad="$(dpkg-deb -c "$deb" | grep -E \
    '/(docker|design|devel|docs/screenshots|tools/packaging)(/|$)|docker-compose\.yml|\.env\.example|\.dockerignore|/ci-[a-z0-9-]+\.sh' \
    || true)"
  if [[ -n "$bad" ]]; then
    echo "ERROR: $deb ships development files:" >&2
    printf '%s\n' "$bad" >&2
    return 1
  fi
}
