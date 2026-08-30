"""install.sh flag/env handling that runs before any network or root step."""
import os
import subprocess
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
TEXT = INSTALL.read_text()


def run(args, env_extra=None):
    env = dict(os.environ)
    for k in ("LLMSYS_SOURCE", "LLMSYS_RELEASE_TAG", "LLMSYS_APT_STAMP"):
        env.pop(k, None)
    env.update(env_extra or {})
    return subprocess.run(["bash", str(INSTALL), *args], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_env_release_tag_conflict_names_env_origin():
    r = run(["--source", "git"], {"LLMSYS_RELEASE_TAG": "v1.2.3"})
    assert r.returncode == 1
    assert "LLMSYS_RELEASE_TAG (environment)=v1.2.3" in r.stderr
    assert "--source=git" in r.stderr
    assert "from the environment (no --ref flag given)" in r.stderr


def test_flag_release_tag_conflict_names_flag():
    r = run(["--ref", "v1.2.3", "--source", "git"])
    assert r.returncode == 1
    assert "--ref=v1.2.3" in r.stderr
    assert "environment" not in r.stderr


def test_env_source_conflict_names_env_origin():
    r = run(["--ref", "v1.2.3"], {"LLMSYS_SOURCE": "git"})
    assert r.returncode == 1
    assert "LLMSYS_SOURCE (environment)=git" in r.stderr
    assert "using LLMSYS_SOURCE=git from the environment" in r.stderr


def test_invalid_env_source_names_env_origin():
    r = run(["--mode", "9"], {"LLMSYS_SOURCE": "bogus"})
    assert r.returncode == 1
    assert "LLMSYS_SOURCE (environment) must be" in r.stderr


def test_help_lists_primary_ip():
    r = run(["--help"])
    assert r.returncode == 0
    assert "--primary-ip ADDR" in r.stdout
    assert "LLMSYS_PRIMARY_IP" in r.stdout


def test_trampoline_runs_after_mode_selection():
    assert TEXT.index("# ── Mode selection") < TEXT.index("# ── Self-update trampoline")
    assert TEXT.index("# ── Self-update trampoline") < TEXT.index("# ── Update short-circuit")


def test_run_group_resolved_after_user_creation():
    i = TEXT.index('ensure_runas_user "$LLMSYS_RUN_USER"')
    assert TEXT.index('resolve_run_group "$LLMSYS_RUN_USER"', i) < TEXT.index("deploy_into_install_dir", i)


def test_preflight_apt_update_writes_stamp():
    i = TEXT.index("$SUDO apt-get update -qq")
    j = TEXT.index("apt-get install -y --no-install-recommends", i)
    assert "printf 'updated\\n' > \"$LLMSYS_APT_STAMP\"" in TEXT[i:j]


def test_primary_ip_must_be_ipv4():
    r = run(["--primary-ip", "bogus", "--mode", "9"])
    assert r.returncode == 1
    assert "must be an IPv4 address" in r.stderr
    r = run(["--mode", "9"], {"LLMSYS_PRIMARY_IP": "not-an-ip"})
    assert r.returncode == 1


# ── Interactive menu + self-update trampoline (#743 / #748) ─────────────────
import os as _os
import pty
import select
import shutil
import stat
import time

import pytest


def _prereqs_present():
    if not all(shutil.which(t) for t in ("jq", "rsync", "git", "curl", "python3")):
        return False
    return subprocess.run(["python3", "-m", "venv", "--help"],
                          capture_output=True).returncode == 0


def run_pty(args, env_extra=None, answer=None, prompt=b"Mode [1-9]:", timeout=60):
    """Run install.sh on a PTY; send `answer` once `prompt` shows; return (rc, output)."""
    env = dict(os.environ, TERM="dumb")
    for k in ("LLMSYS_SOURCE", "LLMSYS_RELEASE_TAG", "LLMSYS_APT_STAMP", "LLMSYS_SELF_UPDATE_DONE"):
        env.pop(k, None)
    env.update(env_extra or {})
    master, slave = pty.openpty()
    proc = subprocess.Popen(["bash", str(INSTALL), *args], stdin=slave, stdout=slave,
                            stderr=slave, env=env, close_fds=True)
    _os.close(slave)
    out = b""
    sent = answer is None
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                chunk = _os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not sent and prompt in out:
                _os.write(master, answer)
                sent = True
        elif proc.poll() is not None:
            break
    _os.close(master)
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -9
    return rc, out.decode("utf-8", "replace")


def _fake_upstream(tmp_path):
    """A file:// raw-base serving a 'newer' install.sh that just echoes its argv."""
    d = tmp_path / "up" / "tools" / "installer"
    d.mkdir(parents=True)
    f = d / "install.sh"
    f.write_text("#!/usr/bin/env bash\n_INSTALL_SH_REVISION=99999999999\n"
                 'rm -f "${LLMSYS_UPSTREAM_TMP:-}"\n'
                 'echo "FAKE-UPSTREAM ARGS: $*"\n')
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return f"file://{tmp_path / 'up'}"


@pytest.mark.skipif(not _prereqs_present(), reason="installer prereqs missing on this host")
def test_menu_quit_never_fetches_upstream(tmp_path):
    rc, out = run_pty([], {"LLMSYS_RAW_BASE_URL": _fake_upstream(tmp_path)}, answer=b"9\n")
    assert rc == 0, out
    assert "Quit selected" in out
    assert "newer install.sh upstream" not in out
    assert "FAKE-UPSTREAM" not in out


@pytest.mark.skipif(not _prereqs_present(), reason="installer prereqs missing on this host")
def test_menu_mode_is_forwarded_on_self_update(tmp_path):
    rc, out = run_pty([], {"LLMSYS_RAW_BASE_URL": _fake_upstream(tmp_path)}, answer=b"3\n")
    assert rc == 0, out
    assert "Selected mode 3" in out
    assert "newer install.sh upstream" in out
    assert "FAKE-UPSTREAM ARGS: --mode 3" in out


def test_explicit_mode_9_exits_before_trampoline(tmp_path):
    r = run(["--mode", "9"], {"LLMSYS_RAW_BASE_URL": _fake_upstream(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert "Quit selected" in r.stdout
    assert "FAKE-UPSTREAM" not in r.stdout + r.stderr


def test_explicit_mode_reexec_keeps_argv(tmp_path):
    r = run(["--mode", "3", "--user", "svc"], {"LLMSYS_RAW_BASE_URL": _fake_upstream(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert "FAKE-UPSTREAM ARGS: --mode 3 --user svc" in r.stdout


def test_shim_defines_detect_primary_ip_when_lib_common_lacks_it():
    assert "if ! declare -F detect_primary_ip >/dev/null; then" in TEXT
    r = subprocess.run(["bash", "-c",
                        "LLMSYS_PRIMARY_IP=10.1.1.1; " + TEXT.split("# Compat shims:")[1].split("\nfi\n")[0]
                        .split("\n", 2)[2] + "\nfi\ndetect_primary_ip"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "10.1.1.1"
