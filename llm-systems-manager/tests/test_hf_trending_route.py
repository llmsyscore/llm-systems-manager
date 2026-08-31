"""/api/llm/hf-trending (#767): query params are validated before being
forwarded to the agent; invalid values are dropped, not passed through."""
from __future__ import annotations

import manager_mod


def _client(monkeypatch, calls):
    def fake_proxy(kind, method, path, **kw):
        calls.update(kind=kind, method=method, path=path, params=kw.get("params"))
        return {"ok": True, "data": []}

    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_valid_params_forwarded(monkeypatch):
    calls = {}
    c = _client(monkeypatch, calls)
    r = c.get("/api/llm/hf-trending?limit=5&min_b=9B&max_b=27B&sort=trending")
    assert r.status_code == 200
    assert calls["kind"] == "llama" and calls["path"] == "/llama/hf-trending"
    assert calls["params"] == {"limit": 5, "min_b": "9B", "max_b": "27B", "sort": "trending"}


def test_lowercase_size_normalized(monkeypatch):
    calls = {}
    c = _client(monkeypatch, calls)
    c.get("/api/llm/hf-trending?min_b=9b&max_b=500b")
    assert calls["params"] == {"min_b": "9B", "max_b": "500B"}


def test_invalid_params_dropped(monkeypatch):
    calls = {}
    c = _client(monkeypatch, calls)
    c.get("/api/llm/hf-trending?limit=999&min_b=;rm+-rf&max_b=27Bx&sort=bogus")
    assert calls["params"] == {}


def test_bare_request_forwards_no_params(monkeypatch):
    calls = {}
    c = _client(monkeypatch, calls)
    c.get("/api/llm/hf-trending")
    assert calls["params"] == {}
