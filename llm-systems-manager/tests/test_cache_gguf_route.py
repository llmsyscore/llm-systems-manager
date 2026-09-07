"""/api/llm/cache/gguf proxies the agent's HF-cache .gguf listing."""
from __future__ import annotations

import manager_mod


def _client(monkeypatch, calls):
    def fake_proxy(kind, method, path, **kw):
        calls.update(kind=kind, method=method, path=path)
        return {"ok": True, "data": [{"repo": "a/b", "file": "x.gguf", "path": "/p/x.gguf", "size": 1}]}

    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_gguf_listing_proxied(monkeypatch):
    calls = {}
    c = _client(monkeypatch, calls)
    r = c.get("/api/llm/cache/gguf")
    assert r.status_code == 200
    assert calls == {"kind": "llama", "method": "GET", "path": "/llama/cache/gguf"}
    assert r.get_json()["data"][0]["file"] == "x.gguf"


def test_gguf_listing_needs_session():
    c = manager_mod.app.test_client()
    r = c.get("/api/llm/cache/gguf")
    assert r.status_code in (302, 401, 403)
