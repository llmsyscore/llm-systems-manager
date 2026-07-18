#!/usr/bin/env bash
# Builds the llm-systems-agent .deb/.rpm around a prebuilt PyInstaller
# binary (arch-specific; built by agent-binaries.yml).
#
# Usage: build-agent-package.sh --binary PATH --version X.Y.Z
#          [--arch amd64|arm64] [--formats deb,rpm|none] [--out DIR]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=pkg-lib.sh
. "$HERE/pkg-lib.sh"

PKG_NAME="llm-systems-agent"
AGENT_DIR="/opt/llm-systems-agent"
RUN_USER="llmsys"
PKG_URL="https://github.com/llmsyscore/llm-systems-manager"
PKG_LICENSE="AGPL-3.0"
PKG_MAINTAINER="llmsyscore <llmsyscore@users.noreply.github.com>"
PKG_DESCRIPTION="LLM Systems Agent — self-contained monitoring/control agent for LLM inference hosts.
Installs the single-file binary under /opt/llm-systems-agent with a
systemd unit; config is generated at install time."

BINARY=""
VERSION=""
ARCH="amd64"
FORMATS="deb,rpm"
OUT_DIR="$REPO_ROOT/dist"

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --binary)  BINARY="${2:?}"; shift 2 ;;
    --version) VERSION="${2:?}"; shift 2 ;;
    --arch)    ARCH="${2:?}"; shift 2 ;;
    --formats) FORMATS="${2:?}"; shift 2 ;;
    --out)     OUT_DIR="${2:?}"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -f "$BINARY" ]] || die "--binary PATH required (PyInstaller output)"
[[ -n "$VERSION" ]] || die "--version required"
command -v python3 >/dev/null 2>&1 || die "python3 required"
if [[ ",$FORMATS," == *,deb,* || ",$FORMATS," == *,rpm,* ]]; then
  command -v fpm >/dev/null 2>&1 || die "fpm not found (gem install fpm)"
fi
if [[ ",$FORMATS," == *,rpm,* ]]; then
  command -v rpmbuild >/dev/null 2>&1 || die "rpmbuild required for rpm (apt-get install rpm)"
fi

case "$ARCH" in
  amd64) RPM_ARCH="x86_64" ;;
  arm64) RPM_ARCH="aarch64" ;;
  *) die "unsupported --arch '$ARCH' (amd64|arm64)" ;;
esac

VERSION="${VERSION#v}"
VERSION="${VERSION//-/\~}"
[[ "$VERSION" =~ ^[0-9][A-Za-z0-9.~+]*$ ]] || die "version '$VERSION' is not a valid package version"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
mkdir -p "$STAGE$AGENT_DIR" "$STAGE/usr/lib/systemd/system" "$OUT_DIR"

install -m 0755 "$BINARY" "$STAGE$AGENT_DIR/llm-systems-agent"
install -m 0644 "$REPO_ROOT/agent/agent_config.yaml.example" "$STAGE$AGENT_DIR/agent_config.yaml.example"

# Renders the binary unit template's ${AGENT_*} tokens with package defaults.
python3 - "$REPO_ROOT/agent/install/llm-systems-agent-binary.service.tmpl" \
          "$STAGE/usr/lib/systemd/system/llm-systems-agent.service" \
          "$AGENT_DIR" "$RUN_USER" <<'PY'
import pathlib, sys
src, dst, agent_dir, run_user = sys.argv[1:5]
text = pathlib.Path(src).read_text()
for token, value in (("${AGENT_USER}", run_user),
                     ("${AGENT_GROUP}", run_user),
                     ("${AGENT_INSTALL_DIR}", agent_dir)):
    text = text.replace(token, value)
pathlib.Path(dst).write_text(text)
PY
chmod 0644 "$STAGE/usr/lib/systemd/system/llm-systems-agent.service"

SCRIPTS="$WORK/scripts"
mkdir -p "$SCRIPTS"
for s in preinst postinst prerm postrm; do
  cat "$HERE/agent/scripts/common.sh" "$HERE/agent/scripts/deb/$s" > "$SCRIPTS/deb-$s"
  chmod 0755 "$SCRIPTS/deb-$s"
done
for s in pre post preun postun; do
  cat "$HERE/agent/scripts/common.sh" "$HERE/agent/scripts/rpm/$s" > "$SCRIPTS/rpm-$s"
  chmod 0755 "$SCRIPTS/rpm-$s"
done
bash -n "$SCRIPTS"/deb-* "$SCRIPTS"/rpm-*

COMMON_ARGS=(
  -s dir -n "$PKG_NAME" -v "$VERSION"
  --description "$PKG_DESCRIPTION"
  --url "$PKG_URL" --license "$PKG_LICENSE" --maintainer "$PKG_MAINTAINER"
  -C "$STAGE"
)

BUILT=()

if [[ ",$FORMATS," == *,deb,* ]]; then
  DEB_OUT="$OUT_DIR/${PKG_NAME}_${VERSION}_${ARCH}.deb"
  rm -f "$DEB_OUT"
  fpm -t deb -a "$ARCH" -p "$DEB_OUT" \
    --depends ca-certificates \
    --deb-priority optional --category admin \
    --deb-compression gz \
    --before-install "$SCRIPTS/deb-preinst" \
    --after-install "$SCRIPTS/deb-postinst" \
    --before-remove "$SCRIPTS/deb-prerm" \
    --after-remove "$SCRIPTS/deb-postrm" \
    --deb-config "$HERE/agent/debconf/config" \
    --deb-templates "$HERE/agent/debconf/templates" \
    --deb-no-default-config-files \
    "${COMMON_ARGS[@]}" .
  strip_deb_opt_entry "$DEB_OUT"
  BUILT+=("$DEB_OUT")
fi

if [[ ",$FORMATS," == *,rpm,* ]]; then
  RPM_OUT="$OUT_DIR/${PKG_NAME}-${VERSION}-1.${RPM_ARCH}.rpm"
  rm -f "$RPM_OUT"
  fpm -t rpm -a "$RPM_ARCH" -p "$RPM_OUT" \
    --depends ca-certificates \
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
