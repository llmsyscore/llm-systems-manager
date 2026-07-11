# agent/tests/test_no_sudoers_install_warning.py
"""#316: fresh install with --no-sudoers while llama/perf is enabled must warn
that svcconfig (ExecStart editor) and unit restarts will be unavailable; the
auto-skip note still prints only when there is nothing for the agent to sudo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"


def _extract_block() -> str:
    lines = INSTALL_SH.read_text().splitlines()
    anchor = next(i for i, l in enumerate(lines) if "skipping sudoers (no llama" in l)
    s = next(i for i in range(anchor, -1, -1) if lines[i].startswith("if [["))
    e = next(i for i in range(s + 1, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[s:e + 1])


BLOCK = _extract_block()


def _run(os_="linux", skip="true", llama="true", perf="false", vllm="false"):
    script = (
        "set -u\n"
        '_warn(){ echo "  ⚠ $*"; }\n'
        f'AGENT_OS="{os_}"\nSKIP_SUDOERS="{skip}"\n'
        f'ENABLE_LLAMA="{llama}"\nENABLE_PERF="{perf}"\nENABLE_VLLM="{vllm}"\n'
        'INSTALL_VLLM="false"\n'
        f"{BLOCK}\n"
        'echo "SKIP=$SKIP_SUDOERS"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.mark.parametrize("llama,perf", [("true", "false"), ("false", "true"), ("true", "true")])
def test_warns_on_explicit_no_sudoers_with_llama_or_perf(llama, perf):
    r = _run(skip="true", llama=llama, perf=perf)
    assert r.returncode == 0
    assert "--no-sudoers" in r.stdout and "svcconfig" in r.stdout


def test_warns_on_explicit_no_sudoers_with_vllm():
    r = _run(skip="true", llama="false", perf="false", vllm="true")
    assert r.returncode == 0
    assert "--no-sudoers" in r.stdout and "svcconfig" in r.stdout


def test_vllm_alone_prevents_auto_skip():
    r = _run(skip="false", llama="false", perf="false", vllm="true")
    assert "skipping sudoers" not in r.stdout and "SKIP=false" in r.stdout


def test_no_warning_when_no_sudoers_and_nothing_to_sudo():
    r = _run(skip="true", llama="false", perf="false")
    assert "svcconfig" not in r.stdout


def test_auto_skip_still_fires_when_nothing_to_sudo():
    r = _run(skip="false", llama="false", perf="false")
    assert "skipping sudoers" in r.stdout and "SKIP=true" in r.stdout


def test_no_output_when_sudoers_wanted_and_llama_on():
    r = _run(skip="false", llama="true", perf="false")
    assert "skipping sudoers" not in r.stdout and "svcconfig" not in r.stdout
    assert "SKIP=false" in r.stdout


def test_macos_prints_nothing():
    r = _run(os_="darwin", skip="true", llama="true")
    assert "svcconfig" not in r.stdout
