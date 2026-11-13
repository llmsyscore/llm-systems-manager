"""#1422: /api/lmstudio/settings/save proxies to the primary LM Studio agent."""
from __future__ import annotations

import manager_mod
import proxies


def _client(role):
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = role
    return c


def _fake(monkeypatch, status=200, body=None):
    calls = []

    def fake(kind, method, path, **kw):
        calls.append((kind, method, path, kw.get("json")))
        return manager_mod.jsonify(body if body is not None else {"ok": True}), status

    monkeypatch.setattr(proxies, "proxy_to_primary", fake)
    return calls


def test_settings_save_proxies_to_primary_lms(monkeypatch):
    calls = _fake(monkeypatch)
    r = _client("admin").post("/api/lmstudio/settings/save", json={"ignored": 1})
    assert r.status_code == 200
    assert calls == [("lms", "POST", "/lms/settings/save", {})]


def test_settings_save_passes_agent_409_through(monkeypatch):
    _fake(monkeypatch, 409, {"reason": "not-detected", "detail": "There are no detected settings to save."})
    r = _client("admin").post("/api/lmstudio/settings/save", json={})
    assert r.status_code == 409 and r.get_json()["reason"] == "not-detected"


def test_settings_save_requires_admin(monkeypatch):
    calls = _fake(monkeypatch)
    r = _client("operator").post("/api/lmstudio/settings/save", json={})
    assert r.status_code == 403
    assert r.get_json() == {"ok": False, "error": "admin role required", "role_denied": True}
    assert calls == []


def test_settings_save_is_audited():
    hit = manager_mod._audit_match("POST", "/api/lmstudio/settings/save")
    assert hit and hit[0] == "lms.settings-save" and hit[2] == "config.server"
    assert manager_mod._AUDIT_LABELS["lms.settings-save"] == "Saved the detected LM Studio settings"
