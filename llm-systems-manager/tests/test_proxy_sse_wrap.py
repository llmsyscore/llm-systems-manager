"""#141: user-service proxies must stream text/event-stream upstreams through
thread_pumped (manager keepalive + lifetime cap + stream slot) so an idle/closed
upstream ends cleanly instead of raising an unhandled ReadTimeout traceback.
Non-stream responses stay on the raw iter_content path."""
from __future__ import annotations

import proxies
from manager_mod import app


class _FakeUpstream:
    status_code = 200

    def __init__(self, ctype):
        self.headers = {"content-type": ctype}
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield b"raw-body"

    def close(self):
        self.closed = True


def test_event_stream_upstream_uses_thread_pumped(monkeypatch):
    seen = {}

    def fake_tp(upstream, path, **kw):
        seen["path"] = path
        return iter([b": ka\n\n"])

    monkeypatch.setattr(proxies, "thread_pumped", fake_tp)
    monkeypatch.setattr(proxies.stream_pool.POOL, "try_acquire", lambda: True)
    monkeypatch.setattr(proxies.stream_pool.POOL, "release", lambda: None)
    up = _FakeUpstream("text/event-stream")
    with app.app_context():
        resp = proxies._proxied_stream_response(up, "/models/sse", [], 200)
        body = b"".join(resp.response)
    assert seen.get("path") == "/models/sse"
    assert b": ka\n\n" in body


def test_non_stream_upstream_proxied_raw(monkeypatch):
    used_tp = {"v": False}
    monkeypatch.setattr(proxies, "thread_pumped",
                        lambda *a, **k: used_tp.__setitem__("v", True) or iter([]))
    up = _FakeUpstream("text/html")
    with app.app_context():
        resp = proxies._proxied_stream_response(up, "/", [], 200)
        body = b"".join(resp.response)
    assert used_tp["v"] is False
    assert body == b"raw-body"


def test_event_stream_at_capacity_returns_503_and_closes(monkeypatch):
    monkeypatch.setattr(proxies.stream_pool.POOL, "try_acquire", lambda: False)
    up = _FakeUpstream("text/event-stream")
    with app.app_context():
        resp = proxies._proxied_stream_response(up, "/models/sse", [], 200)
    assert resp.status_code == 503
    assert up.closed is True


def test_event_stream_releases_slot_on_close(monkeypatch):
    released = {"n": 0}
    monkeypatch.setattr(proxies, "thread_pumped", lambda u, p, **k: iter([b""]))
    monkeypatch.setattr(proxies.stream_pool.POOL, "try_acquire", lambda: True)
    monkeypatch.setattr(proxies.stream_pool.POOL, "release",
                        lambda: released.__setitem__("n", released["n"] + 1))
    up = _FakeUpstream("text/event-stream")
    with app.app_context():
        resp = proxies._proxied_stream_response(up, "/m", [], 200)
    resp.close()
    assert released["n"] == 1


def test_event_stream_releases_slot_if_body_construction_raises(monkeypatch):
    # A newline in an upstream header makes Response() raise after the slot is
    # acquired but before call_on_close is registered: the slot must not leak.
    released = {"n": 0}
    monkeypatch.setattr(proxies.stream_pool.POOL, "try_acquire", lambda: True)
    monkeypatch.setattr(proxies.stream_pool.POOL, "release",
                        lambda: released.__setitem__("n", released["n"] + 1))

    def boom(*a, **k):
        raise ValueError("bad header")

    monkeypatch.setattr(proxies, "thread_pumped", boom)
    up = _FakeUpstream("text/event-stream")
    with app.app_context():
        try:
            proxies._proxied_stream_response(up, "/m", [], 200)
        except ValueError:
            pass
    assert released["n"] == 1
    assert up.closed is True
