"""#403: the self-update proxy must surface a non-SSE / error upstream (e.g. the
frozen 501 'no matching asset' fallback) as a clean done(ok:false) SSE frame,
not forward the raw JSON body the browser's frame parser drops."""
from __future__ import annotations

import json

import manager_mod
from manager_mod import app


class _FakeUpstream:
    def __init__(self, status, ctype, body=b""):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield self._body

    def close(self):
        self.closed = True


# ---- _upstream_error_detail -------------------------------------------------

def _up(body: bytes):
    return _FakeUpstream(501, "application/json", body)


def test_detail_from_fastapi_detail_key():
    up = _up(json.dumps({"detail": "no release asset matches"}).encode())
    assert manager_mod._upstream_error_detail(up) == "no release asset matches"


def test_detail_from_msg_error_message_keys():
    assert manager_mod._upstream_error_detail(_up(b'{"msg":"boom"}')) == "boom"
    assert manager_mod._upstream_error_detail(_up(b'{"error":"nope"}')) == "nope"
    assert manager_mod._upstream_error_detail(_up(b'{"message":"gone"}')) == "gone"


def test_detail_plain_text_body():
    assert manager_mod._upstream_error_detail(_up(b"Bad Gateway")) == "Bad Gateway"


def test_detail_empty_body():
    assert manager_mod._upstream_error_detail(_up(b"")) == ""


def test_detail_json_without_known_keys_falls_back_to_text():
    assert manager_mod._upstream_error_detail(_up(b'{"code":7}')) == '{"code":7}'


class _ChunkedUpstream(_FakeUpstream):
    def iter_content(self, chunk_size=None):
        for i in range(0, len(self._body), 4):   # dribble 4 bytes at a time
            yield self._body[i:i + 4]


def test_detail_reassembles_chunked_body():
    # A body split across many small chunks must still parse — a single-chunk
    # read would truncate the JSON and drop the detail.
    up = _ChunkedUpstream(501, "application/json",
                          json.dumps({"detail": "no release asset matches the platform"}).encode())
    assert manager_mod._upstream_error_detail(up) == "no release asset matches the platform"


# ---- route: non-SSE upstream -> synthesized done frame ----------------------

def _patch_route(monkeypatch, upstream):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents", lambda: {
        "agents": {"a1": {"agent_id": "a1deadbeef", "hostname": "h",
                          "bind_url": "https://x:8082", "token": "t"}}})
    monkeypatch.setattr(manager_mod.agent_registry, "agent_callback_urls",
                        lambda agent: ["https://x:8082"])
    monkeypatch.setattr(manager_mod.agent_registry, "agent_tls_kwargs", lambda url: {})
    monkeypatch.setattr(manager_mod.stream_pool.POOL, "try_acquire", lambda: True)
    monkeypatch.setattr(manager_mod.stream_pool.POOL, "release", lambda: None)
    monkeypatch.setattr(manager_mod.requests, "post", lambda *a, **k: upstream)


def _run_route():
    with app.test_request_context("/api/agents/a1/self-update", method="POST"):
        resp = manager_mod.agents_self_update("a1")
        body = b"".join(resp.response)
    return resp, body


def _first_frame(body: bytes) -> dict:
    assert body.startswith(b"data: ")
    return json.loads(body[len(b"data: "):].split(b"\n\n", 1)[0])


def test_501_json_becomes_clean_done_frame(monkeypatch):
    detail = ("agent is a frozen binary and no release asset matches "
              "Linux/x86_64 — replace the binary manually")
    up = _FakeUpstream(501, "application/json", json.dumps({"detail": detail}).encode())
    _patch_route(monkeypatch, up)
    resp, body = _run_route()
    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type
    frame = _first_frame(body)
    assert frame["stage"] == "done" and frame["ok"] is False
    assert frame["rc"] == 501
    assert detail in frame["msg"]
    assert up.closed


def test_non_sse_200_body_becomes_done_frame(monkeypatch):
    # A 2xx but non-event-stream body would otherwise hit "stream ended
    # without a done frame"; it must also synthesize a done(ok:false) frame.
    up = _FakeUpstream(200, "application/json", b'{"detail":"unexpected json"}')
    _patch_route(monkeypatch, up)
    resp, body = _run_route()
    assert resp.status_code == 200
    frame = _first_frame(body)
    assert frame["ok"] is False and "unexpected json" in frame["msg"]


def test_real_sse_stream_passes_through(monkeypatch):
    up = _FakeUpstream(200, "text/event-stream",
                       b'data: {"stage":"done","ok":true,"rc":0}\n\n')
    _patch_route(monkeypatch, up)
    monkeypatch.setattr(manager_mod, "_latest_agent_version", lambda: None)
    resp, body = _run_route()
    assert resp.status_code == 200
    frame = _first_frame(body)
    assert frame["stage"] == "done" and frame["ok"] is True  # not synthesized
