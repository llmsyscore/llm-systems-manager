# agent/tests/test_autotune_load_mode.py
# Auto-tune optional params emit --load-mode alongside the legacy flags (#572).
from __future__ import annotations

import re
from pathlib import Path

LLAMA_PY = Path(__file__).resolve().parents[1] / "providers" / "llama.py"


def _build(params):
    m = re.search(r"^def _autotune_build_optional_args\(.*?(?=^\S)",
                  LLAMA_PY.read_text(), re.MULTILINE | re.DOTALL)
    assert m, "could not extract _autotune_build_optional_args()"
    ns: dict = {}
    exec(compile(m.group(0), str(LLAMA_PY), "exec"), ns)
    return ns["_autotune_build_optional_args"](params)


def test_load_mode_emits_the_new_flag():
    assert _build({"load_mode": "mmap+mlock"}) == ["--load-mode", "mmap+mlock"]


def test_legacy_flags_still_emit():
    assert _build({"mlock": True, "no_mmap": True}) == ["--mlock", "--no-mmap"]


def test_empty_params_emit_nothing():
    assert _build({}) == []
    assert _build(None) == []
