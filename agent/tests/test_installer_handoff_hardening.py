# agent/tests/test_installer_handoff_hardening.py
# Token handoff parsed not sourced + non-/tmp fallback (#326), atomic
# collectors/providers swap (#298), userdel guard in uninstall (#300).
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
AGENT_INSTALL_SH = AGENT_DIR / "install" / "install.sh"
LIB_COMMON_SH = REPO_ROOT / "tools" / "installer" / "lib-common.sh"
BOOTSTRAP_SH = REPO_ROOT / "tools" / "installer" / "install-config-bootstrap.sh"
UNINSTALL_SH = REPO_ROOT / "tools" / "installer" / "uninstall.sh"


def _extract_func(source: Path, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", source.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from {source.name}"
    return m.group(0)


def _bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, env=env)


# ── #326: token handoff file must be parsed, never executed ──────────────────

STUBS = 'warn() { echo "WARN: $*" >&2; }\nok() { :; }\ndie() { echo "$*" >&2; exit 1; }\nSUDO=""\n'


def test_bootstrap_never_sources_token_file():
    text = BOOTSTRAP_SH.read_text()
    assert "source <(" not in text, "token env file is still executed via source"
    assert re.search(r"\bread_influx_token_file\b", text), \
        "bootstrap does not use the strict token-file parser"


def test_token_file_fallback_not_in_tmp():
    text = LIB_COMMON_SH.read_text()
    m = re.search(r'LLMSYS_INFLUXDB_TOKEN_FILE:=([^}]+)\}', text)
    assert m, "LLMSYS_INFLUXDB_TOKEN_FILE default missing from lib-common.sh"
    assert not m.group(1).startswith("/tmp/"), \
        f"fallback token path still under /tmp: {m.group(1)}"


def test_parser_does_not_execute_hostile_content(tmp_path):
    pwn = tmp_path / "pwned"
    env_file = tmp_path / "influxdb.env"
    env_file.write_text(
        "# comment\n"
        f"PATH=/nonexistent\n"
        f"INFLUX_HOST=$(touch {pwn})\n"
        f"INFLUX_ORG=`touch {pwn}`\n"
        f"touch {pwn}\n"
        f"INFLUX_METRICS_TOKEN=tok-metrics; touch {pwn}\n"
        "EVIL=1\n")
    func = _extract_func(LIB_COMMON_SH, "read_influx_token_file")
    r = _bash(
        STUBS + func + "\n"
        f'read_influx_token_file "{env_file}"\n'
        'printf "HOST=%s\\nORG=%s\\nMET=%s\\nEVIL=%s\\n" '
        '"$INFLUX_HOST" "$INFLUX_ORG" "$INFLUX_METRICS_TOKEN" "${EVIL:-unset}"\n')
    assert r.returncode == 0, r.stderr
    assert not pwn.exists(), "token file contents were executed as shell code"
    out = dict(l.split("=", 1) for l in r.stdout.splitlines())
    # Hostile substitutions survive only as literal text, never expanded.
    assert out["HOST"] == f"$(touch {pwn})"
    assert out["ORG"] == f"`touch {pwn}`"
    assert out["MET"] == f"tok-metrics; touch {pwn}"
    assert out["EVIL"] == "unset", "unexpected key was assigned"


def test_writer_reader_roundtrip(tmp_path):
    env_file = tmp_path / "influxdb.env"
    funcs = (_extract_func(LIB_COMMON_SH, "write_influx_token_file") + "\n"
             + _extract_func(LIB_COMMON_SH, "read_influx_token_file"))
    r = _bash(
        STUBS + funcs + "\n"
        f'write_influx_token_file "{env_file}" '
        '"http://192.0.2.7:8086" "llm-systems-manager" "op-tok==" "met-tok" "roll-tok"\n'
        f'read_influx_token_file "{env_file}"\n'
        'printf "%s|%s|%s|%s|%s\\n" "$INFLUX_HOST" "$INFLUX_ORG" '
        '"$INFLUX_OPERATOR_TOKEN" "$INFLUX_METRICS_TOKEN" "$INFLUX_METRICS_ROLLUP_TOKEN"\n')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == \
        "http://192.0.2.7:8086|llm-systems-manager|op-tok==|met-tok|roll-tok"
    assert (env_file.stat().st_mode & 0o777) == 0o600


def test_writer_rejects_newline_values(tmp_path):
    env_file = tmp_path / "influxdb.env"
    func = _extract_func(LIB_COMMON_SH, "write_influx_token_file")
    r = _bash(
        STUBS + func + "\n"
        f'write_influx_token_file "{env_file}" "http://h:8086" "org" '
        '"tok\nINFLUX_METRICS_TOKEN=injected" "met" "roll"\n')
    assert r.returncode != 0, "newline-embedded value must be refused"


# ── #298: collectors/providers refresh must not delete before copy ──────────

def _pkg_swap_loop() -> str:
    m = re.search(r'^  for _pkg in collectors providers; do\n.*?^  done$',
                  AGENT_INSTALL_SH.read_text(), re.MULTILINE | re.DOTALL)
    assert m, "could not extract the collectors/providers refresh loop"
    return m.group(0)


def _pkg_swap_setup(tmp_path):
    src = tmp_path / "src"
    inst = tmp_path / "inst"
    for pkg in ("collectors", "providers"):
        (src / pkg).mkdir(parents=True)
        (src / pkg / "new.py").write_text("new")
        (inst / pkg).mkdir(parents=True)
        (inst / pkg / "old.py").write_text("old")
    user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    group = subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip()
    script = ("set -e\nSUDO=\"\"\n"
              f'SRC_DIR="{src}"\nINSTALL_DIR="{inst}"\n'
              f'USER_ARG="{user}"\nUSER_GROUP="{group}"\n'
              + _pkg_swap_loop() + "\n")
    return inst, script


def test_pkg_swap_survives_failed_copy(tmp_path):
    inst, script = _pkg_swap_setup(tmp_path)
    binroot = tmp_path / "bin"
    binroot.mkdir()
    (binroot / "cp").write_text("#!/bin/bash\necho 'cp: disk full' >&2\nexit 1\n")
    os.chmod(binroot / "cp", 0o755)
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    r = _bash(script, env=env)
    assert r.returncode != 0, "loop must abort when cp fails"
    for pkg in ("collectors", "providers"):
        assert (inst / pkg / "old.py").exists(), \
            f"{pkg}/ was wiped before the replacement copy landed"


def test_pkg_swap_replaces_content_on_success(tmp_path):
    inst, script = _pkg_swap_setup(tmp_path)
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    for pkg in ("collectors", "providers"):
        assert (inst / pkg / "new.py").exists()
        assert not (inst / pkg / "old.py").exists()
        assert not (inst / f"{pkg}.new").exists(), "staging dir left behind"


def _py_swap_loop() -> str:
    m = re.search(r'^  for _f in llm-systems-agent\.py .*?^  done$',
                  AGENT_INSTALL_SH.read_text(), re.MULTILINE | re.DOTALL)
    assert m, "could not extract the top-level .py refresh loop"
    return m.group(0)


def test_py_swap_survives_failed_copy(tmp_path):
    src = tmp_path / "src"
    inst = tmp_path / "inst"
    src.mkdir()
    inst.mkdir()
    (src / "llm-systems-agent.py").write_text("new")
    (inst / "llm-systems-agent.py").write_text("old")
    binroot = tmp_path / "bin"
    binroot.mkdir()
    (binroot / "cp").write_text("#!/bin/bash\necho 'cp: disk full' >&2\nexit 1\n")
    os.chmod(binroot / "cp", 0o755)
    user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    group = subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip()
    script = ("set -e\nSUDO=\"\"\n"
              f'SRC_DIR="{src}"\nINSTALL_DIR="{inst}"\n'
              f'USER_ARG="{user}"\nUSER_GROUP="{group}"\n'
              + _py_swap_loop() + "\n")
    env = dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}")
    r = _bash(script, env=env)
    assert r.returncode != 0, "loop must abort when cp fails"
    assert (inst / "llm-systems-agent.py").read_text() == "old", \
        "live agent .py was truncated/replaced by a failed copy"


# ── #300: uninstall must refuse to userdel a regular login account ──────────

def _run_guard(tmp_path, uid: int, os_name: str = "linux",
               uid_min: str | None = "UID_MIN 1000") -> int:
    binroot = tmp_path / f"bin-{uid}-{os_name}"
    binroot.mkdir()
    (binroot / "id").write_text(f"#!/bin/bash\necho {uid}\n")
    os.chmod(binroot / "id", 0o755)
    logindefs = tmp_path / f"login.defs-{uid}"
    if uid_min is not None:
        logindefs.write_text(f"# comment\n{uid_min}\n")
    func = _extract_func(UNINSTALL_SH, "_service_account_uid_ok")
    func = func.replace("/etc/login.defs", str(logindefs))
    r = _bash(f'OS={os_name}\n{func}\n_service_account_uid_ok someuser\n',
              env=dict(os.environ, PATH=f"{binroot}:{os.environ['PATH']}"))
    return r.returncode


def test_guard_allows_system_uid(tmp_path):
    assert _run_guard(tmp_path, 998) == 0


def test_guard_refuses_regular_user(tmp_path):
    assert _run_guard(tmp_path, 1000) != 0
    assert _run_guard(tmp_path, 1001) != 0


def test_guard_refuses_root(tmp_path):
    assert _run_guard(tmp_path, 0) != 0


def test_guard_honors_login_defs_uid_min(tmp_path):
    # Custom UID_MIN 2000: uid 1500 is still a system account there.
    assert _run_guard(tmp_path, 1500, uid_min="UID_MIN 2000") == 0
    # Missing/garbage login.defs falls back to 1000.
    assert _run_guard(tmp_path, 999, uid_min=None) == 0
    assert _run_guard(tmp_path, 1000, uid_min=None) != 0


def test_guard_macos_threshold(tmp_path):
    # macOS human accounts start at 501.
    assert _run_guard(tmp_path, 499, os_name="macos") == 0
    assert _run_guard(tmp_path, 501, os_name="macos") != 0


def test_both_userdel_sites_are_guarded():
    text = UNINSTALL_SH.read_text()
    assert text.count("userdel -r") + text.count("dscl . -delete") <= 2, \
        "unexpected extra account-deletion sites"
    # Every deletion must flow through the single guarded helper.
    assert re.search(r"_offer_delete_run_user\b", text), \
        "guarded delete helper missing"
    helper = _extract_func(UNINSTALL_SH, "_offer_delete_run_user")
    assert "userdel -r" in helper and "dscl . -delete" in helper
    outside = text.replace(helper, "")
    assert "userdel -r" not in outside and "dscl . -delete" not in outside, \
        "account deletion exists outside the guarded helper"
