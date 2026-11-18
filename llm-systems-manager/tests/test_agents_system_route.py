"""#1432: GET /api/agents/<id>/system returns the live system sample of one host."""
from __future__ import annotations

import pytest

import agent_registry
import auth
import manager_mod as M
import provider_state

A1, A2, A3 = "systest-a1", "systest-a2", "systest-a3"
DATA = {"agents": {
    A1: {"status": "approved", "hostname": "alpha", "capabilities": {"sysperf": True}},
    A2: {"status": "approved", "hostname": "beta", "capabilities": {"sysperf": True}},
    A3: {"status": "pending", "hostname": "gamma", "capabilities": {"sysperf": True}},
}}


@pytest.fixture(autouse=True)
def _clean_store():
    yield
    for aid in (A1, A2, A3):
        provider_state.STORE.evict(aid)


def _get(monkeypatch, aid):
    monkeypatch.setattr(agent_registry, "load_agents", lambda *a, **k: DATA)
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "operator"
    return c.get(f"/api/agents/{aid}/system")


def test_returns_system_sample(monkeypatch):
    provider_state.STORE.put("system", A2, {"system": {"cpu_total": 12.5}})
    r = _get(monkeypatch, A2)
    assert r.status_code == 200
    j = r.get_json()
    assert j["sample"]["system"]["cpu_total"] == 12.5
    assert j["online"] is True
    assert 0 <= j["age_s"] < 10


def test_never_pushed(monkeypatch):
    j = _get(monkeypatch, A1).get_json()
    assert j["sample"] == {}
    assert j["online"] is False
    assert j["age_s"] is None


def test_unknown_and_pending_404(monkeypatch):
    for aid in ("no-such-host", A3):
        r = _get(monkeypatch, aid)
        assert r.status_code == 404
        assert r.get_json() == {"ok": False, "error": "unknown host"}


def test_operator_role_allowed():
    with M.app.test_request_context("/api/agents/abc/system"):
        assert auth._operator_denied("/api/agents/abc/system") is False
        assert auth._operator_denied("/api/agents/abc/restart") is True


def test_llama_host_falls_back_to_llama_bucket(monkeypatch):
    provider_state.STORE.put("llama", A1, {"cpu_total": 33.0, "ram": {"percent": 40.0}, "llama": {"tokens_per_second": 9.0}})
    j = _get(monkeypatch, A1).get_json()
    assert j["online"] is True
    assert 0 <= j["age_s"] < 10
    assert j["sample"]["cpu_total"] == 33.0
    assert j["sample"]["ram"]["percent"] == 40.0


def test_lms_and_vllm_hosts_return_the_system_block(monkeypatch):
    provider_state.STORE.put("lms", A1, {"ts": 1700000000, "models": [], "system": {"cpu_total": 21.0},
                                         "mac_power": {"gpu_w": 3.5}})
    j = _get(monkeypatch, A1).get_json()
    assert j["online"] is True
    assert j["sample"] == {"cpu_total": 21.0, "mac_power": {"gpu_w": 3.5}, "ts": 1700000000}
    provider_state.STORE.put("vllm", A2, {"vllm": {"state": "running"}, "system": {"cpu_total": 7.0}})
    j = _get(monkeypatch, A2).get_json()
    assert j["online"] is True
    assert j["sample"] == {"cpu_total": 7.0}


def test_newest_bucket_wins_and_stale_host_is_offline(monkeypatch):
    provider_state.STORE.put("system", A1, {"cpu_total": 1.0})
    provider_state.STORE.put("llama", A1, {"cpu_total": 2.0})
    with provider_state.STORE._lock:
        provider_state.STORE._samples["system"][A1]["last_seen"] -= 120
        provider_state.STORE._samples["llama"][A1]["last_seen"] -= 100
    j = _get(monkeypatch, A1).get_json()
    assert j["sample"]["cpu_total"] == 2.0
    assert j["online"] is False
    assert 95 <= j["age_s"] < 110
