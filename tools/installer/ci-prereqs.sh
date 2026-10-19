#!/usr/bin/env bash
# ci-prereqs.sh <apt-pkg...> — install CI prerequisites with apt-get or dnf.
# Package names are given in apt form; lib-common maps them for dnf.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tools/installer/lib-common.sh
. "$here/lib-common.sh"

(( $# > 0 )) || { echo "usage: $0 <apt-pkg...>" >&2; exit 2; }

detect_pkg_mgr
case "$LLMSYS_PKG_MGR" in
  apt)
    apt-get update -qq
    apt-get install -y --no-install-recommends "$@"
    ;;
  dnf)
    mapfile -t mapped < <(map_pkgs_for_dnf "$@")
    dnf install -y "${mapped[@]}"
    ;;
  *)
    echo "no apt-get or dnf on this machine" >&2; exit 1
    ;;
esac
