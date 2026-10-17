"""Package-manager detection and apt-name dispatch in lib-common.sh (#1236)."""
import os
import stat
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib-common.sh"


def fake(b, name, body="exit 0"):
    b.mkdir(exist_ok=True)
    f = b / name
    f.write_text(f"#!/usr/bin/env bash\n{body}\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


def run(script, path, extra_env=None, stdin=subprocess.DEVNULL):
    env = dict(os.environ, PATH=path)
    env.pop("LLMSYS_PKG_MGR", None)
    env.update(extra_env or {})
    return subprocess.run(["/bin/bash", "-c", f'. "{LIB}"; {script}'],
                          env=env, capture_output=True, text=True, stdin=stdin)


def detect(b, only_bin=False):
    path = str(b) if only_bin else f"{b}:{os.environ['PATH']}"
    r = run('detect_pkg_mgr; printf "%s" "$LLMSYS_PKG_MGR"', path)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_apt_wins_when_both_present(tmp_path):
    b = tmp_path / "bin"
    fake(b, "apt-get")
    fake(b, "dnf")
    assert detect(b) == "apt"


def test_dnf_when_apt_absent(tmp_path):
    b = tmp_path / "bin"
    fake(b, "dnf")
    assert detect(b, only_bin=True) == "dnf"


def test_empty_when_neither(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    assert detect(b, only_bin=True) == ""


def test_map_drops_venv_expands_build_essential():
    r = run("map_pkgs_for_dnf python3 python3-venv python3-dev build-essential "
            "sqlite3 gnupg jq foo", os.environ["PATH"])
    assert r.stdout.split() == ["python3", "python3-devel", "gcc", "gcc-c++",
                                "make", "sqlite", "gnupg2", "jq", "foo"]


def test_missing_pkgs_uses_rpm_on_dnf(tmp_path):
    """Fake rpm succeeds only for `-q --whatprovides jq`; a name-only `rpm -q jq` exits 1."""
    b = tmp_path / "bin"
    fake(b, "rpm", '[[ "$*" == "-q --whatprovides jq" ]] && exit 0; exit 1')
    r = run("missing_apt_pkgs jq python3-dev", f"{b}:{os.environ['PATH']}",
            {"LLMSYS_PKG_MGR": "dnf"})
    assert r.stdout.split() == ["python3-devel"]


def test_offer_install_non_tty_prints_dnf_command():
    r = run("offer_apt_install python3-dev", os.environ["PATH"],
            {"LLMSYS_PKG_MGR": "dnf"})
    assert r.returncode == 1
    assert "sudo dnf install -y python3-devel" in r.stderr
    assert "apt-get" not in r.stderr


def test_apt_update_once_is_noop_on_dnf(tmp_path):
    b = tmp_path / "bin"
    marker = tmp_path / "marker"
    fake(b, "apt-get", f'touch "{marker}"')
    r = run("apt_update_once", f"{b}:{os.environ['PATH']}", {"LLMSYS_PKG_MGR": "dnf"})
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
