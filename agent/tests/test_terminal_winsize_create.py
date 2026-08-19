# agent/tests/test_terminal_winsize_create.py
# The interactive PTY opens at the client's fitted size, not 80x24 (#573).
from __future__ import annotations

import re
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
TERMINAL_PY = AGENT_DIR / "providers" / "terminal.py"


def _extract_py_func(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", TERMINAL_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from terminal.py"
    return m.group(0)


def _winsize(body):
    ns: dict = {"Optional": None}
    exec(compile(_extract_py_func("_requested_winsize"),
                 str(TERMINAL_PY), "exec"), ns)
    return ns["_requested_winsize"](body)


def test_defaults_to_80x24_without_a_body():
    assert _winsize(None) == (24, 80)
    assert _winsize({}) == (24, 80)


def test_uses_the_requested_size():
    assert _winsize({"rows": 42, "cols": 187}) == (42, 187)


def test_clamps_absurd_and_rejects_junk():
    assert _winsize({"rows": 100000, "cols": -3}) == (500, 20)
    assert _winsize({"rows": "x", "cols": None}) == (24, 80)
    # Falsy values read as "not sent" and take the defaults, not the floor.
    assert _winsize({"rows": 0, "cols": 0}) == (24, 80)
