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

def test_pin_placement_enforces_provider_capability():
    # Pin llama entry to A2 which only has vllm capability
    e = {**E, "placement": A2}
    obs = _obs(_agents(**{A2: {"provider_caps": ["vllm"], "vram_free_mb": 20000}}))
    assert pl.plan(_desired([e]), obs, _ledger(), now=1000.0) == []

def test_cooldown_suppresses_agent_actions():
    led = _ledger(); led["last_action_ts"][A1] = 950.0     # 50s ago < 120s
    led2 = _ledger()
    obs = _obs(_agents(**{A2: {"live": False}}))
    acts = pl.plan(_desired([E]), obs, led, now=1000.0)
    assert acts == []
    assert pl.plan(_desired([E]), obs, led2, now=1000.0)   # control

def test_dwell_blocks_replan_of_recent_placement():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 900.0}
    obs = _obs(_agents(**{A1: {"live": False}}))            # A1 just died
    # within DWELL_S of placement -> no migration yet
    assert pl.plan(_desired([E]), obs, led, now=1000.0) == []

def test_failover_migrates_after_dwell():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0}
    obs = _obs(_agents(**{A1: {"live": False}}))
    acts = pl.plan(_desired([E]), obs, led, now=100000.0)
    assert [a.kind for a in acts] == ["load"] and acts[0].agent_id == A2

def test_one_migration_in_flight_globally():
    led = _ledger(); led["in_flight_migrations"] = 1
    obs = _obs(_agents(**{A1: {"live": False}}))
    assert pl.plan(_desired([E]), obs, led, now=100000.0) == []

def test_only_one_failover_load_per_plan_pass():
    # Two entries each have a dead recorded placement; only ONE may migrate
    # per pass even though a third agent has room and VRAM for both.
    A3 = "c" * 32
    e1, e2 = {**E, "model": "m1"}, {**E, "model": "m2"}
    led = _ledger()
    led["placed_at"]["m1/llama"] = {A1: 100.0}
    led["placed_at"]["m2/llama"] = {A2: 100.0}
    agents = _agents(**{A1: {"live": False}, A2: {"live": False}})
    agents[A3] = {"provider_caps": ["llama"], "live": True, "vram_total_mb": 48000,
                  "vram_free_mb": 48000, "loaded": {"llama": []},
                  "server_state": "awake", "idle_since": None, "saturation": {}}
    obs = {"agents": agents, "model_sizes_mb": {"llama:m1": 8000, "llama:m2": 8000}}
    acts = pl.plan(_desired([e1, e2]), obs, led, now=100000.0)
    failovers = [a for a in acts if a.reason.startswith("failover:")]
    assert len(failovers) == 1

def test_no_fail_back_when_original_agent_returns():
    # m1 now on A2 (after failover); A1 back up. Desired met -> no action.
    obs = _obs(_agents(**{A2: {"loaded": {"llama": ["m1"]}}}))
    assert pl.plan(_desired([E]), obs, _ledger(), now=100000.0) == []

def test_wake_ordered_before_load_on_sleeping_host():
    obs = _obs(_agents(**{A1: {"live": False},
                          A2: {"server_state": "sleeping"}}))
    acts = pl.plan(_desired([E]), obs, _ledger(), now=100000.0)
    assert [a.kind for a in acts] == ["wake", "load"]
    assert acts[0].agent_id == acts[1].agent_id == A2

def test_idle_host_gets_sleep_proposal():
    des = _desired([]); des["hosts"] = {A1: {"sleep_after_idle_min": 30}}
    obs = _obs(_agents(**{A1: {"idle_since": 1000.0}}))
    acts = pl.plan(des, obs, _ledger(), now=1000.0 + 31 * 60)
    assert [a.kind for a in acts] == ["sleep"] and acts[0].agent_id == A1

def test_idle_below_threshold_no_sleep():
    des = _desired([]); des["hosts"] = {A1: {"sleep_after_idle_min": 30}}
    obs = _obs(_agents(**{A1: {"idle_since": 1000.0}}))
    assert pl.plan(des, obs, _ledger(), now=1000.0 + 29 * 60) == []

def test_sleep_never_planned_for_lms_host():
    des = _desired([]); des["hosts"] = {A1: {"sleep_after_idle_min": 30}}
    ag = _agents(**{A1: {"provider_caps": ["lms"], "idle_since": 1000.0}})
    assert pl.plan(des, {"agents": ag, "model_sizes_mb": {}}, _ledger(),
                   now=1000.0 + 31 * 60) == []

def test_dead_agent_stale_sleeping_state_not_placeable():
    # live=False blocks placement even with ample VRAM and a cached "sleeping" sample.
    one = _agents()
    one[A1].update({"live": False, "server_state": "sleeping"})
    obs = {"agents": {A1: one[A1]}, "model_sizes_mb": {"llama:m1": 8000}}
    assert pl.plan(_desired([E]), obs, _ledger(), now=1000.0) == []

def test_wake_not_emitted_for_non_llama_entry_on_sleeping_host():
    e = {**E, "provider": "vllm"}
    obs = _obs(_agents(**{A1: {"provider_caps": ["vllm"],
                               "server_state": "sleeping"}}),
               sizes={"vllm:m1": 8000})
    acts = pl.plan(_desired([e]), obs, _ledger(), now=1000.0)
    assert [a.kind for a in acts] == ["load"]
    assert acts[0].agent_id == A1

# ── entry_status (#472 honest plan messages) ──────────────────────────

def test_entry_status_satisfied():
    obs = _obs(_agents(**{A1: {"loaded": {"llama": ["m1"]}}}))
    st = pl.entry_status(_desired([E]), obs)
    assert st == {"m1/llama": {"placed": 1, "want": 1, "blocked": None}}

def test_entry_status_no_live_agent():
    obs = _obs(_agents(**{A1: {"live": False}, A2: {"live": False}}))
    st = pl.entry_status(_desired([E]), obs)
    assert st["m1/llama"] == {"placed": 0, "want": 1,
                              "blocked": "no live agent supports this provider"}

def test_entry_status_pin_lacking_capability():
    # explicit-pin variant: pinned agent lacks the capability -> same reason
    e = {**E, "placement": A2}
    obs = _obs(_agents(**{A2: {"provider_caps": ["vllm"], "vram_free_mb": 20000}}))
    st = pl.entry_status(_desired([e]), obs)
    assert st["m1/llama"] == {"placed": 0, "want": 1,
                              "blocked": "no live agent supports this provider"}

def test_entry_status_model_size_unknown():
    # sizes={} is falsy in _obs()'s "sizes or {default}" fallback, so use a
    # non-empty dict that simply omits this entry's key.
    obs = _obs(_agents(), sizes={"llama:other": 9000})
    st = pl.entry_status(_desired([E]), obs)
    assert st["m1/llama"] == {"placed": 0, "want": 1, "blocked": "model size unknown"}

def test_entry_status_insufficient_vram():
    obs = _obs(_agents(**{A1: {"vram_free_mb": 4000}, A2: {"vram_free_mb": 4000}}))
    st = pl.entry_status(_desired([E]), obs)
    assert st["m1/llama"] == {"placed": 0, "want": 1,
                              "blocked": "insufficient free VRAM on any candidate"}

def test_entry_status_pending_not_blocked():
    # fits somewhere but hasn't been placed yet (e.g. cooldown) -> not "blocked"
    obs = _obs(_agents())
    st = pl.entry_status(_desired([E]), obs)
    assert st["m1/llama"] == {"placed": 0, "want": 1, "blocked": None}

def test_entry_status_already_placed_agent_not_its_own_candidate():
    # want=2: A1 already hosts m1 (ample free VRAM there is irrelevant — it
    # can't host a second replica of itself); A2 is the only real candidate
    # and lacks VRAM -> genuinely stuck, not falsely "pending".
    e = {**E, "min_replicas": 2, "max_replicas": 2}
    obs = _obs(_agents(**{A1: {"loaded": {"llama": ["m1"]}},
                          A2: {"vram_free_mb": 100}}))
    st = pl.entry_status(_desired([e]), obs)
    assert st["m1/llama"] == {"placed": 1, "want": 2,
                              "blocked": "insufficient free VRAM on any candidate"}
