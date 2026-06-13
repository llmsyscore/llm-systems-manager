"""Terminal provider — 5 PTY-backed routes used by the manager's terminal tab."""

from __future__ import annotations

import fcntl as _fcntl_mod
import json
import logging
import os
import pty as _pty_mod
import pwd
import queue as _queue_lib
import select as _select_mod
import struct as _struct_mod
import subprocess
import termios as _termios_mod
import threading
import uuid
from typing import Any, Iterator, Optional

from fastapi import Header, HTTPException, Query, Request as _Request
from fastapi.responses import StreamingResponse

import stream_pool  # type: ignore[import-not-found]  # sibling at agent root

log = logging.getLogger("llm-systems-agent.providers.terminal")

_ctx = None


def set_context(ctx) -> None:
    global _ctx
    _ctx = ctx


def _require_ctx():
    if _ctx is None:
        raise RuntimeError("providers.terminal.set_context() not called")
    return _ctx


# ── Module state ───────────────────────────────────────────────────────

_term_sessions: dict[str, dict[str, Any]] = {}
_term_sessions_lock = threading.Lock()
_TERM_SESSION_LIMIT = 16


# ── Helpers ────────────────────────────────────────────────────────────

def _reap_session(sid: str) -> bool:
    """Pop from `_term_sessions`, terminate proc, close fd. Idempotent — safe
    to call multiple times. Returns True if a session was actually reaped."""
    with _term_sessions_lock:
        sess = _term_sessions.pop(sid, None)
    if sess is None:
        return False
    try: sess["proc"].terminate()
    except Exception as e: log.warning("reap %s: terminate failed: %s", sid, e)
    try: os.close(sess["master_fd"])
    except Exception as e: log.warning("reap %s: close fd failed: %s", sid, e)
    log.info("terminal session reaped: %s", sid)
    return True


def _term_reader(sid: str, master_fd: int, q: "_queue_lib.Queue", proc: subprocess.Popen) -> None:
    """Drain PTY master fd into the per-session queue."""
    while proc.poll() is None:
        try:
            r, _, _ = _select_mod.select([master_fd], [], [], 0.5)
            if r:
                data = os.read(master_fd, 4096)
                if data:
                    try:
                        q.put(data.decode("utf-8", errors="replace"), timeout=1)
                    except _queue_lib.Full:
                        try: q.get_nowait()
                        except _queue_lib.Empty: pass
                        try: q.put_nowait(data.decode("utf-8", errors="replace"))
                        except _queue_lib.Full: pass
        except OSError:
            break
    # PTY exited. If no SSE consumer has attached, no _gen.finally will fire
    # to reap us — do it inline. Otherwise let _gen.finally handle it after
    # the consumer drains the final queue bytes.
    with _term_sessions_lock:
        sess = _term_sessions.get(sid)
        if sess is None:
            return
        sess["alive"] = False
        no_consumer = not sess.get("consumer_attached", False)
    if no_consumer:
        _reap_session(sid)


# ── Route handlers (module top-level so __qualname__ is stable) ────────

def terminal_create_endpoint(
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    ctx = _require_ctx()
    ctx.check_bearer(authorization)
    with _term_sessions_lock:
        if len(_term_sessions) >= _TERM_SESSION_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"too many open terminals ({_TERM_SESSION_LIMIT}); close some first",
            )
    # Pick the user's login shell; macOS's zsh-nag fires if we hardcode bash.
    shell = "/bin/bash"
    if ctx.config.AGENT_USER:
        try:
            shell = pwd.getpwnam(ctx.config.AGENT_USER).pw_shell or shell
        except KeyError:
            pass
    if not os.path.isfile(shell):
        shell = os.environ.get("SHELL") or "/bin/bash"

    try:
        master_fd, slave_fd = _pty_mod.openpty()
        _fcntl_mod.ioctl(slave_fd, _termios_mod.TIOCSWINSZ,
                         _struct_mod.pack("HHHH", 24, 80, 0, 0))

        def _child_setup() -> None:
            os.setsid()
            _fcntl_mod.ioctl(0, _termios_mod.TIOCSCTTY, 0)

        proc = subprocess.Popen(
            [shell, "-l"] if shell.endswith(("zsh", "bash"))
            else [shell],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True,
            preexec_fn=_child_setup,
            env={**os.environ, "TERM": "xterm-256color",
                 "COLUMNS": "80", "LINES": "24"},
        )
        os.close(slave_fd)
    except Exception as e:
        log.error("terminal_create failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}

    sid = uuid.uuid4().hex[:12]
    q: "_queue_lib.Queue[str]" = _queue_lib.Queue(maxsize=50000)
    sess = {"proc": proc, "master_fd": master_fd, "queue": q,
            "alive": True, "consumer_attached": False}
    with _term_sessions_lock:
        _term_sessions[sid] = sess
    threading.Thread(target=_term_reader,
                     args=(sid, master_fd, q, proc),
                     daemon=True).start()
    log.info("terminal session opened: %s (pid=%s)", sid, proc.pid)
    return {"ok": True, "sid": sid}


def terminal_output_endpoint(
    sid: str,
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    """SSE stream of terminal output; bearer header or ?token= stream token."""
    _require_ctx().check_stream_auth(authorization, token, f"/terminal/output/{sid}")
    with _term_sessions_lock:
        sess = _term_sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if not stream_pool.POOL.try_acquire():
        raise HTTPException(status_code=503, detail="agent at stream capacity; retry shortly")
    with _term_sessions_lock:
        sess["consumer_attached"] = True
    q = sess["queue"]

    def _gen() -> Iterator[bytes]:
        try:
            while True:
                alive = sess.get("alive", True)
                try:
                    chunk = q.get(timeout=0.4)
                    yield f"data: {json.dumps(chunk)}\n\n".encode()
                except _queue_lib.Empty:
                    if not alive and q.empty():
                        break
                    yield b": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            _reap_session(sid)

    return StreamingResponse(
        stream_pool.guarded_async(_gen()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def terminal_input_endpoint(
    sid: str,
    request: _Request,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    with _term_sessions_lock:
        sess = _term_sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    data = await request.body()
    if not data:
        return {"ok": True}
    try:
        os.write(sess["master_fd"], data)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


def terminal_resize_endpoint(
    sid: str,
    body: dict,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    with _term_sessions_lock:
        sess = _term_sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    rows = int(body.get("rows", 24))
    cols = int(body.get("cols", 80))
    try:
        _fcntl_mod.ioctl(sess["master_fd"], _termios_mod.TIOCSWINSZ,
                         _struct_mod.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass
    return {"ok": True}


def terminal_close_endpoint(
    sid: str,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    _reap_session(sid)
    return {"ok": True}


# ── Route registration ────────────────────────────────────────────────

_ROUTES: tuple = (
    ("POST", "/terminal/create",         terminal_create_endpoint),
    ("GET",  "/terminal/output/{sid}",   terminal_output_endpoint),
    ("POST", "/terminal/input/{sid}",    terminal_input_endpoint),
    ("POST", "/terminal/resize/{sid}",   terminal_resize_endpoint),
    ("POST", "/terminal/close/{sid}",    terminal_close_endpoint),
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
