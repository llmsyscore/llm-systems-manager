# agent/tests/test_llama_method_help.py
from __future__ import annotations

import re
import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parents[1] / "install" / "install.sh"

ALL_METHODS = ["custom_script", "source", "release_binary", "conda", "homebrew"]


def _run_help(*methods: str) -> str:
    text = INSTALL_SH.read_text()
    m = re.search(r"^_print_llama_method_help\(\) \{.*?^\}", text, re.M | re.S)
    assert m, "could not extract _print_llama_method_help from install.sh"
    script = f'{m.group(0)}\n_print_llama_method_help "$@"\n'
    out = subprocess.run(["bash", "-c", script, "bash", *methods],
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_all_methods_each_get_a_description_line():
    out = _run_help(*ALL_METHODS)
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == len(ALL_METHODS)
    for method in ALL_METHODS:
        # each method name appears and the line carries more than just the name
        line = next(l for l in lines if l.split()[0] == method)
        assert len(line.split(maxsplit=1)[1].strip()) > 10


def test_install_subset_excludes_custom_script():
    out = _run_help("source", "release_binary", "conda", "homebrew")
    assert "custom_script" not in out
    assert "release_binary" in out
    assert out.count("\n") == 4  # one line per requested method
