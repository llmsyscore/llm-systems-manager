"""#966: Autopilot derives llama server_state from residency and refuses stale/unknown samples."""
from __future__ import annotations

import time

import autopilot as ap


def _deps(sample, age=0.0, caps=None):
    agents = {"agents": {"a1": {"agent_id": "a1", "capabilities": caps or {"llama": True}}}}
    return {
        "agents": lambda: agents,
        "provider_snapshot": lambda prov, aid: {"sample": sample, "last_seen": time.time() - age},
        "saturation": lambda prov, aid: {},
        "liveness": lambda agent: "live",
        "model_sizes": lambda: {},
        "model_gpu_layers": lambda: {},
    }


def test_sleeping_aggregate_yields_sleeping_server_state():
    obs = ap.build_observed(_deps({"llama": {"state": "awake", "model": "m",
                                             "residency": {"aggregate": "sleeping"}}}))
    a = obs["agents"]["a1"]
    assert a["server_state"] == "sleeping" and a["answered"]["llama"] is True and a["stale"] is False


def test_loading_and_idle_are_awake_for_placement():
    for agg in ("loading", "idle", "active"):
        obs = ap.build_observed(_deps({"llama": {"state": "awake", "model": "m", "residency": {"aggregate": agg}}}))
        assert obs["agents"]["a1"]["server_state"] == "awake", agg


def test_unknown_aggregate_or_stale_sample_is_not_answered():
    obs = ap.build_observed(_deps({"llama": {"state": "awake", "model": "m", "residency": {"aggregate": "unknown"}}}))
    assert obs["agents"]["a1"]["answered"]["llama"] is False and obs["agents"]["a1"]["server_state"] is None
    obs = ap.build_observed(_deps({"llama": {"state": "awake", "model": "m",
                                             "residency": {"aggregate": "active"}}}, age=20))
    assert obs["agents"]["a1"]["stale"] is True and obs["agents"]["a1"]["answered"]["llama"] is False


def test_legacy_sample_without_residency_keeps_old_behaviour():
    obs = ap.build_observed(_deps({"llama": {"state": "sleeping", "model": "m (sleeping)"}}))
    assert obs["agents"]["a1"]["server_state"] == "sleeping" and obs["agents"]["a1"]["answered"]["llama"] is True
    assert obs["agents"]["a1"]["loaded"]["llama"] == ["m"]


def test_loaded_list_excludes_unloaded_models_from_residency():
    res = {"aggregate": "active", "models": [
        {"provider": "llama", "model_id": "a", "status": "loaded"},
        {"provider": "llama", "model_id": "b", "status": "unloaded"},
        {"provider": "llama", "model_id": "c", "status": "sleeping"}]}
    obs = ap.build_observed(_deps({"llama": {"state": "awake", "model": "a", "residency": res}}))
    assert sorted(obs["agents"]["a1"]["loaded"]["llama"]) == ["a", "c"]


def test_other_providers_stale_sample_does_not_gate_llama_answered():
    """Only llama's own snapshot age gates answered["llama"]; "stale" stays agent-wide."""
    now = time.time()

    def snapshot(prov, aid):
        if prov == "llama":
            return {"sample": {"llama": {"state": "awake", "model": "m",
                                          "residency": {"aggregate": "active"}}}, "last_seen": now}
        return {"sample": {"ps": [], "ps_ok": True}, "last_seen": now - 40}

    deps = {
        "agents": lambda: {"agents": {"a1": {"agent_id": "a1",
                                             "capabilities": {"llama": True, "lms": True}}}},
        "provider_snapshot": snapshot,
        "saturation": lambda prov, aid: {},
        "liveness": lambda agent: "live",
        "model_sizes": lambda: {},
        "model_gpu_layers": lambda: {},
    }
    a = ap.build_observed(deps)["agents"]["a1"]
    assert a["answered"]["llama"] is True
    assert a["stale"] is True
