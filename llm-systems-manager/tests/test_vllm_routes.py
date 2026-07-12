# llm-systems-manager/tests/test_vllm_routes.py
"""#125: /api/vllm/* route family + provider-parametrized gateway."""
from __future__ import annotations

import types

from flask import Flask

import gateway
import manager_mod


def test_vllm_routes_registered():
    rules = {str(r) for r in manager_mod.app.url_map.iter_rules()}
    for path in [
        "/api/vllm/metrics",
        "/api/vllm/models",
        "/api/vllm/server/status",
        "/api/vllm/server/start",
        "/api/vllm/server/stop",
        "/api/vllm/server/restart",
        "/api/vllm/server/log",
        "/api/vllm/server/svcconfig",
        "/api/vllm/log/stream",
        "/api/vllm/lora/load",
        "/api/vllm/lora/unload",
        "/api/gateway/vllm/v1/chat/completions",
        "/api/gateway/vllm/v1/completions",
        "/api/gateway/vllm/v1/models",
    ]:
        assert path in rules, f"missing route {path}"


def test_vllm_metrics_offline_shape():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    r = c.get("/api/vllm/metrics")
    assert r.status_code == 200
    body = r.get_json()
    assert body.get("agent_online") is False


def _client():
    app = Flask(__name__)
    gateway.register_routes(app, types.SimpleNamespace())
    return app.test_client()


def test_gateway_vllm_no_candidates_503(monkeypatch):
    monkeypatch.setattr(gateway, "_candidates", lambda m, a, p="llama": [])
    r = _client().post("/api/gateway/vllm/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 503
    err = r.get_json()["error"]
    assert err["code"] == 503


def test_gateway_paths_map_per_provider():
    assert gateway._AGENT_PATHS["llama"]["chat/completions"] == "/llama/openai/chat/completions"
    assert gateway._AGENT_PATHS["vllm"]["chat/completions"] == "/vllm/openai/chat/completions"
    assert gateway._AGENT_PATHS["vllm"]["completions"] == "/vllm/openai/completions"


def test_gateway_vllm_candidates_use_vllm_capability(monkeypatch):
    seen = {}

    def fake_resolve(pk, model_id, agent_id, allow_pool=False):
        seen["pk"] = pk
        return None, None

    monkeypatch.setattr(gateway.proxies, "_resolve_target", fake_resolve)
    monkeypatch.setattr(gateway.agent_registry, "load_agents", lambda: {})
    monkeypatch.setattr(gateway.agent_registry, "default_agent_id_for",
                        lambda p: seen.setdefault("default_for", p) and None)
    assert gateway._candidates(None, None, "vllm") == []
    assert seen["pk"] == "vllm"
    assert seen["default_for"] == "vllm"


def test_vllm_autotune_routes_registered():
    rules = {str(r) for r in manager_mod.app.url_map.iter_rules()}
    for path in ["/api/vllm/autotune/run", "/api/vllm/autotune/stream",
                 "/api/vllm/autotune/cancel"]:
        assert path in rules, f"missing route {path}"


def test_vllm_autotune_run_proxies_to_vllm(monkeypatch):
    calls = {}

    def fake_proxy(kind, method, path, **kw):
        calls.update(kind=kind, method=method, path=path)
        return {"ok": True}

    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    r = c.post("/api/vllm/autotune/run", json={"probe_len": 4096})
    assert r.status_code == 200
    assert calls == {"kind": "vllm", "method": "POST", "path": "/vllm/autotune/run"}
