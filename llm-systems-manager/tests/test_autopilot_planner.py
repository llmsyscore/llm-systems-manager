"""#472: pure planner — placement, priority, VRAM fit, guardrails."""
from __future__ import annotations
import autopilot_planner as pl

A1, A2 = "a" * 32, "b" * 32

def _agents(**over):
    base = {
        A1: {"provider_caps": ["llama"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "loaded": {"llama": []},
             "server_state": "awake", "idle_since": None, "saturation": {}},
        A2: {"provider_caps": ["llama"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "loaded": {"llama": []},
             "server_state": "awake", "idle_since": None, "saturation": {}}}
    for k, v in over.items():
        base[k].update(v)
    return base

def _desired(entries):
    return {"enabled": True, "entries": entries, "hosts": {}}

def _ledger():
    return {"last_action_ts": {}, "placed_at": {}, "in_flight_migrations": 0,
            "backoff_until": {}}

E = {"model": "m1", "provider": "llama", "placement": "auto",
     "failover": "semi", "priority": 100, "min_replicas": 1,
     "max_replicas": 1}

def _obs(agents, sizes=None):
    return {"agents": agents, "model_sizes_mb": sizes or {"llama:m1": 8000}}

def test_places_missing_model_on_fittest_agent():
    acts = pl.plan(_desired([E]), _obs(_agents()), _ledger(), now=1000.0)
    assert len(acts) == 1
    assert acts[0].kind == "load" and acts[0].model == "m1"
    assert acts[0].auto is False              # semi entry -> proposal

def test_noop_when_already_placed():
    obs = _obs(_agents(**{A1: {"loaded": {"llama": ["m1"]}}}))
    assert pl.plan(_desired([E]), obs, _ledger(), now=1000.0) == []

def test_respects_explicit_pin_placement():
    e = {**E, "placement": A2}
    acts = pl.plan(_desired([e]), _obs(_agents()), _ledger(), now=1000.0)
    assert acts[0].agent_id == A2

def test_vram_fit_rejects_and_reports_nothing():
    obs = _obs(_agents(**{A1: {"vram_free_mb": 4000},
                          A2: {"vram_free_mb": 4000}}))
    assert pl.plan(_desired([E]), obs, _ledger(), now=1000.0) == []

def test_vram_headroom_margin_enforced():
    # 8000 model + 1024 headroom > 8500 free -> no fit
    obs = _obs(_agents(**{A1: {"vram_free_mb": 8500},
                          A2: {"vram_free_mb": 8500}}))
    assert pl.plan(_desired([E]), obs, _ledger(), now=1000.0) == []

def test_priority_orders_contention():
    hi = {**E, "model": "hi", "priority": 1}
    lo = {**E, "model": "lo", "priority": 200}
    sizes = {"llama:hi": 15000, "llama:lo": 15000}
    one = {A1: _agents()[A1]}
    acts = pl.plan(_desired([lo, hi]), {"agents": one, "model_sizes_mb": sizes},
                   _ledger(), now=1000.0)
    assert [a.model for a in acts] == ["hi"]   # only one fits; hi wins

def test_unknown_size_only_places_where_loaded():
    obs = _obs(_agents(**{A2: {"loaded": {"llama": ["m1"]}}}), sizes={})
    # loaded on A2 -> desired met; and never proposes a load on A1
    assert pl.plan(_desired([E]), obs, _ledger(), now=1000.0) == []

def test_never_unloads_unmanaged_models():
    obs = _obs(_agents(**{A1: {"loaded": {"llama": ["m1", "stranger"]}}}))
    acts = pl.plan(_desired([E]), obs, _ledger(), now=1000.0)
    assert all(a.kind != "unload" for a in acts)

def test_disabled_returns_plan_but_all_semi():
    # kill switch off: planner still plans (dry-run view) but nothing auto
    e = {**E, "failover": "auto"}
    acts = pl.plan({**_desired([e]), "enabled": False}, _obs(_agents()),
                   _ledger(), now=1000.0)
    assert acts and all(a.auto is False for a in acts)

def test_auto_flag_carried_when_enabled():
    e = {**E, "failover": "auto"}
    acts = pl.plan(_desired([e]), _obs(_agents()), _ledger(), now=1000.0)
    assert acts[0].auto is True
