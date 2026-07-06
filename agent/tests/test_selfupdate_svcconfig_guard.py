# agent/tests/test_selfupdate_svcconfig_guard.py
"""#307: self-update must refuse to deploy new code on a llama host that
hasn't had the root-owned svcconfig helper installed yet, and leave the
current agent running instead of breaking the ExecStart editor."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"


def _extract_guard() -> str:
    lines = INSTALL_SH.read_text().splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.strip().startswith("if $FROM_SELF_UPDATE && [[ \"$AGENT_OS\" == \"linux\""))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "  fi")
    return "\n".join(lines[start:end + 1])


GUARD = _extract_guard()


def _run(from_self: str, llama_unit: Path | None, wrap: Path | None):
    block = GUARD.replace("/etc/systemd/system/llama_server.service", "$LLAMA_UNIT") \
                 .replace("/usr/local/sbin/llm-svcconfig-apply", "$WRAP")
    script = (
        "set -u\n"
        f'AGENT_OS="linux"\n'
        f"FROM_SELF_UPDATE={from_self}\n"
        "SRC_DIR=/tmp/staged\n"
        f'LLAMA_UNIT="{llama_unit or "/nonexistent/unit"}"\n'
        f'WRAP="{wrap or "/nonexistent/wrap"}"\n'
        f"{block}\n"
        "echo PROCEEDED\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.fixture
def unit(tmp_path):
    p = tmp_path / "llama_server.service"; p.write_text("[Service]\n"); return p


@pytest.fixture
def wrapper(tmp_path):
    p = tmp_path / "llm-svcconfig-apply"; p.write_text("#!/bin/sh\n"); p.chmod(0o755); return p


def test_aborts_on_llama_host_missing_wrapper(unit):
    r = _run("true", unit, None)
    assert r.returncode == 1
    assert "PROCEEDED" not in r.stdout
    assert "one-time root migration" in r.stderr


def test_proceeds_when_wrapper_present(unit, wrapper):
    r = _run("true", unit, wrapper)
    assert r.returncode == 0
    assert "PROCEEDED" in r.stdout


def test_proceeds_on_non_llama_host(tmp_path):
    r = _run("true", None, None)
    assert r.returncode == 0
    assert "PROCEEDED" in r.stdout


def test_does_not_block_root_update(unit):
    # A privileged (non-self-update) --update installs the wrapper itself,
    # so the guard must not fire even when the wrapper is still absent.
    r = _run("false", unit, None)
    assert r.returncode == 0
    assert "PROCEEDED" in r.stdout
