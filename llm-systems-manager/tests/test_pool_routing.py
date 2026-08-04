# llm-systems-manager/tests/test_pool_routing.py
"""#359: spec-driven pinned_agent / pick_agent / primary_agent.
#479: lms promoted to a pool provider (pool RR + pins)."""
from __future__ import annotations

import agent_registry


A1 = {"agent_id": "a" * 32, "status": "approved", "capabilities": {"llama": True}}
A2 = {"agent_id": "b" * 32, "status": "approved", "capabilities": {"llama": True}}
V1 = {"agent_id": "c" * 32, "status": "approved", "capabilities": {"vllm": True}}
V2 = {"agent_id": "d" * 32, "status": "approved", "capabilities": {"vllm": True}}
L1 = {"agent_id": "e" * 32, "status": "approved", "capabilities": {"lms": True}}
L2 = {"agent_id": "f" * 32, "status": "approved", "capabilities": {"lms": True}}


def _store(glob):
    agents = {a["agent_id"]: a for a in (A1, A2, V1, V2, L1, L2)}
    return {"agents": agents, "global": glob}


def _patch(monkeypatch, glob, live=lambda a: "live"):
    monkeypatch.setattr(agent_registry, "load_agents", lambda: _store(glob))
    monkeypatch.setattr(agent_registry, "agent_liveness", live)
    monkeypatch.setattr(agent_registry, "_pool_rr_index", {}, raising=False)


def test_pinned_agent_reads_spec_pin_key(monkeypatch):
    _patch(monkeypatch, {"vllm_model_pins": {"m1": V2["agent_id"]}})
    assert agent_registry.pinned_agent("vllm", "m1") is not None
    assert agent_registry.pinned_agent("vllm", "m1")["agent_id"] == V2["agent_id"]


def test_pinned_agent_lms_reads_pin_key(monkeypatch):
    # #479: lms is a pin provider — its pin dict resolves like llama/vllm.
    _patch(monkeypatch, {"lms_model_pins": {"m1": L1["agent_id"]}})
    assert agent_registry.pinned_agent("lms", "m1")["agent_id"] == L1["agent_id"]


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


def test_primary_agent_lms_pool_first(monkeypatch):
    # #479: lms pool-picker — pool members win over the legacy primary id.
    _patch(monkeypatch, {"lms_pool": [L1["agent_id"]],
                         "primary_lms_id": L2["agent_id"]})
    assert agent_registry.primary_agent("lms")["agent_id"] == L1["agent_id"]


def test_pick_agent_lms_round_robin(monkeypatch):
    _patch(monkeypatch, {"lms_pool": [L1["agent_id"], L2["agent_id"]]})
    got = [agent_registry.pick_agent("lms")["agent_id"] for _ in range(4)]
    assert got == [L1["agent_id"], L2["agent_id"], L1["agent_id"], L2["agent_id"]]


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
    # No pool and no pin set — resolution still lands on the default agent.
    _patch(monkeypatch, {"default_lms_id": L1["agent_id"]})
    agent, override = proxies._resolve_target("lms", "some-model", None)
    assert agent["agent_id"] == L1["agent_id"]
    assert override is None


def test_resolve_target_lms_pin_beats_picker(monkeypatch):
    _patch(monkeypatch, {"lms_model_pins": {"m1": L2["agent_id"]}})
    agent, override = proxies._resolve_target("lms", "m1", L1["agent_id"])
    assert agent["agent_id"] == L2["agent_id"]
    assert override == "pin"


def test_resolve_target_lms_pool_rr(monkeypatch):
    _patch(monkeypatch, {"lms_pool": [L1["agent_id"], L2["agent_id"]]})
    a1, _ = proxies._resolve_target("lms", None, None)
    a2, _ = proxies._resolve_target("lms", None, None)
    assert {a1["agent_id"], a2["agent_id"]} == {L1["agent_id"], L2["agent_id"]}


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
    assert "/api/agents/<agent_id>/lms-pool" in rules


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
    assert "/api/admin/lms-models" in rules
    assert "/api/admin/lms-pins" in rules


def test_audit_matches_provider_pool_and_pins():
    m = manager_mod._audit_match
    assert m("POST", "/api/agents/abc123/llama-pool") == ("agent.llama-pool", "abc123")
    assert m("POST", "/api/agents/abc123/vllm-pool") == ("agent.vllm-pool", "abc123")
    assert m("POST", "/api/agents/abc123/lms-pool") == ("agent.lms-pool", "abc123")
    assert m("POST", "/api/admin/llama-pins") == ("config.llama-pins", None)
    assert m("POST", "/api/admin/vllm-pins") == ("config.vllm-pins", None)
    assert m("POST", "/api/admin/lms-pins") == ("config.lms-pins", None)


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


def test_agents_list_includes_pool_providers(monkeypatch):
    _patch(monkeypatch, {})
    body = _admin_client().get("/api/agents").get_json()
    got = {p["name"]: p for p in body["pool_providers"]}
    assert set(got) == {"llama", "vllm", "lms"}
    assert got["vllm"]["label"] == "vLLM"
    assert got["vllm"]["pin_key"] == "vllm_model_pins"
    assert got["llama"]["pin_key"] == "llama_model_pins"
    assert got["lms"]["label"] == "LM Studio"
    assert got["lms"]["pin_key"] == "lms_model_pins"


def test_lms_pins_roundtrip(monkeypatch):
    _patch(monkeypatch, {})
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    c = _admin_client()
    r = c.post("/api/admin/lms-pins",
               json={"model_id": "m1", "agent_id": L1["agent_id"]})
    assert r.status_code == 200
    assert r.get_json()["lms_model_pins"] == {"m1": L1["agent_id"]}
    assert saved[-1]["global"]["lms_model_pins"] == {"m1": L1["agent_id"]}
    # llama-only agent -> capability 400
    r = c.post("/api/admin/lms-pins",
               json={"model_id": "m1", "agent_id": A1["agent_id"]})
    assert r.status_code == 400
    assert "lms" in r.get_json()["error"]


def test_lms_pool_route_adds_and_checks_capability(monkeypatch):
    _patch(monkeypatch, {})
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    c = _admin_client()
    r = c.post(f"/api/agents/{L1['agent_id']}/lms-pool", json={"in_pool": True})
    assert r.status_code == 200
    assert r.get_json()["lms_pool"] == [L1["agent_id"]]
    r = c.post(f"/api/agents/{A1['agent_id']}/lms-pool", json={"in_pool": True})
    assert r.status_code == 400
    assert "lms" in r.get_json()["error"]


def test_autopilot_set_pin_writes_lms_pins(monkeypatch):
    # #479: lms has a pin_dict_key, so autopilot pin placement is live.
    import autopilot
    monkeypatch.setattr(agent_registry, "load_agents", lambda: _store({}))
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    autopilot._prod_set_pin("lms", "m1", L1["agent_id"])
    assert saved and saved[-1]["global"]["lms_model_pins"] == {"m1": L1["agent_id"]}


def test_lmstudio_load_unload_pass_model_id(monkeypatch):
    # #479: model ops carry model_id so pins can steer them (llama parity).
    calls = []
    def fake_proxy(kind, method, path, **kw):
        calls.append((kind, path, kw.get("model_id")))
        return manager_mod.jsonify({"ok": True})
    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    c = _admin_client()
    c.post("/api/lmstudio/load", json={"model": "m1"})
    c.post("/api/lmstudio/unload", json={"model": "m1"})
    assert [x[2] for x in calls] == ["m1", "m1"]


def test_pins_route_rejects_unapproved_agent(monkeypatch):
    pending = {"agent_id": "9" * 32, "status": "pending",
               "capabilities": {"llama": True}}
    agents = {a["agent_id"]: a for a in (A1, A2, pending)}
    monkeypatch.setattr(agent_registry, "load_agents",
                        lambda: {"agents": agents, "global": {}})
    saved = []
    monkeypatch.setattr(agent_registry, "save_agents",
                        lambda data: saved.append(data))
    c = _admin_client()
    r = c.post("/api/admin/llama-pins",
               json={"model_id": "m1", "agent_id": pending["agent_id"]})
    assert r.status_code == 400
    assert "approved" in r.get_json()["error"]
    assert saved == []


def test_pinned_agent_stale_pin_warns_once(monkeypatch, caplog):
    import logging
    _patch(monkeypatch, {"llama_model_pins": {"m1": A1["agent_id"]}},
           live=lambda a: "down")
    agent_registry._stale_pin_warned.clear()
    with caplog.at_level(logging.WARNING):
        assert agent_registry.pinned_agent("llama", "m1") is None
        assert agent_registry.pinned_agent("llama", "m1") is None
    warns = [r for r in caplog.records if "pinned to agent" in r.message]
    assert len(warns) == 1
    # Pin resolving again re-arms the warning.
    _patch(monkeypatch, {"llama_model_pins": {"m1": A1["agent_id"]}})
    assert agent_registry.pinned_agent("llama", "m1") is not None
    assert not agent_registry._stale_pin_warned
