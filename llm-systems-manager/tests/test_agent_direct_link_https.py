"""A role-checked agent that reports a plain-HTTP address gets no direct browser link."""
from __future__ import annotations

import pytest

import agent_registry as ar
import manager_mod as M

CHECKED = "2026-10-01T00:00:00+00:00"


def _agent(bind_url, **over):
    a = {"agent_id": "ag1", "hostname": "box", "status": "approved", "bind_url": bind_url,
         "registered_from": "10.0.0.5", "token": "t"}
    a.update(over)
    return a


def test_refusal_only_for_a_checked_agent_on_plain_http():
    assert ar.plain_http_refusal(None) is None
    assert ar.plain_http_refusal(_agent("http://10.0.0.5:8082")) is None
    assert ar.plain_http_refusal(_agent("https://10.0.0.5:8082", tls_role_checked_at=CHECKED)) is None
    msg = ar.plain_http_refusal(_agent("HTTP://10.0.0.5:8082", tls_role_checked_at=CHECKED))
    assert msg.startswith("agent box:") and "plain-HTTP" in msg


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ar, "issue_stream_token", lambda aid, path, ttl=None: "TOKEN123")
    monkeypatch.setattr(M, "_tool_provider", lambda tool: ("llama", None))
    M.app.config.update(TESTING=True)
    c = M.app.test_client()
    with c.session_transaction() as sess:
        sess["auth_ok"] = True
    return c


INFO_ROUTES = ["/api/llm/server/log/stream-info", "/api/llm/download/stream-info",
               "/api/llm/build/stream-info", "/api/llm/autotune/stream-info"]


@pytest.mark.parametrize("route", INFO_ROUTES)
def test_no_direct_link_for_a_checked_agent_on_plain_http(client, monkeypatch, route):
    monkeypatch.setattr(M, "_request_agent",
                        lambda kind: _agent("http://10.0.0.5:8082", tls_role_checked_at=CHECKED))
    r = client.get(route)
    d = r.get_json()
    assert r.status_code == 409 and d["ok"] is False and "url" not in d
    assert "plain-HTTP" in d["error"]


@pytest.mark.parametrize("route", INFO_ROUTES)
@pytest.mark.parametrize("agent", [_agent("https://10.0.0.5:8082", tls_role_checked_at=CHECKED),
                                   _agent("http://10.0.0.5:8082")])
def test_direct_link_is_still_issued_otherwise(client, monkeypatch, route, agent):
    monkeypatch.setattr(M, "_request_agent", lambda kind: agent)
    r = client.get(route)
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True
    assert d["url"].startswith(agent["bind_url"]) and d["url"].endswith("?token=TOKEN123")


def test_stream_token_route_refuses_a_checked_agent_on_plain_http(client, monkeypatch):
    store = {"agents": {"ag1": _agent("http://10.0.0.5:8082", tls_role_checked_at=CHECKED),
                        "ag2": _agent("https://10.0.0.5:8082", agent_id="ag2", tls_role_checked_at=CHECKED)}}
    monkeypatch.setattr(ar, "load_agents", lambda: store)
    monkeypatch.setattr(ar._deps, "require_admin", lambda: None)
    r = client.post("/api/agents/ag1/stream-token", json={"path": "/llama/log/stream"})
    assert r.status_code == 409 and "plain-HTTP" in r.get_json()["error"]
    r = client.post("/api/agents/ag2/stream-token", json={"path": "/llama/log/stream"})
    assert r.status_code == 200 and r.get_json()["url"].startswith("https://10.0.0.5:8082/llama/log/stream?token=")
