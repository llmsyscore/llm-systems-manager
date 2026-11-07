"""#1419: /api/llm/mode and /api/llm/settings/save proxy to the primary llama agent."""
from __future__ import annotations

import manager_mod
import proxies


def _admin_client():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def _fake(monkeypatch, status=200, body=None):
    calls = []

    def fake(kind, method, path, **kw):
        calls.append((kind, method, path, kw.get("json")))
        return manager_mod.jsonify(body if body is not None else {"ok": True}), status

    monkeypatch.setattr(proxies, "proxy_to_primary", fake)
    return calls


def test_mode_route_proxies_to_primary_llama_agent(monkeypatch):
    calls = _fake(monkeypatch)
    r = _admin_client().post("/api/llm/mode", json={"to": "router"})
    assert r.status_code == 200
    assert calls == [("llama", "POST", "/llama/mode", {"to": "router"})]


def test_mode_route_passes_agent_409_through(monkeypatch):
    _fake(monkeypatch, 409, {"reason": "environment", "detail": "set by env"})
    r = _admin_client().post("/api/llm/mode", json={"to": "router"})
    assert r.status_code == 409 and r.get_json()["reason"] == "environment"


def test_settings_save_route_proxies(monkeypatch):
    calls = _fake(monkeypatch)
    r = _admin_client().post("/api/llm/settings/save", json={"ignored": 1})
    assert r.status_code == 200
    assert calls == [("llama", "POST", "/llama/settings/save", {})]


def test_models_route_keeps_extra(monkeypatch):
    extra = {"cached": [{"repo": "a/b"}], "mode": {"mode": "single"}}
    calls = _fake(monkeypatch, 200, {"data": [], "extra": extra})
    r = _admin_client().get("/api/llm/models")
    assert r.get_json()["extra"] == extra
    assert calls[0][2] == "/llama/models"


def test_mode_and_save_are_audited():
    for path, action in (("/api/llm/mode", "llama.mode-switch"),
                         ("/api/llm/settings/save", "llama.settings-save")):
        hit = manager_mod._audit_match("POST", path)
        assert hit and hit[0] == action


def test_mode_and_save_refuse_operator(monkeypatch):
    calls = _fake(monkeypatch)
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "operator"
    for path in ("/api/llm/mode", "/api/llm/settings/save"):
        r = c.post(path, json={"to": "router"})
        assert r.status_code == 403 and r.get_json().get("role_denied") is True
    assert calls == []


def test_mode_route_waits_240_s(monkeypatch):
    seen = {}

    def fake(kind, method, path, **kw):
        seen.update(kw)
        return manager_mod.jsonify({"ok": True}), 200

    monkeypatch.setattr(proxies, "proxy_to_primary", fake)
    _admin_client().post("/api/llm/mode", json={"to": "router"})
    assert seen["timeout"] == 240
