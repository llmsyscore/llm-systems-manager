# llm-systems-manager/tests/test_pool_routing.py
"""#359: spec-driven pinned_agent / pick_agent / primary_agent."""
from __future__ import annotations

import agent_registry


A1 = {"agent_id": "a" * 32, "status": "approved", "capabilities": {"llama": True}}
A2 = {"agent_id": "b" * 32, "status": "approved", "capabilities": {"llama": True}}
V1 = {"agent_id": "c" * 32, "status": "approved", "capabilities": {"vllm": True}}
V2 = {"agent_id": "d" * 32, "status": "approved", "capabilities": {"vllm": True}}


def _store(glob):
    agents = {a["agent_id"]: a for a in (A1, A2, V1, V2)}
    return {"agents": agents, "global": glob}


def _patch(monkeypatch, glob, live=lambda a: "live"):
    monkeypatch.setattr(agent_registry, "load_agents", lambda: _store(glob))
    monkeypatch.setattr(agent_registry, "agent_liveness", live)
    monkeypatch.setattr(agent_registry, "_pool_rr_index", {}, raising=False)


def test_pinned_agent_reads_spec_pin_key(monkeypatch):
    _patch(monkeypatch, {"vllm_model_pins": {"m1": V2["agent_id"]}})
    assert agent_registry.pinned_agent("vllm", "m1") is not None
    assert agent_registry.pinned_agent("vllm", "m1")["agent_id"] == V2["agent_id"]


def test_pinned_agent_none_without_pin_key(monkeypatch):
    # lms spec has no pin_dict_key -> always None, even with a stray dict.
    _patch(monkeypatch, {"lms_model_pins": {"m1": A1["agent_id"]}})
    assert agent_registry.pinned_agent("lms", "m1") is None


def test_pinned_agent_not_live_falls_back(monkeypatch):
    _patch(monkeypatch, {"vllm_model_pins": {"m1": V2["agent_id"]}},
           live=lambda a: "stale")
    assert agent_registry.pinned_agent("vllm", "m1") is None


def test_pick_agent_round_robin_rotates(monkeypatch):
    _patch(monkeypatch, {"vllm_pool": [V1["agent_id"], V2["agent_id"]]})
    got = [agent_registry.pick_agent("vllm")["agent_id"] for _ in range(4)]
    assert got == [V1["agent_id"], V2["agent_id"], V1["agent_id"], V2["agent_id"]]


def test_pick_agent_rr_independent_per_provider(monkeypatch):
    _patch(monkeypatch, {"llama_pool": [A1["agent_id"], A2["agent_id"]],
                         "vllm_pool": [V1["agent_id"], V2["agent_id"]]})
    assert agent_registry.pick_agent("llama")["agent_id"] == A1["agent_id"]
    # llama's rotation must not advance vllm's index.
    assert agent_registry.pick_agent("vllm")["agent_id"] == V1["agent_id"]
    assert agent_registry.pick_agent("llama")["agent_id"] == A2["agent_id"]
    assert agent_registry.pick_agent("vllm")["agent_id"] == V2["agent_id"]


def test_pick_agent_pin_precedes_pool(monkeypatch):
    _patch(monkeypatch, {"vllm_pool": [V1["agent_id"], V2["agent_id"]],
                         "vllm_model_pins": {"m1": V2["agent_id"]}})
    assert agent_registry.pick_agent("vllm", "m1")["agent_id"] == V2["agent_id"]


def test_pick_agent_stale_pool_still_serves(monkeypatch):
    # All pool members stale -> approved-but-not-live are still candidates.
    _patch(monkeypatch, {"vllm_pool": [V1["agent_id"]]}, live=lambda a: "stale")
    assert agent_registry.pick_agent("vllm")["agent_id"] == V1["agent_id"]


def test_pick_agent_empty_pool_falls_back_to_primary(monkeypatch):
    _patch(monkeypatch, {"vllm_pool": [], "primary_vllm_id": V1["agent_id"]})
    assert agent_registry.pick_agent("vllm")["agent_id"] == V1["agent_id"]


def test_primary_agent_pool_first_for_pool_pickers(monkeypatch):
    _patch(monkeypatch, {"vllm_pool": [V2["agent_id"]],
                         "primary_vllm_id": V1["agent_id"]})
    assert agent_registry.primary_agent("vllm")["agent_id"] == V2["agent_id"]


def test_primary_agent_lms_ignores_pool_key(monkeypatch):
    # first_approved picker: a stray lms_pool key must not affect resolution.
    _patch(monkeypatch, {"lms_pool": [A1["agent_id"]],
                         "primary_lms_id": A2["agent_id"]})
    assert agent_registry.primary_agent("lms")["agent_id"] == A2["agent_id"]


def test_llama_behavior_unchanged(monkeypatch):
    _patch(monkeypatch, {"llama_pool": [A1["agent_id"], A2["agent_id"]],
                         "llama_model_pins": {"q": A2["agent_id"]}})
    assert agent_registry.pick_agent("llama", "q")["agent_id"] == A2["agent_id"]
    assert agent_registry.pick_agent("llama")["agent_id"] == A1["agent_id"]
    assert agent_registry.primary_agent("llama")["agent_id"] == A1["agent_id"]


# --- proxies._resolve_target (spec-driven) ---
import proxies


def test_resolve_target_vllm_pin_beats_picker(monkeypatch):
    _patch(monkeypatch, {"vllm_model_pins": {"m1": V2["agent_id"]}})
    agent, override = proxies._resolve_target("vllm", "m1", V1["agent_id"])
    assert agent["agent_id"] == V2["agent_id"]
    assert override == "pin"


def test_resolve_target_vllm_pool_rr(monkeypatch):
    _patch(monkeypatch, {"vllm_pool": [V1["agent_id"], V2["agent_id"]]})
    a1, _ = proxies._resolve_target("vllm", None, None)
    a2, _ = proxies._resolve_target("vllm", None, None)
    assert {a1["agent_id"], a2["agent_id"]} == {V1["agent_id"], V2["agent_id"]}


def test_resolve_target_stream_skips_pool(monkeypatch):
    _patch(monkeypatch, {"vllm_pool": [V1["agent_id"], V2["agent_id"]],
                         "default_vllm_id": V2["agent_id"]})
    for _ in range(3):
        agent, _o = proxies._resolve_target("vllm", None, None, allow_pool=False)
        assert agent["agent_id"] == V2["agent_id"]


def test_resolve_target_lms_picker_then_default(monkeypatch):
    lms_agent = {"agent_id": "e" * 32, "status": "approved",
                 "capabilities": {"lms": True}}
    store = _store({"default_lms_id": lms_agent["agent_id"]})
    store["agents"][lms_agent["agent_id"]] = lms_agent
    monkeypatch.setattr(agent_registry, "load_agents", lambda: store)
    monkeypatch.setattr(agent_registry, "agent_liveness", lambda a: "live")
    agent, override = proxies._resolve_target("lms", "some-model", None)
    assert agent["agent_id"] == lms_agent["agent_id"]
    assert override is None


# --- provider pool routes (registered into the manager app) ---
import manager_mod


def _admin_client():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_pool_routes_registered_per_pool_provider():
    rules = {str(r) for r in manager_mod.app.url_map.iter_rules()}
    assert "/api/agents/<agent_id>/llama-pool" in rules
    assert "/api/agents/<agent_id>/vllm-pool" in rules
    assert "/api/agents/<agent_id>/lms-pool" not in rules


def test_vllm_pool_route_checks_vllm_capability(monkeypatch):
    _patch(monkeypatch, {})
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    r = _admin_client().post(f"/api/agents/{A1['agent_id']}/vllm-pool",
                             json={"in_pool": True})
    assert r.status_code == 400
    assert "vllm" in r.get_json()["error"]
    assert not saved


def test_vllm_pool_route_adds_and_removes(monkeypatch):
    _patch(monkeypatch, {})
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    c = _admin_client()
    r = c.post(f"/api/agents/{V1['agent_id']}/vllm-pool", json={"in_pool": True})
    assert r.status_code == 200
    assert r.get_json()["vllm_pool"] == [V1["agent_id"]]
    assert saved[-1]["global"]["vllm_pool"] == [V1["agent_id"]]


# --- manager admin routes + audit table ---
def test_admin_provider_routes_registered():
    rules = {str(r) for r in manager_mod.app.url_map.iter_rules()}
    assert "/api/admin/llama-models" in rules
    assert "/api/admin/llama-pins" in rules
    assert "/api/admin/vllm-models" in rules
    assert "/api/admin/vllm-pins" in rules
    assert "/api/admin/lms-pins" not in rules


def test_audit_matches_provider_pool_and_pins():
    m = manager_mod._audit_match
    assert m("POST", "/api/agents/abc123/llama-pool") == ("agent.llama-pool", "abc123")
    assert m("POST", "/api/agents/abc123/vllm-pool") == ("agent.vllm-pool", "abc123")
    assert m("POST", "/api/admin/llama-pins") == ("config.llama-pins", None)
    assert m("POST", "/api/admin/vllm-pins") == ("config.vllm-pins", None)


def test_vllm_pins_roundtrip(monkeypatch):
    _patch(monkeypatch, {})
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    c = _admin_client()
    r = c.post("/api/admin/vllm-pins",
               json={"model_id": "m1", "agent_id": V1["agent_id"]})
    assert r.status_code == 200
    assert r.get_json()["vllm_model_pins"] == {"m1": V1["agent_id"]}
    assert saved[-1]["global"]["vllm_model_pins"] == {"m1": V1["agent_id"]}
    # llama-only agent -> capability 400
    r = c.post("/api/admin/vllm-pins",
               json={"model_id": "m1", "agent_id": A1["agent_id"]})
    assert r.status_code == 400
    assert "vllm" in r.get_json()["error"]
