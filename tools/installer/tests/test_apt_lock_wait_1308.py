"""Every apt call in the installer scripts waits for the apt/dpkg lock (#1308, #1299)."""
import os
import re
import stat
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
LIB = HERE / "lib-common.sh"
AGENT_INSTALL = HERE.parents[1] / "agent" / "install" / "install.sh"
SCRIPTS = ["install.sh", "install-influxdb.sh", "lib-common.sh", "uninstall.sh", AGENT_INSTALL]
UNINSTALL = (HERE / "uninstall.sh").read_text()


def apt_invocations(name):
    """Lines that run apt-get through $SUDO (comments and hint strings excluded)."""
    out = []
    for n, ln in enumerate((HERE / name).read_text().splitlines(), 1):
        if ln.lstrip().startswith("#"):
            continue
        if re.search(r"\$SUDO apt-get\b", ln):
            out.append((n, ln))
    return out


def test_every_sudo_apt_get_waits_for_the_lock():
    bad = []
    for name in SCRIPTS:
        for n, ln in apt_invocations(name):
            if "DPkg::Lock::Timeout" not in ln and "_APT_LOCK[@]" not in ln:
                bad.append(f"{name}:{n}: {ln.strip()}")
    assert not bad, "apt-get without a lock wait:\n" + "\n".join(bad)


def test_install_paths_use_the_wrapper():
    influx = (HERE / "install-influxdb.sh").read_text()
    lib = LIB.read_text()
    assert "apt_get install -y --no-install-recommends influxdb2 influxdb2-cli" in influx
    assert "apt_get install -y --no-install-recommends \"${pkgs[@]}\"" in lib
    assert "$(apt_get update 2>&1)" in lib


def test_agent_installer_shows_apt_error_on_failure():
    blk = AGENT_INSTALL.read_text()
    blk = blk[blk.index("_offer_apt_install() {"):blk.index("── Host hardware collectors")]
    assert ">/dev/null 2>&1; then" not in blk.split("install -y $pkgs", 1)[1].split("\n", 1)[0]
    assert 'printf \'%s\\n\' "$_out"' in blk


def test_uninstall_shows_package_manager_errors():
    blk = UNINSTALL[UNINSTALL.index('if [[ "$_PKG" == "dnf" ]]; then'):UNINSTALL.index('_influx_resid=""')]
    assert "apt-get" in blk and "2>/dev/null" not in re.sub(r"dpkg --purge[^\n]*|dpkg-query[^\n]*", "", blk)
    assert "apt purge failed" in blk and "dnf remove failed" in blk
    assert "apt-get purge influxdb2 influxdb2-cli" in blk
    assert "dnf remove influxdb2 influxdb2-cli" in blk


def fake(b, name, body="exit 0"):
    b.mkdir(exist_ok=True)
    f = b / name
    f.write_text(f"#!/usr/bin/env bash\n{body}\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


def run(script, path, extra_env=None):
    env = dict(os.environ, PATH=path)
    env.pop("LLMSYS_APT_LOCK_TIMEOUT", None)
    env.update(extra_env or {})
    return subprocess.run(["/bin/bash", "-c", f'. "{LIB}"; SUDO=""; {script}'],
                          env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_apt_get_wrapper_passes_lock_timeout(tmp_path):
    b = tmp_path / "bin"
    fake(b, "apt-get", 'printf "%s\\n" "$*"')
    fake(b, "fuser", "exit 1")
    r = run("apt_get install -y foo", f"{b}:{os.environ['PATH']}")
    assert r.returncode == 0, r.stderr
    assert "-o DPkg::Lock::Timeout=300 install -y foo" in r.stdout
    assert "another package manager" not in r.stdout + r.stderr
    r = run("apt_get update", f"{b}:{os.environ['PATH']}", {"LLMSYS_APT_LOCK_TIMEOUT": "7"})
    assert "-o DPkg::Lock::Timeout=7 update" in r.stdout


def test_apt_get_wrapper_announces_a_held_lock(tmp_path):
    b = tmp_path / "bin"
    fake(b, "apt-get", "exit 0")
    fake(b, "fuser", "exit 0")
    r = run("apt_get install -y foo", f"{b}:{os.environ['PATH']}")
    assert r.returncode == 0, r.stderr
    assert "waiting for another package manager to finish" in r.stdout + r.stderr


def test_scripts_parse():
    for name in SCRIPTS:
        assert subprocess.run(["bash", "-n", str(HERE / name)]).returncode == 0
