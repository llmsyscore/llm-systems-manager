# llm-systems-manager/tests/test_agent_refs_cleanup.py
"""#562/#563: token-authenticated re-registration after an IP change, and
dangling agent-id reference cleanup (delete cascade + load-time reconcile)."""
from __future__ import annotations

import json

import pytest

import agent_registry
import manager_mod


TOK = "t" * 64


def _agent(aid, hostname, *, token=None, registered_from="192.0.2.10",
           caps=None, status="approved"):
    return {
        "agent_id": aid, "hostname": hostname, "os": "linux",
        "status": status, "token": token,
        "capabilities": caps or {"llama": True},
        "bind_url": f"http://{registered_from}:8081",
        "fingerprint": "sha256:old",
        "registered_from": registered_from,
    }


@pytest.fixture
def registry_file(tmp_path, monkeypatch):
    """Install a real tmp agents.json and reset the module cache around it."""
    def _install(data):
        f = tmp_path / "agents.json"
        f.write_text(json.dumps(data))
        monkeypatch.setattr(agent_registry, "AGENTS_FILE", f)
        with agent_registry._agents_cache_lock:
            agent_registry._agents_cache.update(
                {"mtime": 0.0, "data": None, "by_token": {}})
        return f
    yield _install
    with agent_registry._agents_cache_lock:
        agent_registry._agents_cache.update(
            {"mtime": 0.0, "data": None, "by_token": {}})


def _admin_client():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


# ── re-registration auth after an IP change ──────────────────────────
def test_reregister_with_token_from_new_ip_updates_record(registry_file):
    f = registry_file({"agents": {"aid-1": _agent("aid-1", "h1", token=TOK)},
                       "global": {}})
    c = manager_mod.app.test_client()
    r = c.post("/api/agents/register",
               json={"hostname": "h1", "os": "linux",
                     "bind_url": "http://192.0.2.99:8081",
                     "fingerprint": "sha256:new-boot"},
               headers={"Authorization": f"Bearer {TOK}"},
               environ_base={"REMOTE_ADDR": "192.0.2.99"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["agent_id"] == "aid-1"
    assert body["token"] == TOK
    saved = json.loads(f.read_text())["agents"]["aid-1"]
    assert saved["registered_from"] == "192.0.2.99"
    assert saved["bind_url"] == "http://192.0.2.99:8081"
    assert saved["fingerprint"] == "sha256:new-boot"


def test_reregister_without_any_matching_factor_still_403(registry_file):
    registry_file({"agents": {"aid-1": _agent("aid-1", "h1", token=TOK)},
                   "global": {}})
    c = manager_mod.app.test_client()
    r = c.post("/api/agents/register",
               json={"hostname": "h1", "os": "linux",
                     "bind_url": "http://192.0.2.99:8081",
                     "fingerprint": "sha256:different"},
               environ_base={"REMOTE_ADDR": "192.0.2.99"})
    assert r.status_code == 403
    assert "re-registration" in r.get_json()["error"]


# ── delete cascade ───────────────────────────────────────────────────
def test_delete_cascades_all_global_refs(registry_file):
    f = registry_file({
        "agents": {"keep": _agent("keep", "h-keep"),
                   "victim": _agent("victim", "h-victim")},
        "global": {
            "primary_llama_id": "victim", "default_llama_id": "victim",
            "primary_vllm_id": "keep",
            "host_agent_id": "victim",
            "llama_pool": ["victim", "keep"],
            "llama_model_pins": {"m1": "victim", "m2": "keep"},
        },
    })
    r = _admin_client().delete("/api/agents/victim")
    assert r.status_code == 200
    saved = json.loads(f.read_text())
    assert "victim" not in saved["agents"]
    g = saved["global"]
    assert g["primary_llama_id"] == ""
    assert g["default_llama_id"] == ""
    assert g["primary_vllm_id"] == "keep"
    assert "host_agent_id" not in g
    assert g["llama_pool"] == ["keep"]
    assert g["llama_model_pins"] == {"m2": "keep"}


# ── load-time reconcile (self-heal for already-dangling files) ───────
def test_load_agents_prunes_dangling_refs_and_persists(registry_file):
    f = registry_file({
        "agents": {"keep": _agent("keep", "h-keep")},
        "global": {
            "primary_llama_id": "ghost1", "default_llama_id": "ghost1",
            "host_agent_id": "ghost2",
            "llama_pool": ["ghost1", "keep"],
            "llama_model_pins": {"m1": "ghost1", "m2": "keep"},
        },
    })
    data = agent_registry.load_agents()
    g = data["global"]
    assert g["primary_llama_id"] == ""
    assert g["default_llama_id"] == ""
    assert "host_agent_id" not in g
    assert g["llama_pool"] == ["keep"]
    assert g["llama_model_pins"] == {"m2": "keep"}
    # Pruned state was written back to disk, not just cached.
    on_disk = json.loads(f.read_text())["global"]
    assert on_disk["primary_llama_id"] == ""
    assert on_disk["llama_pool"] == ["keep"]


def test_load_agents_keeps_valid_refs(registry_file):
    registry_file({
        "agents": {"keep": _agent("keep", "h-keep")},
        "global": {
            "primary_llama_id": "keep",
            "host_agent_id": "keep",
            "llama_pool": ["keep"],
            "llama_model_pins": {"m1": "keep"},
        },
    })
    g = agent_registry.load_agents()["global"]
    assert g["primary_llama_id"] == "keep"
    assert g["host_agent_id"] == "keep"
    assert g["llama_pool"] == ["keep"]
    assert g["llama_model_pins"] == {"m1": "keep"}


def test_reconcile_after_delete_unblocks_auto_promotion(registry_file):
    # #563: a dangling primary no longer blocks first-approval auto-promotion.
    registry_file({
        "agents": {"fresh": _agent("fresh", "h-fresh", status="pending")},
        "global": {"primary_llama_id": "ghost1", "default_llama_id": "ghost1"},
    })
    r = _admin_client().post("/api/agents/fresh/approve")
    assert r.status_code == 200
    assert "llama" in r.get_json()["auto_primary"]
    g = agent_registry.load_agents()["global"]
    assert g["primary_llama_id"] == "fresh"


def test_reconcile_resets_dangling_autopilot_placement(registry_file):
    f = registry_file({
        "agents": {"keep": _agent("keep", "h-keep")},
        "global": {"autopilot": {"enabled": True, "entries": [
            {"model": "m1", "provider": "llama", "placement": "ghost1"},
            {"model": "m2", "provider": "llama", "placement": "auto"},
            {"model": "m3", "provider": "llama", "placement": "keep"},
        ], "hosts": {}}},
    })
    entries = agent_registry.load_agents()["global"]["autopilot"]["entries"]
    assert [e["placement"] for e in entries] == ["auto", "auto", "keep"]
    on_disk = json.loads(f.read_text())["global"]["autopilot"]["entries"]
    assert on_disk[0]["placement"] == "auto"


def test_delete_survives_corrupt_global_shapes(registry_file):
    # Hand-edited garbage shapes must not break the delete or the reconcile.
    f = registry_file({
        "agents": {"keep": _agent("keep", "h-keep"),
                   "victim": _agent("victim", "h-victim")},
        "global": {
            "primary_llama_id": ["not", "a", "string"],
            "llama_model_pins": ["not-a-dict"],
            "llama_pool": "not-a-list",
            "vllm_pool": ["victim", None, 42, "keep"],
            "autopilot": {"entries": "not-a-list"},
        },
    })
    r = _admin_client().delete("/api/agents/victim")
    assert r.status_code == 200
    saved = json.loads(f.read_text())
    assert "victim" not in saved["agents"]
    g = saved["global"]
    # Corrupt shapes are left alone; well-formed refs are still cleaned.
    assert g["primary_llama_id"] == ["not", "a", "string"]
    assert g["llama_model_pins"] == ["not-a-dict"]
    assert g["llama_pool"] == "not-a-list"
    assert g["vllm_pool"] == ["keep"]


# ── pool removal of unknown ids ──────────────────────────────────────
def test_set_pool_membership_removes_unknown_id(monkeypatch):
    known = _agent("keep", "h-keep", caps={"vllm": True})
    store = {"agents": {"keep": known},
             "global": {"vllm_pool": ["ghost", "keep"]}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: store)
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    ok, err, pool, hostname = agent_registry.set_pool_membership(
        "ghost", "vllm", False)
    assert ok and err is None
    assert pool == ["keep"]
    assert hostname is None
    assert saved[-1]["global"]["vllm_pool"] == ["keep"]


def test_set_pool_membership_still_rejects_unknown_add(monkeypatch):
    monkeypatch.setattr(agent_registry, "load_agents",
                        lambda: {"agents": {}, "global": {}})
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    ok, err, pool, hostname = agent_registry.set_pool_membership(
        "ghost", "vllm", True)
    assert not ok
    assert err == "unknown agent"
    assert not saved
