"""#412: operator-designated manager host agent.

Under Docker the manager's own hostname is a container id and the native host
agent arrives via the bridge gateway, so hostname/loopback matching never
fires. An explicit global.host_agent_id must let self_agent_id() /
colocated_infra() resolve it anyway (host metrics + version pills).
"""
from __future__ import annotations

import types

import agent_registry
import manager_mod


# Host keys deliberately unlike the manager host + a bridge-gateway source IP,
# so ONLY an explicit designation can resolve this agent (the Docker case).
HOST_AGENT = {
    "agent_id": "h" * 32,
    "status": "approved",
    "hostname": "container-host-not-the-manager",
    "registered_from": "172.18.0.1",
    "capabilities": {"llama": True},
}


def _store(agents, glob):
    return {"agents": {a["agent_id"]: a for a in agents}, "global": glob}


def test_designated_host_agent_id_reads_global(monkeypatch):
    monkeypatch.setattr(agent_registry, "load_agents",
                        lambda: _store([HOST_AGENT], {"host_agent_id": HOST_AGENT["agent_id"]}))
    assert agent_registry.designated_host_agent_id() == HOST_AGENT["agent_id"]


def test_designated_host_agent_id_none_when_unset(monkeypatch):
    monkeypatch.setattr(agent_registry, "load_agents", lambda: _store([HOST_AGENT], {}))
    assert agent_registry.designated_host_agent_id() is None


def test_self_agent_id_prefers_designated(monkeypatch):
    monkeypatch.setattr(agent_registry, "load_agents",
                        lambda: _store([HOST_AGENT], {"host_agent_id": HOST_AGENT["agent_id"]}))
    assert agent_registry.self_agent_id() == HOST_AGENT["agent_id"]


def test_self_agent_id_ignores_designated_when_not_approved(monkeypatch):
    pending = dict(HOST_AGENT, status="pending")
    monkeypatch.setattr(agent_registry, "load_agents",
                        lambda: _store([pending], {"host_agent_id": pending["agent_id"]}))
    # Not approved -> designation ignored; host keys don't match -> None.
    assert agent_registry.self_agent_id() is None


def test_colocated_infra_designated_shows_manager_pill(monkeypatch):
    monkeypatch.setattr(agent_registry, "load_agents",
                        lambda: _store([HOST_AGENT], {"host_agent_id": HOST_AGENT["agent_id"]}))
    infra = agent_registry.colocated_infra(HOST_AGENT, HOST_AGENT["agent_id"])
    roles = {p["role"] for p in infra}
    assert "manager" in roles
    # A designated agent stands in for the whole control plane, so a configured
    # AE surfaces its pill even behind a compose service name it can't match.
    if agent_registry._deps.alarm_engine_url() or "":
        assert "alarm_engine" in roles


def test_colocated_infra_not_designated_no_manager_pill(monkeypatch):
    monkeypatch.setattr(agent_registry, "load_agents", lambda: _store([HOST_AGENT], {}))
    infra = agent_registry.colocated_infra(HOST_AGENT, None)
    assert "manager" not in {p["role"] for p in infra}


def test_set_host_route_sets_then_clears(monkeypatch):
    store = _store([HOST_AGENT], {})
    monkeypatch.setattr(agent_registry, "load_agents", lambda: store)
    monkeypatch.setattr(agent_registry, "save_agents", lambda d: None)
    monkeypatch.setattr(agent_registry, "_deps",
                        types.SimpleNamespace(require_admin=lambda: None))
    aid = HOST_AGENT["agent_id"]
    with manager_mod.app.test_request_context(json={"set": True}):
        resp = agent_registry._agents_set_host(aid)
    assert resp.get_json()["host_agent_id"] == aid
    assert store["global"]["host_agent_id"] == aid
    with manager_mod.app.test_request_context(json={"set": False}):
        agent_registry._agents_set_host(aid)
    assert store["global"].get("host_agent_id") is None


def test_set_host_route_rejects_unapproved(monkeypatch):
    pending = dict(HOST_AGENT, status="pending")
    store = _store([pending], {})
    monkeypatch.setattr(agent_registry, "load_agents", lambda: store)
    monkeypatch.setattr(agent_registry, "save_agents", lambda d: None)
    monkeypatch.setattr(agent_registry, "_deps",
                        types.SimpleNamespace(require_admin=lambda: None))
    with manager_mod.app.test_request_context(json={"set": True}):
        resp = agent_registry._agents_set_host(pending["agent_id"])
    body = resp[0] if isinstance(resp, tuple) else resp
    status = resp[1] if isinstance(resp, tuple) else 200
    assert status == 400
    assert body.get_json()["ok"] is False
    assert "host_agent_id" not in store["global"]
