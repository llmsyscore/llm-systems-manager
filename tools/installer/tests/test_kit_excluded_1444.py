"""The panel kit tooling (tools/kit) is left out of every install path: packages, installer copy and Docker image."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_build_packages_removes_tools_kit():
    assert '"$APP_STAGE/tools/kit"' in (REPO / "tools" / "packaging" / "build-packages.sh").read_text()


def test_deb_payload_check_flags_tools_kit():
    assert "|tools/kit|" in (REPO / "tools" / "packaging" / "pkg-lib.sh").read_text()


def test_installer_rsync_excludes_tools_kit():
    assert "--exclude='/tools/kit/'" in (REPO / "tools" / "installer" / "lib-common.sh").read_text()


def test_dockerignore_excludes_tools_kit():
    lines = (REPO / ".dockerignore").read_text().splitlines()
    assert "tools/kit" in lines
