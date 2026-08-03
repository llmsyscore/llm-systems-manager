"""#472: desired-state validation + normalization."""
from __future__ import annotations
import pytest
import agent_registry
import autopilot as ap

def test_minimal_entry_normalizes_defaults():
    st = ap.validate_state({"enabled": True, "entries": [
        {"model": "m1", "provider": "llama"}], "hosts": {}})
    e = st["entries"][0]
    assert e["failover"] == "semi" and e["min_replicas"] == 1
    assert e["max_replicas"] == 1 and e["placement"] == "auto"
    assert "autoscale" not in e

def test_autoscale_defaults_applied_when_range_open():
    st = ap.validate_state({"enabled": False, "entries": [
        {"model": "m1", "provider": "llama", "max_replicas": 3}], "hosts": {}})
    a = st["entries"][0]["autoscale"]
    assert a == {"target_saturation": 0.75, "up_window_s": 120,
                 "down_window_s": 900}

@pytest.mark.parametrize("bad", [
    {"model": "", "provider": "llama"},
    {"model": "m", "provider": "ollama"},
    {"model": "m", "provider": "llama", "failover": "yolo"},
    {"model": "m", "provider": "llama", "min_replicas": 0},
    {"model": "m", "provider": "llama", "min_replicas": 3, "max_replicas": 2},
    {"model": "m", "provider": "llama", "placement": ["x"]},
    {"model": "m", "provider": "llama", "placement": {"a": 1}},
    {"model": "m", "provider": "llama", "placement": 5},
    {"model": "m", "provider": "llama", "placement": ""},
    {"model": "m", "provider": "llama", "min_replicas": ["x"]},
    {"model": "m", "provider": "llama", "max_replicas": ["x"]},
    {"model": "m", "provider": "llama", "priority": ["x"]},
    {"model": "m", "provider": "llama", "min_replicas": {"a": 1}},
    {"model": "m", "provider": "llama", "size_mb": 0},
    {"model": "m", "provider": "llama", "size_mb": -5},
    {"model": "m", "provider": "llama", "size_mb": ["x"]},
    {"model": "m", "provider": "llama", "size_mb": "garbage"},
])
def test_invalid_entries_rejected(bad):
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [bad], "hosts": {}})


def test_entry_size_mb_kept_and_coerced():
    st = ap.validate_state({"enabled": True, "entries": [
        {"model": "m1", "provider": "vllm", "size_mb": 15000},
        {"model": "m2", "provider": "llama", "size_mb": "8192"}], "hosts": {}})
    assert st["entries"][0]["size_mb"] == 15000
    assert st["entries"][1]["size_mb"] == 8192


@pytest.mark.parametrize("blank", [None, ""])
def test_entry_size_mb_blank_means_absent(blank):
    st = ap.validate_state({"enabled": True, "entries": [
        {"model": "m1", "provider": "vllm", "size_mb": blank}], "hosts": {}})
    assert "size_mb" not in st["entries"][0]


def test_entry_size_mb_absent_key_stays_absent():
    st = ap.validate_state({"enabled": True, "entries": [
        {"model": "m1", "provider": "vllm"}], "hosts": {}})
    assert "size_mb" not in st["entries"][0]


def test_hosts_config_dropped():
    # Any submitted hosts shape is ignored, not validated.
    st = ap.validate_state({"enabled": True, "entries": [],
                            "hosts": {"a" * 32: {"sleep_after_idle_min": -5}}})
    assert st["hosts"] == {}
    st = ap.validate_state({"enabled": True, "entries": [], "hosts": ["junk"]})
    assert st["hosts"] == {}

def test_duplicate_model_provider_rejected():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [
            {"model": "m", "provider": "llama"},
            {"model": "m", "provider": "llama"}], "hosts": {}})

def test_non_dict_entry_rejected():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": ["m1"], "hosts": {}})

def test_non_list_entries_rejected():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": "m1", "hosts": {}})

def test_non_dict_autoscale_rejected():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [
            {"model": "m", "provider": "llama", "min_replicas": 1,
             "max_replicas": 2, "autoscale": "fast"}], "hosts": {}})


def _patch_registry(monkeypatch):
    """Fake registry store standing in for agents.json, wired the same
    way load_agents()/save_agents() read/write it."""
    store = {"agents": {}, "global": {}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: store)
    def _save(data):
        store["global"] = data.get("global", {})
        store["agents"] = data.get("agents", {})
    monkeypatch.setattr(agent_registry, "save_agents", _save)
    return store


def test_get_state_default_when_no_autopilot_key(monkeypatch):
    _patch_registry(monkeypatch)
    assert ap.get_state() == {"enabled": False, "entries": [], "hosts": {}}


def test_get_state_default_mutation_does_not_leak(monkeypatch):
    _patch_registry(monkeypatch)
    first = ap.get_state()
    first["entries"].append({"model": "phantom", "provider": "llama"})
    first["hosts"]["x"] = {"sleep_after_idle_min": 5}
    second = ap.get_state()
    assert second == {"enabled": False, "entries": [], "hosts": {}}


def test_set_state_get_state_roundtrip(monkeypatch):
    store = _patch_registry(monkeypatch)
    st = ap.validate_state({"enabled": True, "entries": [
        {"model": "m1", "provider": "llama"}], "hosts": {}})
    ap.set_state(st)
    assert store["global"]["autopilot"] == st
    assert ap.get_state() == st


# ── #500 follow-up: route sync — pins/pools converge to placements ──────

AGENT_A, AGENT_B = "a" * 32, "b" * 32


def _rs_observed(loaded_a=("m1",), loaded_b=()):
    return {"agents": {
        AGENT_A: {"provider_caps": ["llama"], "live": True,
                  "loaded": {"llama": list(loaded_a)}},
        AGENT_B: {"provider_caps": ["llama"], "live": True,
                  "loaded": {"llama": list(loaded_b)}}}}


def _rs_desired(max_replicas=1, enabled=True):
    return {"enabled": enabled, "entries": [
        {"model": "m1", "provider": "llama", "placement": AGENT_A,
         "failover": "semi", "priority": 1, "min_replicas": 1,
         "max_replicas": max_replicas}], "hosts": {}}


def test_route_sync_pins_placed_single_replica():
    writes = ap.route_sync_writes(_rs_desired(), _rs_observed(), {})
    assert writes == [("pin", "llama", "m1", AGENT_A)]


def test_route_sync_noop_when_pin_matches():
    glob = {"llama_model_pins": {"m1": AGENT_A}}
    assert ap.route_sync_writes(_rs_desired(), _rs_observed(), glob) == []


def test_route_sync_keeps_current_pin_when_still_placed():
    # m1 on both agents; existing pin to B is kept, not flapped to A.
    glob = {"llama_model_pins": {"m1": AGENT_B}}
    obs = _rs_observed(loaded_a=("m1",), loaded_b=("m1",))
    assert ap.route_sync_writes(_rs_desired(), obs, glob) == []


def test_route_sync_repins_when_pinned_agent_lost_model():
    glob = {"llama_model_pins": {"m1": AGENT_B}}
    writes = ap.route_sync_writes(_rs_desired(), _rs_observed(), glob)
    assert writes == [("pin", "llama", "m1", AGENT_A)]


def test_route_sync_pool_add_for_multi_replica():
    obs = _rs_observed(loaded_a=("m1",), loaded_b=("m1",))
    glob = {"llama_pool": [AGENT_A]}
    writes = ap.route_sync_writes(_rs_desired(max_replicas=2), obs, glob)
    assert writes == [("pool_add", "llama", AGENT_B)]


def test_route_sync_disabled_writes_nothing():
    assert ap.route_sync_writes(_rs_desired(enabled=False),
                                _rs_observed(), {}) == []


def test_route_sync_unplaced_entry_leaves_pin_alone():
    obs = _rs_observed(loaded_a=(), loaded_b=())
    glob = {"llama_model_pins": {"m1": AGENT_B}}
    assert ap.route_sync_writes(_rs_desired(), obs, glob) == []


def test_route_sync_keeps_executors_fresh_pin_after_failover():
    # Stale samples still show m1 on dead A; the executor just loaded and
    # pinned m1 on B this tick (fresh ledger entry). No revert to A.
    obs = {"agents": {
        AGENT_A: {"provider_caps": ["llama"], "live": False,
                  "loaded": {"llama": ["m1"]}},
        AGENT_B: {"provider_caps": ["llama"], "live": True,
                  "loaded": {"llama": []}}}}
    glob = {"llama_model_pins": {"m1": AGENT_B}}
    ledger = {"placed_at": {"m1/llama": {AGENT_B: 1000.0}}}
    assert ap.route_sync_writes(_rs_desired(), obs, glob, ledger, 1000.0) == []


def test_route_sync_prefers_live_placement_for_new_pin():
    # Unpinned model placed on dead A (stale sample) and live B: pin B.
    obs = {"agents": {
        AGENT_A: {"provider_caps": ["llama"], "live": False,
                  "loaded": {"llama": ["m1"]}},
        AGENT_B: {"provider_caps": ["llama"], "live": True,
                  "loaded": {"llama": ["m1"]}}}}
    writes = ap.route_sync_writes(_rs_desired(), obs, {}, None, None)
    assert writes == [("pin", "llama", "m1", AGENT_B)]


def test_route_sync_multi_replica_single_placed_pins():
    # max>1 but only one replica serving: pin it (visible + deterministic).
    obs = _rs_observed(loaded_a=("m1",), loaded_b=())
    glob = {"llama_pool": [AGENT_A, AGENT_B]}
    writes = ap.route_sync_writes(_rs_desired(max_replicas=2), obs, glob)
    assert writes == [("pin", "llama", "m1", AGENT_A)]


def test_route_sync_multi_replica_two_placed_clears_pin():
    obs = _rs_observed(loaded_a=("m1",), loaded_b=("m1",))
    glob = {"llama_pool": [AGENT_A, AGENT_B],
            "llama_model_pins": {"m1": AGENT_A}}
    writes = ap.route_sync_writes(_rs_desired(max_replicas=2), obs, glob)
    assert writes == [("pin", "llama", "m1", None)]


def test_route_sync_multi_replica_two_placed_no_pin_no_write():
    obs = _rs_observed(loaded_a=("m1",), loaded_b=("m1",))
    glob = {"llama_pool": [AGENT_A, AGENT_B]}
    assert ap.route_sync_writes(_rs_desired(max_replicas=2), obs, glob) == []
