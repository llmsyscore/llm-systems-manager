"""Standalone aiohttp SSE daemon (#110).

Serves /api/llama-state/stream off the fixed Cheroot worker pool: a held
stream costs an asyncio task on this daemon's own loop/thread instead of
pinning one OS worker. aiohttp is imported lazily (inside start()/_build_app)
so this module always imports — a missing dep just disables the daemon and
the Cheroot path serves the stream as before.

Auth mirrors the existing browser→agent stream-token model: a session-gated
Cheroot route mints `agent_registry.issue_stream_token(agent_id, path, ttl)`
and this daemon verifies it (constant-time). The daemon does no authorization.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time

import provider_state  # type: ignore[import-not-found]  # leaf, no cycle
from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling

log = logging.getLogger("llm-systems-manager.sse_daemon")

PATH = "/sse/llama-state"

# _active/_running mutated only on the daemon loop thread; read cross-thread
# (atomic int/bool reads — no lock needed).
_active = 0
_running = False
_stopping = False  # set by stop() so the intentional loop teardown isn't logged as a crash
_loop = None  # the daemon's asyncio loop, for stop()


def active_count() -> int:
    return _active


def is_running() -> bool:
    return _running


def _inc() -> None:
    global _active
    _active += 1


def _dec() -> None:
    global _active
    _active = max(0, _active - 1)


def _verify_handoff(token: str, agent_id: str, path: str, secret: bytes) -> bool:
    """Verify "<expiry>.<sig>" where sig=HMAC-SHA256(secret, "<agent_id>|<path>|<expiry>"),
    matching agent_registry.issue_stream_token. agent_id comes from the URL; a
    tampered agent_id fails the HMAC."""
    if not token or "." not in token or not agent_id:
        return False
    try:
        expiry_str, sig = token.split(".", 1)
        expiry = int(expiry_str)
    except (ValueError, TypeError):
        return False
    if expiry < time.time():
        return False
    msg = f"{agent_id}|{path}|{expiry}".encode()
    expected = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _build_app(*, secret: bytes, snapshot_fn, lifetime_s: float = 600.0,
               keepalive_s: float = 8.0, is_shutting_down=lambda: False,
               store=None):
    """Construct the aiohttp app. `store` defaults to the live STORE singleton;
    tests inject a fresh _ProviderSampleStore for isolation."""
    import asyncio
    from aiohttp import web

    st = store if store is not None else provider_state.STORE

    async def _healthz(request):
        return web.json_response({"ok": True, "active": _active})

    async def _llama_state(request):
        token = request.query.get("token", "")
        aid = request.query.get("agent", "")
        if not _verify_handoff(token, aid, PATH, secret):
            return web.Response(status=401, text="invalid stream token")
        loop = asyncio.get_running_loop()
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": request.headers.get("Origin", "*"),
        })
        await resp.prepare(request)
        q = st.subscribe_async("llama", aid, loop)
        _inc()
        try:
            await resp.write(f"data: {json.dumps(snapshot_fn(aid))}\n\n".encode())
            deadline = loop.time() + lifetime_s
            while not is_shutting_down() and loop.time() < deadline:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=keepalive_s)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                if msg is None:  # shutdown / evict sentinel
                    break
                await resp.write(msg.encode() if isinstance(msg, str) else msg)
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            # Runs on client-disconnect cancel (async finally DOES run, unlike a
            # sync-gen finally — CLAUDE.md gotcha #23) and on normal end.
            st.unsubscribe_async("llama", aid, q)
            _dec()
        return resp

    app = web.Application()
    app.router.add_get(PATH, _llama_state)
    app.router.add_get("/healthz", _healthz)
    return app


def start(*, port: int, lifetime_s: float, keepalive_s: float,
          secret: bytes, snapshot_fn, is_shutting_down) -> None:
    """Launch the daemon thread. No-op (logged) when port<=0 or aiohttp missing."""
    if not port or int(port) <= 0:
        log.info("  SSE daemon:  disabled (set [manager].stream_proxy_port to enable)")
        return
    try:
        import aiohttp  # noqa: F401
    except Exception as e:
        log.warning("  SSE daemon:  disabled (aiohttp not installed: %s)", e)
        return

    def _run() -> None:
        import asyncio
        from aiohttp import web

        global _loop, _running
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop = loop
        app = _build_app(secret=secret, snapshot_fn=snapshot_fn,
                         lifetime_s=lifetime_s, keepalive_s=keepalive_s,
                         is_shutting_down=is_shutting_down)
        runner = web.AppRunner(app)

        async def _serve() -> None:
            global _running
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(port))
            await site.start()
            _running = True
            log.info("  SSE daemon:  serving http://0.0.0.0:%d%s", int(port), PATH)
            await asyncio.Future()  # run forever

        try:
            loop.run_until_complete(_serve())
        except Exception:
            if not _stopping:
                log.exception("SSE daemon: server crashed")
        finally:
            _running = False

    threading.Thread(target=_run, name="sse-daemon", daemon=True).start()


def stop() -> None:
    """Best-effort: stop the daemon loop. The process's hard-exit backstop
    covers an unclean aiohttp teardown (thread is daemon=True)."""
    global _running, _stopping
    _stopping = True
    _running = False
    loop = _loop
    if loop is None:
        return
    with best_effort("sse_daemon: stop event loop", log=log):
        loop.call_soon_threadsafe(loop.stop)
