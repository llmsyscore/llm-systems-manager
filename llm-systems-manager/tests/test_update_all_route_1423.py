"""#1423 manager wiring: the self-update frame reader used by the Update all job, and the route's refusals."""
from __future__ import annotations

import json

import pytest

import manager_mod
from manager_mod import app


class _Up:
    def __init__(self, status, ctype, body=b""):
        self.status_code, self.headers, self._body, self.closed = status, {"Content-Type": ctype}, body, False

    def iter_content(self, chunk_size=None):
        for i in range(0, len(self._body), 5):
            yield self._body[i:i + 5]

    def close(self):
        self.closed = True


AGENT = {"agent_id": "a1deadbeef", "hostname": "h", "bind_url": "https://x:8082", "token": "t"}


def _patch(monkeypatch, post):
    monkeypatch.setattr(manager_mod.agent_registry, "agent_callback_urls", lambda agent: ["https://x:8082"])
    monkeypatch.setattr(manager_mod.agent_registry, "agent_tls_kwargs", lambda url: {})
    monkeypatch.setattr(manager_mod.agent_registry, "note_dial_result", lambda *a: None)
    monkeypatch.setattr(manager_mod.agent_registry, "note_dial_error", lambda *a: None)
    monkeypatch.setattr(manager_mod.requests, "post", post)


def test_frames_parse_chunked_sse_and_skip_comments(monkeypatch):
    body = (b': keepalive\n\ndata: {"line":"pip ok"}\n\ndata: not json\n\n'
            b'data: {"stage":"done","ok":true,"rc":0}\n\n')
    up = _Up(200, "text/event-stream", body)
    _patch(monkeypatch, lambda *a, **k: up)
    assert list(manager_mod._agent_self_update_frames(AGENT)) == [
        {"line": "pip ok"}, {"stage": "done", "ok": True, "rc": 0}]
    assert up.closed


def test_frames_non_sse_reply_becomes_failed_done(monkeypatch):
    up = _Up(501, "application/json", json.dumps({"detail": "no asset"}).encode())
    _patch(monkeypatch, lambda *a, **k: up)
    frames = list(manager_mod._agent_self_update_frames(AGENT))
    assert frames == [{"stage": "done", "ok": False, "rc": 501, "msg": "agent returned HTTP 501: no asset"}]


def test_frames_unreachable_agent_raises(monkeypatch):
    def boom(*a, **k):
        raise manager_mod.requests.exceptions.ConnectionError("refused")
    _patch(monkeypatch, boom)
    with pytest.raises(RuntimeError, match="could not reach the agent"):
        list(manager_mod._agent_self_update_frames(AGENT))


def test_route_refuses_when_nothing_needs_an_update(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    monkeypatch.setattr(manager_mod._update_all, "_load", lambda: {"agents": {"a1": {**AGENT, "version": "v9"}}})
    monkeypatch.setattr(manager_mod._update_all, "_latest", lambda: "v9")
    with app.test_request_context("/api/agents/update-all", method="POST"):
        resp, status = manager_mod.agents_update_all()
    assert status == 409
    assert resp.get_json()["error"] == "all agents are already up to date"


def test_route_get_unknown_job_is_empty(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    with app.test_request_context("/api/agents/update-all?job=nope", method="GET"):
        resp = manager_mod.agents_update_all()
    assert resp.get_json() == {"ok": True, "job": None, "lines": [], "next": 0}


def test_route_needs_admin(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: (manager_mod.jsonify({"ok": False}), 403))
    with app.test_request_context("/api/agents/update-all", method="POST"):
        assert manager_mod.agents_update_all()[1] == 403


def test_route_get_without_job_reads_live_then_latest_and_filters_kind(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    other = {"id": "x1", "kind": "forecast.run", "status": "running"}
    monkeypatch.setattr(manager_mod._update_all, "live", lambda: None)
    monkeypatch.setattr(manager_mod._update_all, "latest", lambda: other)
    with app.test_request_context("/api/agents/update-all", method="GET"):
        assert manager_mod.agents_update_all().get_json()["job"] is None
    monkeypatch.setattr(manager_mod._jobs_service, "get", lambda jid: other)
    with app.test_request_context("/api/agents/update-all?job=x1", method="GET"):
        assert manager_mod.agents_update_all().get_json()["job"] is None
