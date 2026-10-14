"""Phase 1 (#110): /api/llama-state/stream-info handoff route."""
from __future__ import annotations

import pytest

import manager_mod as M
import agent_registry
import sse_daemon


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(M, "_primary_llama_agent_id", lambda: "ag1", raising=False)
    monkeypatch.setattr(agent_registry, "issue_handoff_token",
                        lambda aid, path, ttl=None: f"TOKEN-{aid}", raising=False)
    monkeypatch.setattr(agent_registry, "resolve_agent_by_id",
                        lambda aid, capability=None: {"agent_id": aid} if aid in ("ag1", "ag2") else None,
                        raising=False)
    monkeypatch.setattr(sse_daemon, "is_running", lambda: True, raising=False)
    monkeypatch.setattr(M, "_request_host_no_port", lambda: "10.0.0.9", raising=False)
    monkeypatch.setattr(M.settings.manager, "stream_proxy_port", 5445, raising=False)
    M.app.config.update(TESTING=True)
    c = M.app.test_client()
    with c.session_transaction() as sess:
        sess["auth_ok"] = True
    return c


def test_info_requires_session():
    M.app.config.update(TESTING=True)
    c = M.app.test_client()  # no session
    r = c.get("/api/llama-state/stream-info")
    assert r.status_code == 401


def test_info_returns_daemon_url(client):
    r = client.get("/api/llama-state/stream-info")
    assert r.status_code == 200
    d = r.get_json()
    assert d["enabled"] is True
    assert d["url"] == "http://10.0.0.9:5445/sse/llama-state?agent=ag1&token=TOKEN-ag1"


def test_info_disabled_when_port_zero(client, monkeypatch):
    monkeypatch.setattr(M.settings.manager, "stream_proxy_port", 0, raising=False)
    r = client.get("/api/llama-state/stream-info")
    assert r.get_json() == {"enabled": False}


def test_info_disabled_under_https(client):
    # TLS requests carry their own __Secure- session cookie (#864).
    with client.session_transaction(base_url="https://localhost") as sess:
        sess["auth_ok"] = True
    r = client.get("/api/llama-state/stream-info", base_url="https://localhost")
    assert r.get_json() == {"enabled": False}


def test_info_disabled_when_daemon_down(client, monkeypatch):
    monkeypatch.setattr(sse_daemon, "is_running", lambda: False, raising=False)
    r = client.get("/api/llama-state/stream-info")
    assert r.get_json() == {"enabled": False}


def test_info_disabled_when_no_agent(client, monkeypatch):
    monkeypatch.setattr(M, "_primary_llama_agent_id", lambda: None, raising=False)
    r = client.get("/api/llama-state/stream-info")
    assert r.get_json() == {"enabled": False}


def test_info_signs_only_for_a_registered_agent(client):
    d = client.get("/api/llama-state/stream-info?agent=ag2").get_json()
    assert d["enabled"] is True and "agent=ag2&token=TOKEN-ag2" in d["url"]
    d = client.get("/api/llama-state/stream-info?agent=../not-registered").get_json()
    assert d["enabled"] is True and "agent=ag1&token=TOKEN-ag1" in d["url"]
