# agent/tests/test_selfupdate_svcconfig_guard.py
"""#307/#313: self-update must refuse to deploy new code on a llama host that
lacks the root-owned svcconfig helper (any configured unit name), but must NOT
block a non-llama host, and the root --update must install the helper BEFORE it
deploys new code."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"


def _extract(name_or_start: str, is_func: bool) -> str:
    lines = INSTALL_SH.read_text().splitlines()
    if is_func:
        s = next(i for i, l in enumerate(lines) if l.startswith(name_or_start + "()"))
        e = next(i for i in range(s + 1, len(lines)) if lines[i] == "}")
    else:
        s = next(i for i, l in enumerate(lines) if l.strip() == name_or_start)
        e = next(i for i in range(s + 1, len(lines)) if lines[i] == "  fi")
    return "\n".join(lines[s:e + 1])


GUARD = _extract('_lu_guard="$(_resolved_llama_unit)"', is_func=False)
YAML_SCALAR = _extract("_yaml_scalar", is_func=True)


def _run(from_self, unit_name, unit_present, wrap, llama_enabled, tmp_path):
    unitdir = tmp_path / "systemd"; unitdir.mkdir()
    if unit_present:
        (unitdir / unit_name).write_text("[Service]\n")
    installdir = tmp_path / "install"; installdir.mkdir()
    cfg = installdir / "agent_config.yaml"
    cfg.write_text("" if llama_enabled is None else f"LLAMA_ENABLED: {llama_enabled}\n")
    block = GUARD.replace("/etc/systemd/system/", f"{unitdir}/") \
                 .replace("/usr/local/sbin/llm-svcconfig-apply", "$WRAP")
    script = (
        "set -u\n"
        f"{YAML_SCALAR}\n"
        '_resolved_llama_unit() { printf "%s" "$UNITNAME"; }\n'
        'AGENT_OS="linux"\n'
        f"FROM_SELF_UPDATE={from_self}\n"
        "SRC_DIR=/tmp/staged\n"
        f'INSTALL_DIR="{installdir}"\n'
        f'UNITNAME="{unit_name}"\n'
        f'WRAP="{wrap or "/nonexistent/wrap"}"\n'
        f"{block}\n"
        "echo PROCEEDED\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.fixture
def wrapper(tmp_path):
    p = tmp_path / "llm-svcconfig-apply"; p.write_text("#!/bin/sh\n"); p.chmod(0o755); return p


@pytest.mark.parametrize("unit_name", ["llama_server.service", "my-llama.service", "llama@gpu0.service"])
def test_aborts_on_llama_host_missing_wrapper_any_unit_name(unit_name, tmp_path):
    r = _run("true", unit_name, True, None, "true", tmp_path)
    assert r.returncode == 1 and "one-time root migration" in r.stderr


def test_proceeds_when_wrapper_present(wrapper, tmp_path):
    r = _run("true", "my-llama.service", True, wrapper, "true", tmp_path)
    assert r.returncode == 0 and "PROCEEDED" in r.stdout


def test_proceeds_on_non_llama_host_no_unit_file(tmp_path):
    r = _run("true", "my-llama.service", False, None, "true", tmp_path)
    assert r.returncode == 0 and "PROCEEDED" in r.stdout


def test_llama_disabled_with_stray_unit_file_does_not_block(tmp_path):
    # #313: LLAMA_ENABLED:false + a leftover unit file must NOT abort self-update.
    r = _run("true", "llama_server.service", True, None, "false", tmp_path)
    assert r.returncode == 0 and "PROCEEDED" in r.stdout


def test_unknown_llama_enabled_still_guards(tmp_path):
    # Missing/unreadable LLAMA_ENABLED => conservative: still fire the guard.
    r = _run("true", "llama_server.service", True, None, None, tmp_path)
    assert r.returncode == 1 and "one-time root migration" in r.stderr


def test_does_not_block_root_update(tmp_path):
    r = _run("false", "my-llama.service", True, None, "true", tmp_path)
    assert r.returncode == 0 and "PROCEEDED" in r.stdout


def test_root_update_installs_wrapper_before_deploying_code():
    # #313: the privileged wrapper/sudoers install must precede the code copy so
    # a failure never leaves wrapper-dependent code on disk.
    text = INSTALL_SH.read_text()
    apply_at = text.index('_apply_sudoers_and_wrapper || exit 1')      # first (root --update)
    deploy_at = text.index('_section "Updating agent code"')
    assert apply_at < deploy_at
