import subprocess
from pathlib import Path

UNINSTALL_PATH = Path(__file__).resolve().parents[1] / "uninstall.sh"
SMOKE_PATH = Path(__file__).resolve().parents[1] / "ci-uninstall-smoke.sh"
UNINSTALL = UNINSTALL_PATH.read_text()
SMOKE = SMOKE_PATH.read_text()


def test_uninstall_detects_influx_via_rpm():
    assert "rpm -q influxdb2 >/dev/null 2>&1" in UNINSTALL


def test_uninstall_dnf_remove_and_repo_file():
    assert "$SUDO dnf remove -y influxdb2 influxdb2-cli" in UNINSTALL
    assert "/etc/yum.repos.d/influxdata.repo" in UNINSTALL


def test_uninstall_dpkg_recovery_only_on_apt():
    blk = UNINSTALL[UNINSTALL.index('if [[ "$_PKG" == "dnf" ]]; then'):UNINSTALL.index('_influx_resid=""')]
    dnf_part, apt_part = blk.split("else", 1)
    assert "dpkg --purge" not in dnf_part
    assert "dpkg --purge" in apt_part


def test_uninstall_dpkg_query_guarded_in_detection():
    assert "command -v dpkg-query >/dev/null 2>&1 && dpkg-query" in UNINSTALL


def test_ci_uninstall_smoke_uses_rpm_on_dnf():
    assert 'rpm -q "$pkg"' in SMOKE and "command -v dpkg-query" in SMOKE


def test_both_scripts_parse():
    for s in (UNINSTALL_PATH, SMOKE_PATH):
        assert subprocess.run(["bash", "-n", str(s)]).returncode == 0


def test_residual_rpm_state_text_agrees():
    assert "residual rpm state" in UNINSTALL and "residual rpm state" in SMOKE
