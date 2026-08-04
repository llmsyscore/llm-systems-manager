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


def test_history_rates_from_sampled_counters():
    aid = "test-502-ring-a"
    gateway_usage.record(aid, 100, 200)
    gateway_usage.sample_now(now=1000.0)
    gateway_usage.record(aid, 50, 300)   # +50 prompt, +300 gen
    gateway_usage.sample_now(now=1010.0)
    rows = gateway_usage.history_rates(aid)
    assert rows == [{"ts": rows[0]["ts"], "gen_tps": 30.0, "prompt_tps": 5.0}]
    assert rows[0]["ts"].endswith("+00:00")


def test_history_rates_skips_counter_reset():
    aid = "test-502-ring-b"
    gateway_usage.record(aid, 10, 10)
    gateway_usage.sample_now(now=2000.0)
    with gateway_usage._lock:
        gateway_usage._counters[aid] = {"gen": 1, "prompt": 1}
    gateway_usage.sample_now(now=2010.0)
    gateway_usage.record(aid, 4, 9)
    gateway_usage.sample_now(now=2020.0)
    rows = gateway_usage.history_rates(aid)
    assert len(rows) == 1
    assert rows[0] == {"ts": rows[0]["ts"], "gen_tps": 0.9, "prompt_tps": 0.4}


def test_tokens_history_route(monkeypatch):
    aid = "test-502-ring-c"
    monkeypatch.setattr(manager_mod.agent_registry, "default_agent_id_for",
                        lambda p: aid if p == "lms" else None)
    gateway_usage.record(aid, 60, 120)
    gateway_usage.sample_now(now=3000.0)
    gateway_usage.record(aid, 60, 120)
    gateway_usage.sample_now(now=3010.0)
    body = _auth_client().get("/api/lmstudio/tokens/history").get_json()
    assert body["agent_id"] == aid
    assert body["rows"] == [{"ts": body["rows"][0]["ts"],
                             "gen_tps": 12.0, "prompt_tps": 6.0}]


def test_tokens_history_route_no_agent(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "default_agent_id_for",
                        lambda p: None)
    body = _auth_client().get("/api/lmstudio/tokens/history").get_json()
    assert body == {"agent_id": None, "rows": []}
