# agent/tests/test_selfupdate_svcconfig_guard.py
"""#307: self-update must refuse to deploy new code on a llama host that
hasn't had the root-owned svcconfig helper installed yet — for ANY configured
unit name, not just the default llama_server.service."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"


def _extract_guard() -> str:
    lines = INSTALL_SH.read_text().splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.strip() == '_lu_guard="$(_resolved_llama_unit)"')
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "  fi")
    return "\n".join(lines[start:end + 1])


GUARD = _extract_guard()


def _run(from_self: str, unit_name: str, unit_present: bool, wrap, tmp_path):
    unitdir = tmp_path / "systemd"; unitdir.mkdir()
    if unit_present:
        (unitdir / unit_name).write_text("[Service]\n")
    block = GUARD.replace("/etc/systemd/system/", f"{unitdir}/") \
                 .replace("/usr/local/sbin/llm-svcconfig-apply", "$WRAP")
    script = (
        "set -u\n"
        '_resolved_llama_unit() { printf "%s" "$UNITNAME"; }\n'
        'AGENT_OS="linux"\n'
        f"FROM_SELF_UPDATE={from_self}\n"
        "SRC_DIR=/tmp/staged\n"
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
    r = _run("true", unit_name, True, None, tmp_path)
    assert r.returncode == 1
    assert "PROCEEDED" not in r.stdout
    assert "one-time root migration" in r.stderr


def test_proceeds_when_wrapper_present(wrapper, tmp_path):
    r = _run("true", "my-llama.service", True, wrapper, tmp_path)
    assert r.returncode == 0 and "PROCEEDED" in r.stdout


def test_proceeds_on_non_llama_host(tmp_path):
    r = _run("true", "my-llama.service", False, None, tmp_path)
    assert r.returncode == 0 and "PROCEEDED" in r.stdout


def test_does_not_block_root_update(tmp_path):
    # A privileged (non-self-update) --update installs the wrapper itself.
    r = _run("false", "my-llama.service", True, None, tmp_path)
    assert r.returncode == 0 and "PROCEEDED" in r.stdout
