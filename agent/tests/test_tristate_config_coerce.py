# agent/tests/test_tristate_config_coerce.py
"""#547: tri-state COLLECT_* values from YAML or LSA_* env overrides must keep
"auto" intact and coerce every other string fail-closed to a strict bool."""
from __future__ import annotations

import re
from pathlib import Path

_AGENT_PY = Path(__file__).resolve().parent.parent / "llm-systems-agent.py"


def _coerce():
    m = re.search(r"^def _coerce_tristate\(.*?(?=^\S)", _AGENT_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, "could not extract _coerce_tristate() from llm-systems-agent.py"
    ns: dict = {}
    exec(compile(m.group(0), str(_AGENT_PY), "exec"), ns)
    return ns["_coerce_tristate"]


def test_auto_survives_any_casing():
    f = _coerce()
    for v in ("auto", "AUTO", " Auto "):
        assert f(v) == "auto"


def test_bools_pass_through():
    f = _coerce()
    assert f(True) is True
    assert f(False) is False


def test_truthy_words_coerce_true():
    f = _coerce()
    for v in ("1", "true", "YES", "on", 1):
        assert f(v) is True


def test_unrecognized_strings_fail_closed():
    f = _coerce()
    for v in ("disabled", "Fales", "", "garbage", "none", "0", "off"):
        assert f(v) is False


def test_load_routes_tristate_keys_through_coercion():
    text = _AGENT_PY.read_text()
    # env override branch must keep the raw string for tri-state keys …
    assert re.search(
        r"if k in _TRISTATE_COLLECT_KEYS:\s*\n\s*setattr\(cfg, k, raw\)", text), \
        "LSA_ override branch bool-coerces tri-state keys — 'auto' would become False"
    # … and a post-load pass must normalize them all.
    assert re.search(
        r"for k in _TRISTATE_COLLECT_KEYS:\s*\n\s*"
        r"setattr\(cfg, k, _coerce_tristate\(getattr\(cfg, k\)\)\)", text), \
        "load() never normalizes tri-state flags — YAML typos would fail open"
