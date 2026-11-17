"""#1432: GET /api/agents/hosts lists every approved host with providers and liveness."""
from __future__ import annotations

import time

import pytest

import agent_registry
import auth
import manager_mod as M
import provider_state

A1, A2, A3 = "hosts-test-a1", "hosts-test-a2", "hosts-test-a3"


def _agents(hostnames=None):
    hostnames = hostnames or {A1: "alpha", A2: "beta", A3: "gamma"}
    return {"agents": {
        A1: {"status": "approved", "hostname": hostnames[A1],
             "capabilities": {"llama": True, "sysperf": True}},
        A2: {"status": "approved", "hostname": hostnames[A2],
             "capabilities": {"sysperf": True}},
        A3: {"status": "pending", "hostname": hostnames[A3],
             "capabilities": {"llama": True, "sysperf": True}},
    }}


def _push(monkeypatch, bucket, aid, ago_s):
    """Seeds a STORE sample through put() with the clock set ago_s seconds back."""
    stamp = time.time() - ago_s
    with monkeypatch.context() as m:
        m.setattr(provider_state.time, "time", lambda: stamp)
        provider_state.STORE.put(bucket, aid, {})


@pytest.fixture(autouse=True)
def _clean_store():
    yield
    for aid in (A1, A2, A3):
        provider_state.STORE.evict(aid)


def _get(monkeypatch, data):
    monkeypatch.setattr(agent_registry, "load_agents", lambda *a, **k: data)
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    r = c.get("/api/agents/hosts")
    assert r.status_code == 200
    return r.get_json()["hosts"]


def _by_id(hosts):
    return {h["agent_id"]: h for h in hosts}


def test_lists_every_approved_host_including_system_only(monkeypatch):
    hosts = _by_id(_get(monkeypatch, _agents()))
    assert set(hosts) == {A1, A2}
    assert hosts[A1]["providers"] == ["llama"]
    assert hosts[A2]["providers"] == []


def test_online_from_newest_bucket(monkeypatch):
    _push(monkeypatch, M.SYSTEM_PROVIDER, A2, 5)
    hosts = _by_id(_get(monkeypatch, _agents()))
    assert hosts[A2]["online"] is True
    assert 0 <= hosts[A2]["age_s"] < 10
    assert hosts[A1]["online"] is False
    assert hosts[A1]["age_s"] is None


def test_stale_system_push_is_offline(monkeypatch):
    _push(monkeypatch, M.SYSTEM_PROVIDER, A2, 120)
    hosts = _by_id(_get(monkeypatch, _agents()))
    assert hosts[A2]["online"] is False
    assert hosts[A2]["age_s"] == pytest.approx(120, abs=2)


def test_fresh_provider_push_wins_over_stale_system(monkeypatch):
    _push(monkeypatch, "llama", A1, 5)
    _push(monkeypatch, M.SYSTEM_PROVIDER, A1, 120)
    hosts = _by_id(_get(monkeypatch, _agents()))
    assert hosts[A1]["online"] is True
    assert 0 <= hosts[A1]["age_s"] < 10


def test_sorted_by_hostname(monkeypatch):
    data = _agents({A1: "b", A2: "A", A3: "x"})
    data["agents"][A3]["status"] = "approved"
    data["agents"][A3]["hostname"] = None
    hosts = _get(monkeypatch, data)
    assert [h["hostname"] for h in hosts] == ["A", "b", None]


def test_operator_role_allowed():
    assert auth._operator_denied("/api/agents/hosts") is False
    assert auth._operator_denied("/api/agents/metrics") is True
