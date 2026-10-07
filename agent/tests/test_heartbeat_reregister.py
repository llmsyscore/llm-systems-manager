"""#1188: repeated 401/403 heartbeats start a new registration pass instead of
retrying the same token forever."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"
SRC = AGENT_PY.read_text()


def _extract_func(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _ns(register_calls: list, done: threading.Event):
    def _register():
        register_calls.append(time.monotonic())
        done.wait(2)
    ns = {"threading": threading, "Optional": object, "logger": logging.getLogger("test"),
          "registry_register_blocking": _register, "_register_thread": None}
    exec(compile(_extract_func("_start_registration"), str(AGENT_PY), "exec"), ns)
    return ns


def test_start_registration_runs_one_pass_at_a_time():
    calls, done = [], threading.Event()
    ns = _ns(calls, done)
    assert ns["_start_registration"]() is True
    assert ns["_start_registration"]() is False       # first pass still running
    done.set()
    ns["_register_thread"].join(2)
    assert ns["_start_registration"]() is True        # finished pass → a new one may start
    done.set()
    ns["_register_thread"].join(2)
    assert len(calls) == 2


def test_heartbeat_loop_reregisters_after_consecutive_rejects():
    body = _extract_func("heartbeat_loop")
    reject = body[body.index("elif r.status_code in (401, 403):"):]
    reject = reject[:reject.index("else:")]
    assert "rejected += 1" in reject
    assert "rejected >= _REREGISTER_AFTER_REJECTS" in reject
    assert "_start_registration()" in reject
    ok = body[body.index("if r.ok:"):body.index("elif r.status_code in (401, 403):")]
    assert "rejected = 0" in ok, "a successful heartbeat must reset the reject count"
    assert re.search(r"^_REREGISTER_AFTER_REJECTS = [2-9]$", SRC, re.MULTILINE)


def test_startup_registration_goes_through_the_shared_starter():
    assert "threading.Thread(target=registry_register_blocking" not in SRC.replace(
        _extract_func("_start_registration"), "")
    assert "    _start_registration()\n    threading.Thread(target=heartbeat_loop" in SRC
