# agent/tests/test_restart_exit_code.py
# #437: brew-services systemd units are Restart=on-failure — a self-restart
# must leave a non-zero exit, or the agent stops dead instead of respawning.
from __future__ import annotations

import re
from pathlib import Path

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"


def _extract_func(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", AGENT_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _exit_code_with_state(state: dict) -> int:
    ns = {"_state": state}
    exec(compile(_extract_func("_restart_exit_code"), str(AGENT_PY), "exec"), ns)
    return ns["_restart_exit_code"]()


def test_exit_code_zero_by_default():
    assert _exit_code_with_state({}) == 0
    assert _exit_code_with_state({"restart_pending": False}) == 0


def test_exit_code_one_when_restart_pending():
    assert _exit_code_with_state({"restart_pending": True}) == 1


def test_main_exits_nonzero_on_restart_pending():
    src = _extract_func("main")
    assert "_restart_exit_code" in src, \
        "main() never consults _restart_exit_code() after uvicorn.run"
    assert "SystemExit" in src, "main() never raises SystemExit on restart"


def test_tls_auto_restart_marks_restart_pending():
    # The first-TLS-receipt auto-restart SIGTERMs like /agent/restart does —
    # it must set restart_pending first or a brew agent dies on approval.
    text = AGENT_PY.read_text()
    m = re.search(r"if not _SERVED_WITH_TLS:.*?_th\.Thread\(target=_restart_for_tls",
                  text, re.DOTALL)
    assert m, "could not locate the TLS auto-restart block"
    assert '_state["restart_pending"] = True' in m.group(0), \
        "TLS auto-restart never sets restart_pending"
