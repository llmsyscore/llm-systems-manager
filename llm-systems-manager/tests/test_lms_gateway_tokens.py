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


def test_metric_points_rates_from_consecutive_calls():
    aid = "test-502-push-a"
    hosts = {aid: "mac-host"}
    gateway_usage.record(aid, 100, 200)
    first = gateway_usage.metric_points(hosts, now=1000.0)
    by_name = {p["metric_name"]: p for p in first}
    assert by_name["lms_tokens_per_second"]["value"] == 0.0   # no prior snapshot
    assert by_name["lms_gen_tokens_total"]["value"] == 200.0
    assert by_name["lms_gen_tokens_total"]["hostname"] == "mac-host"
    assert by_name["lms_gen_tokens_total"]["source"] == "gateway"
    assert by_name["lms_gen_tokens_total"]["timestamp"].endswith("+00:00")
    gateway_usage.record(aid, 50, 300)   # +50 prompt, +300 gen
    second = gateway_usage.metric_points(hosts, now=1010.0)
    by_name = {p["metric_name"]: p for p in second}
    assert by_name["lms_tokens_per_second"]["value"] == 30.0
    assert by_name["lms_prompt_tokens_per_second"]["value"] == 5.0


def test_metric_points_counter_reset_reseeds():
    aid = "test-502-push-b"
    hosts = {aid: "mac-host"}
    gateway_usage.record(aid, 10, 10)
    gateway_usage.metric_points(hosts, now=2000.0)
    with gateway_usage._lock:
        gateway_usage._counters[aid] = {"gen": 1, "prompt": 1}
    by_name = {p["metric_name"]: p
               for p in gateway_usage.metric_points(hosts, now=2010.0)}
    assert by_name["lms_tokens_per_second"]["value"] == 0.0
    gateway_usage.record(aid, 4, 9)
    by_name = {p["metric_name"]: p
               for p in gateway_usage.metric_points(hosts, now=2020.0)}
    assert by_name["lms_tokens_per_second"]["value"] == 0.9
    assert by_name["lms_prompt_tokens_per_second"]["value"] == 0.4


def test_metric_points_emit_zeros_for_idle_agents():
    # An approved LMS agent with no gateway traffic still gets a series.
    by_name = {p["metric_name"]: p for p in gateway_usage.metric_points(
        {"test-502-push-idle": "quiet-mac"}, now=3000.0)}
    assert by_name["lms_gen_tokens_total"]["value"] == 0.0
    assert by_name["lms_tokens_per_second"]["value"] == 0.0


def test_lms_agent_hosts_filters_approved_lms(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents", lambda: {
        "agents": {
            "a1": {"status": "approved", "hostname": "mac-1",
                   "capabilities": {"lms": True}},
            "a2": {"status": "approved", "hostname": "linux-1",
                   "capabilities": {"lms": False, "llama": True}},
            "a3": {"status": "pending", "hostname": "mac-2",
                   "capabilities": {"lms": True}},
        }})
    assert manager_mod._lms_agent_hosts() == {"a1": "mac-1"}
