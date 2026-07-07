# agent/tests/test_terminal_reattach.py
# Terminal SSE disconnect detaches the consumer instead of killing the PTY;
# detached sessions reap after a grace period (#281).
from __future__ import annotations

import logging
import os
import queue
import re
import select
import subprocess
import threading
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
TERMINAL_PY = AGENT_DIR / "providers" / "terminal.py"


def _extract_py_func(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", TERMINAL_PY.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from terminal.py"
    return m.group(0)


def _ns(sessions: dict, grace: float = 600.0) -> dict:
    ns = {
        "os": os, "threading": threading, "time": time, "select": select,
        "_select_mod": select, "_queue_lib": queue, "subprocess": subprocess,
        "log": logging.getLogger("test"),
        "_term_sessions": sessions,
        "_term_sessions_lock": threading.Lock(),
        "_DETACH_GRACE_S": grace,
    }
    for fn in ("_reap_session", "_release_consumer", "_term_reader"):
        exec(compile(_extract_py_func(fn), str(TERMINAL_PY), "exec"), ns)
    return ns


class _FakeProc:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def poll(self):
        return 0 if self.terminated else None


def _mk_session(alive: bool = True, consumers: int = 1):
    r, w = os.pipe()
    return {"proc": _FakeProc(), "master_fd": r, "queue": queue.Queue(),
            "alive": alive, "consumers": consumers, "detached_at": None}, w


def test_release_keeps_live_session_for_reattach():
    sess, w = _mk_session(alive=True, consumers=1)
    sessions = {"sid1": sess}
    ns = _ns(sessions)
    ns["_release_consumer"]("sid1")
    assert "sid1" in sessions, "live session must survive a consumer disconnect"
    assert sess["consumers"] == 0
    assert sess["detached_at"] is not None
    assert not sess["proc"].terminated
    os.close(w)


def test_release_reaps_dead_session():
    sess, w = _mk_session(alive=False, consumers=1)
    sessions = {"sid1": sess}
    ns = _ns(sessions)
    ns["_release_consumer"]("sid1")
    assert "sid1" not in sessions
    assert sess["proc"].terminated
    os.close(w)


def test_reader_reaps_after_detach_grace():
    sess, w = _mk_session(alive=True, consumers=0)
    sess["detached_at"] = time.monotonic() - 10
    sessions = {"sid1": sess}
    ns = _ns(sessions, grace=0.05)
    t = threading.Thread(
        target=ns["_term_reader"],
        args=("sid1", sess["master_fd"], sess["queue"], sess["proc"]),
        daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert "sid1" not in sessions, "detached session must reap after the grace period"
    assert sess["proc"].terminated
    os.close(w)


def test_reader_keeps_attached_session():
    sess, w = _mk_session(alive=True, consumers=1)
    sessions = {"sid1": sess}
    ns = _ns(sessions, grace=0.05)
    t = threading.Thread(
        target=ns["_term_reader"],
        args=("sid1", sess["master_fd"], sess["queue"], sess["proc"]),
        daemon=True)
    t.start()
    time.sleep(0.8)
    assert "sid1" in sessions, "attached session must not be grace-reaped"
    sess["proc"].terminated = True  # let the reader exit
    t.join(timeout=5)
    os.close(w)


def test_reap_only_if_detached_spares_attached_session():
    sess, w = _mk_session(alive=True, consumers=1)
    sessions = {"sid1": sess}
    ns = _ns(sessions)
    assert ns["_reap_session"]("sid1", only_if_detached=True) is False
    assert "sid1" in sessions and not sess["proc"].terminated
    os.close(w)


def test_reap_flags_session_dead_for_attached_generators():
    sess, w = _mk_session(alive=True, consumers=1)
    sessions = {"sid1": sess}
    ns = _ns(sessions)
    assert ns["_reap_session"]("sid1") is True
    assert sess["alive"] is False, "generators must see the reaped session as dead"
    os.close(w)


def test_stream_finally_releases_instead_of_reaping():
    text = TERMINAL_PY.read_text()
    m = re.search(r"def _gen\(\).*?return StreamingResponse", text, re.DOTALL)
    assert m, "could not locate the terminal _gen body"
    body = m.group(0)
    assert "_release_consumer(sid)" in body
    assert "_reap_session(sid)" not in body, \
        "stream disconnect must not hard-reap the session"
