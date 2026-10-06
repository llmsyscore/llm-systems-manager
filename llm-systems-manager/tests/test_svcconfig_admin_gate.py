"""Startup Config writes (llama + vLLM svcconfig POST) need the admin role and a JSON body."""
from __future__ import annotations

import sqlite3

import pytest
from flask import jsonify

import manager_mod as M  # noqa: E402  # loaded by conftest

POST_ROUTES = ["/api/llm/server/svcconfig", "/api/vllm/server/svcconfig"]
OUTSIDE_ADMIN_CIDRS = "203.0.113.7"
BODY = {"binary": "/opt/llama/bin/llama-server", "args": [], "restart": False}


def _mem_audit_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, role TEXT,
            ip TEXT, method TEXT, path TEXT, action TEXT, target TEXT,
            status INTEGER, outcome TEXT, auth TEXT, detail TEXT, event TEXT)
    """)
    return conn


@pytest.fixture
def proxied(monkeypatch):
    calls = []

    def fake_proxy(kind, method, path, **kw):
        calls.append((kind, method, path, kw.get("json")))
        return jsonify({"ok": True})

    monkeypatch.setattr(M.proxies, "proxy_to_primary", fake_proxy)
    conn = _mem_audit_db()
    monkeypatch.setattr(M, "get_audit_db", lambda: conn)
    monkeypatch.setattr(M, "_AGENT_ADMIN_ALLOW", ["127.0.0.1"])
    return calls


def _session_client(monkeypatch, role):
    import auth
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    monkeypatch.setattr(auth, "_live_role_for_session", lambda: (role, True))
    monkeypatch.setattr(auth, "_session_must_change", lambda: False)
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = role
        s["user"] = f"svccfg-{role}"
    return c


@pytest.mark.parametrize("path", POST_ROUTES)
def test_operator_post_denied_and_not_proxied(monkeypatch, proxied, path):
    c = _session_client(monkeypatch, "operator")
    r = c.post(path, json=BODY, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 403
    assert r.get_json().get("role_denied") is True
    assert proxied == []


@pytest.mark.parametrize("path", POST_ROUTES)
def test_admin_outside_admin_cidrs_still_saves(monkeypatch, proxied, path):
    assert M._admin_ip_allowed(OUTSIDE_ADMIN_CIDRS) is False
    c = _session_client(monkeypatch, "admin")
    r = c.post(path, json=BODY, environ_base={"REMOTE_ADDR": OUTSIDE_ADMIN_CIDRS})
    assert r.status_code == 200
    assert len(proxied) == 1 and proxied[0][1] == "POST" and proxied[0][3] == BODY


@pytest.mark.parametrize("path", POST_ROUTES)
def test_operator_can_still_read_config(monkeypatch, proxied, path):
    c = _session_client(monkeypatch, "operator")
    r = c.get(path, environ_base={"REMOTE_ADDR": OUTSIDE_ADMIN_CIDRS})
    assert r.status_code == 200
    assert len(proxied) == 1 and proxied[0][1] == "GET"


@pytest.mark.parametrize("path", POST_ROUTES)
def test_non_json_body_rejected_in_bypass_mode(monkeypatch, proxied, path):
    import auth
    monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
    monkeypatch.setattr(auth, "_bypass_role", lambda: "admin")
    c = M.app.test_client()
    r = c.post(path, data='{"binary": "/bin/sh", "args": [], "restart": true}',
               content_type="text/plain", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 415
    assert proxied == []
    r = c.post(path, json=BODY, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200 and len(proxied) == 1
