#!/usr/bin/env bash
# Builds the llm-systems-manager .deb/.rpm (manager + alarm engine) with fpm
# from the committed tree (HEAD). See docs/DEPLOYMENT.md for the install side.
#
# Usage: build-packages.sh [--version X.Y.Z] [--formats deb,rpm|none] [--out DIR]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

PKG_NAME="llm-systems-manager"
INSTALL_DIR="/opt/llm-systems-manager"
RUN_USER="llmsys"
PKG_URL="https://github.com/llmsyscore/llm-systems-manager"
PKG_LICENSE="AGPL-3.0"
PKG_MAINTAINER="llmsyscore <llmsyscore@users.noreply.github.com>"
PKG_DESCRIPTION="LLM Systems Manager — dashboard + alarm engine for local LLM inference hosts.
Installs the manager and alarm engine under /opt/llm-systems-manager with
systemd units; Python venvs are built at configure time (network required)."

VERSION=""
FORMATS="deb,rpm"
OUT_DIR="$REPO_ROOT/dist"

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:?}"; shift 2 ;;
    --formats) FORMATS="${2:?}"; shift 2 ;;
    --out)     OUT_DIR="${2:?}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3 required"
if [[ ",$FORMATS," == *,deb,* || ",$FORMATS," == *,rpm,* ]]; then
  command -v fpm >/dev/null 2>&1 || die "fpm not found (gem install fpm)"
fi
if [[ ",$FORMATS," == *,rpm,* ]]; then
  command -v rpmbuild >/dev/null 2>&1 || die "rpmbuild required for rpm (apt-get install rpm)"
fi

if [[ -z "$VERSION" ]]; then
  VERSION="$(git -C "$REPO_ROOT" describe --tags --abbrev=0 2>/dev/null || true)"
  [[ -n "$VERSION" ]] || die "no git tag found — pass --version"
fi
# Hyphens (pre-release tags like 1.0.6-rc1) become '~' — valid in both
# formats and sorts before the final release.
VERSION="${VERSION#v}"
VERSION="${VERSION//-/\~}"
[[ "$VERSION" =~ ^[0-9][A-Za-z0-9.~+]*$ ]] || die "version '$VERSION' is not a valid package version"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
APP_STAGE="$STAGE$INSTALL_DIR"
mkdir -p "$APP_STAGE" "$OUT_DIR"

# Stage tracked files only — never live config, venvs, or untracked files.
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$APP_STAGE"

# Mirror deploy_into_install_dir()'s exclusion list (lib-common.sh).
rm -rf "$APP_STAGE/.github"
find "$APP_STAGE" -depth -type d \
  \( -name tests -o -name test -o -name data -o -name backups -o -name plans \
     -o -name venv -o -name __pycache__ -o -name node_modules \
     -o -name .pytest_cache -o -name .claude \) -exec rm -rf {} +
find "$APP_STAGE" -type f \
  \( -name pytest.ini -o -name requirements-dev.txt \
     -o -name .gitignore -o -name .gitattributes -o -name .llmsys-release \) -delete

# Renders @@INSTALL_DIR@@ / @@RUN_USER@@ / @@RUN_GROUP@@ into a template.
render_template() {
  python3 - "$1" "$2" "$INSTALL_DIR" "$RUN_USER" <<'PY'
import pathlib, sys
src, dst, install_dir, run_user = sys.argv[1:5]
text = pathlib.Path(src).read_text()
for token, value in (("@@INSTALL_DIR@@", install_dir),
                     ("@@RUN_USER@@", run_user),
                     ("@@RUN_GROUP@@", run_user)):
    text = text.replace(token, value)
pathlib.Path(dst).write_text(text)
PY
}

UNIT_DIR="$STAGE/usr/lib/systemd/system"
mkdir -p "$UNIT_DIR" "$STAGE/etc/sudoers.d"
render_template "$APP_STAGE/systemd/llm-systems-manager.service.example" \
                "$UNIT_DIR/llm-systems-manager.service"
render_template "$APP_STAGE/llm-systems-alarm-engine/systemd/llm-systems-alarm-engine.service.example" \
                "$UNIT_DIR/llm-systems-alarm-engine.service"
render_template "$APP_STAGE/systemd/llm-systems-manager.sudoers.tmpl" \
                "$STAGE/etc/sudoers.d/llm-systems-manager"
chmod 0644 "$UNIT_DIR"/*.service
chmod 0440 "$STAGE/etc/sudoers.d/llm-systems-manager"
if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$STAGE/etc/sudoers.d/llm-systems-manager" >/dev/null \
    || die "rendered sudoers fragment failed visudo -c"
fi

# Maintainer scripts = common.sh + per-format body.
SCRIPTS="$WORK/scripts"
mkdir -p "$SCRIPTS"
for s in postinst prerm postrm; do
  cat "$HERE/scripts/common.sh" "$HERE/scripts/deb/$s" > "$SCRIPTS/deb-$s"
  chmod 0755 "$SCRIPTS/deb-$s"
done
for s in pre post preun postun; do
  cat "$HERE/scripts/common.sh" "$HERE/scripts/rpm/$s" > "$SCRIPTS/rpm-$s"
  chmod 0755 "$SCRIPTS/rpm-$s"
done
bash -n "$SCRIPTS"/deb-* "$SCRIPTS"/rpm-*

COMMON_ARGS=(
  -s dir -n "$PKG_NAME" -v "$VERSION"
  --description "$PKG_DESCRIPTION"
  --url "$PKG_URL" --license "$PKG_LICENSE" --maintainer "$PKG_MAINTAINER"
  --config-files /etc/sudoers.d/llm-systems-manager
  -C "$STAGE"
)

BUILT=()

if [[ ",$FORMATS," == *,deb,* ]]; then
  DEB_OUT="$OUT_DIR/${PKG_NAME}_${VERSION}_all.deb"
  rm -f "$DEB_OUT"
  fpm -t deb -a all -p "$DEB_OUT" \
    --depends 'python3 (>= 3.10)' --depends python3-venv --depends python3-pip \
    --depends ca-certificates --depends curl --depends jq \
    --depends sqlite3 --depends openssl \
    --deb-priority optional --category admin \
    --after-install "$SCRIPTS/deb-postinst" \
    --before-remove "$SCRIPTS/deb-prerm" \
    --after-remove "$SCRIPTS/deb-postrm" \
    --deb-config "$HERE/debconf/config" \
    --deb-templates "$HERE/debconf/templates" \
    --deb-no-default-config-files \
    "${COMMON_ARGS[@]}" .
  BUILT+=("$DEB_OUT")
fi

if [[ ",$FORMATS," == *,rpm,* ]]; then
  RPM_OUT="$OUT_DIR/${PKG_NAME}-${VERSION}-1.noarch.rpm"
  rm -f "$RPM_OUT"
  fpm -t rpm -a noarch -p "$RPM_OUT" \
    --depends python3 --depends python3-pip \
    --depends ca-certificates --depends /usr/bin/curl --depends jq \
    --depends sqlite --depends openssl \
    --rpm-os linux \
    --before-install "$SCRIPTS/rpm-pre" \
    --after-install "$SCRIPTS/rpm-post" \
    --before-remove "$SCRIPTS/rpm-preun" \
    --after-remove "$SCRIPTS/rpm-postun" \
    "${COMMON_ARGS[@]}" .
  BUILT+=("$RPM_OUT")
fi

for f in "${BUILT[@]}"; do
  (cd "$OUT_DIR" && sha256sum "$(basename "$f")" > "$(basename "$f").sha256")
done

echo "Built:"
printf '  %s\n' "${BUILT[@]}"
