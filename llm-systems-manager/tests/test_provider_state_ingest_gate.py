"""#686: provider-state ingest only accepts providers the agent advertises."""
from __future__ import annotations

import pytest

import agent_registry
import manager_mod as M
import provider_state


@pytest.fixture
def ingest(monkeypatch):
    puts = []
    agent = {"agent_id": "a" * 32, "hostname": "h", "status": "approved",
             "capabilities": {"sysperf": True, "llama": True, "lms": False, "vllm": False}}
    monkeypatch.setattr(agent_registry, "bearer_from_request", lambda: "tok")
    monkeypatch.setattr(agent_registry, "agent_by_token", lambda tok: agent if tok == "tok" else None)
    monkeypatch.setattr(provider_state.STORE, "put", lambda p, aid, s: puts.append((p, aid)))
    monkeypatch.setattr(provider_state.STORE, "mark_online", lambda p, aid: False)
    monkeypatch.setattr(M, "_broadcast_llama_state_if_changed", lambda aid: None)
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        yield c, puts


def test_advertised_provider_is_accepted(ingest):
    c, puts = ingest
    r = c.post("/api/remote/provider-state", json={"provider": "llama", "sample": {"x": 1}})
    assert r.status_code == 200 and puts == [("llama", "a" * 32)]


@pytest.mark.parametrize("prov", ["lms", "vllm"])
def test_unadvertised_provider_is_refused(ingest, prov):
    c, puts = ingest
    r = c.post("/api/remote/provider-state", json={"provider": prov, "sample": {"x": 1}})
    assert r.status_code == 403
    assert "capab" in (r.get_json() or {}).get("error", "")
    assert puts == []


def test_unknown_provider_still_404(ingest):
    c, puts = ingest
    r = c.post("/api/remote/provider-state", json={"provider": "nope", "sample": {}})
    assert r.status_code == 404 and puts == []


@pytest.mark.parametrize("path,prov", [("/api/remote/host-metrics", "llama"), ("/api/remote/lmstudio", "lms")])
def test_legacy_ingest_paths_share_the_gate(ingest, path, prov):
    c, puts = ingest
    r = c.post(path, json={"cpu": 1})
    if prov == "llama":
        assert r.status_code == 200 and puts == [("llama", "a" * 32)]
    else:
        assert r.status_code == 403 and puts == []


def test_non_dict_capabilities_are_refused_not_500(ingest, monkeypatch):
    c, puts = ingest
    agent = {"agent_id": "b" * 32, "hostname": "h", "status": "approved", "capabilities": ["llama"]}
    monkeypatch.setattr(agent_registry, "agent_by_token", lambda tok: agent)
    r = c.post("/api/remote/provider-state", json={"provider": "llama", "sample": {}})
    assert r.status_code == 403 and puts == []


def test_heartbeat_refreshes_capabilities(monkeypatch):
    aid = "c" * 32
    data = {"agents": {aid: {"agent_id": aid, "hostname": "h", "status": "approved",
                             "token": "tok", "capabilities": {"llama": False}}}, "global": {}}
    saved = []
    import auth
    monkeypatch.setattr(agent_registry, "bearer_from_request", lambda: "tok")
    monkeypatch.setattr(agent_registry, "agent_by_token", lambda tok: dict(data["agents"][aid]))
    monkeypatch.setattr(auth, "_bearer_from_request", lambda: "tok", raising=False)
    monkeypatch.setattr(auth, "_agent_by_token", lambda tok: dict(data["agents"][aid]), raising=False)
    monkeypatch.setattr(agent_registry, "load_agents", lambda: data)
    monkeypatch.setattr(agent_registry, "save_agents", lambda d: saved.append(True))
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        r = c.post("/api/agents/heartbeat", json={"capabilities": {"llama": True, "lms": False}})
    assert r.status_code == 200
    assert data["agents"][aid]["capabilities"] == {"llama": True, "lms": False}
    assert saved  # flushed immediately on a capability change
