"""Runs the shell test for the llama parts of the package maintainer scripts."""
import pathlib
import shutil
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "tools/packaging/tests/test_agent_common_llama.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_common_sh_llama_cases():
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
