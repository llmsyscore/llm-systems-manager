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
