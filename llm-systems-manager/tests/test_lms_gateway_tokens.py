# llm-systems-manager/tests/test_lms_gateway_tokens.py
"""#502: /api/lmstudio/metrics carries agent_id + gateway token counters."""
from __future__ import annotations

import gateway_usage
import manager_mod
import provider_state


def _auth_client():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_lms_metrics_attaches_gateway_tokens(monkeypatch):
    aid = "test-502-agent-a"
    monkeypatch.setattr(manager_mod.agent_registry, "default_agent_id_for",
                        lambda p: aid if p == "lms" else None)
    provider_state.STORE.put("lms", aid, {"ps": [], "models": []})
    gateway_usage.record(aid, 7, 11)  # (prompt, completion)
    body = _auth_client().get("/api/lmstudio/metrics").get_json()
    assert body["agent_id"] == aid
    assert body["gateway_tokens"] == {"gen": 11, "prompt": 7}


def test_lms_metrics_zero_counters_before_any_traffic(monkeypatch):
    aid = "test-502-agent-b"
    monkeypatch.setattr(manager_mod.agent_registry, "default_agent_id_for",
                        lambda p: aid if p == "lms" else None)
    provider_state.STORE.put("lms", aid, {"ps": [], "models": []})
    body = _auth_client().get("/api/lmstudio/metrics").get_json()
    assert body["gateway_tokens"] == {"gen": 0, "prompt": 0}


def test_lms_metrics_no_agent_has_no_gateway_tokens(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "default_agent_id_for",
                        lambda p: None)
    body = _auth_client().get("/api/lmstudio/metrics").get_json()
    assert body.get("agent_id") is None
    assert "gateway_tokens" not in body
