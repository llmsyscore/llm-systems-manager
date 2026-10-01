"""Agent replies relayed to the browser keep only non-rendering media types."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import Flask

import manager_mod
import proxies
from manager_mod import app


@pytest.mark.parametrize("upstream", [
    "application/json", "application/json; charset=utf-8", "Application/JSON",
    "application/x-ndjson", "application/octet-stream", "text/event-stream", "text/plain; charset=utf-8",
])
def test_api_types_pass_through(upstream):
    assert proxies.agent_reply_ctype(upstream) == upstream


@pytest.mark.parametrize("upstream", [
    "text/html", "text/html; charset=utf-8", "image/svg+xml", "application/xhtml+xml",
    "text/xml", "text/html; a=text/event-stream", "", None,
])
def test_other_types_fall_back_to_the_default(upstream):
    assert proxies.agent_reply_ctype(upstream) == "application/json"
    assert proxies.agent_reply_ctype(upstream, "text/event-stream") == "text/event-stream"


def test_proxy_to_primary_relays_an_html_reply_as_json(monkeypatch):
    agent = {"agent_id": "a1deadbeef", "hostname": "h", "token": "t"}
    reply = SimpleNamespace(status_code=200, content=b"<b>x</b>", headers={"Content-Type": "text/html"},
                            elapsed=timedelta(0))
    monkeypatch.setattr(proxies, "_resolve_target", lambda *a, **k: (agent, None))
    monkeypatch.setattr(proxies.agent_registry, "agent_request", lambda *a, **k: (reply, [], None))
    with Flask(__name__).test_request_context("/api/llama/models"):
        resp = proxies.proxy_to_primary("llama", "GET", "/llama/models", agent_id="a1")
    assert resp.mimetype == "application/json"
    assert resp.get_data() == b"<b>x</b>"


class _Upstream:
    def __init__(self, ctype):
        self.status_code = 200
        self.headers = {"Content-Type": ctype}

    def iter_content(self, chunk_size=None):
        yield b'data: {"stage":"done","ok":true,"rc":0}\n\n'

    def close(self):
        pass


def test_self_update_stream_is_always_an_event_stream(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents", lambda: {
        "agents": {"a1": {"agent_id": "a1deadbeef", "hostname": "h",
                          "bind_url": "https://x:8082", "token": "t"}}})
    monkeypatch.setattr(manager_mod.agent_registry, "agent_callback_urls", lambda agent: ["https://x:8082"])
    monkeypatch.setattr(manager_mod.agent_registry, "agent_tls_kwargs", lambda url: {})
    monkeypatch.setattr(manager_mod.stream_pool.POOL, "try_acquire", lambda: True)
    monkeypatch.setattr(manager_mod.stream_pool.POOL, "release", lambda: None)
    monkeypatch.setattr(manager_mod, "_latest_agent_version", lambda: None)
    monkeypatch.setattr(manager_mod.requests, "post",
                        lambda *a, **k: _Upstream("text/html; a=text/event-stream"))
    with app.test_request_context("/api/agents/a1/self-update", method="POST"):
        resp = manager_mod.agents_self_update("a1")
    assert resp.mimetype == "text/event-stream"
