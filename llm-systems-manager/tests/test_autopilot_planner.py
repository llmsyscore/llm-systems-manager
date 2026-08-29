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
            "backoff_until": {}, "unload_backoff": {}, "confirmed": {}}

E = {"model": "m1", "provider": "llama", "placement": "auto",
     "failover": "semi", "priority": 100, "min_replicas": 1,
     "max_replicas": 1}

def _obs(agents, sizes=None, gpu_layers=None):
    return {"agents": agents, "model_sizes_mb": sizes or {"llama:m1": 8000},
            "model_gpu_layers": gpu_layers or {}}

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

def test_dead_agent_stale_loaded_sample_does_not_count_as_placement():
    # Dead A1 with "m1 loaded" frozen in its last sample (#711).
    obs = _obs(_agents(**{A1: {"live": False, "loaded": {"llama": ["m1"]}}}))
    acts = pl.plan(_desired([E]), obs, _ledger(), now=100000.0)
    assert [a.kind for a in acts] == ["load"] and acts[0].agent_id == A2
    assert acts[0].reason.startswith("failover:")
    st = pl.entry_status(_desired([E]), obs)
    assert st["m1/llama"]["placed"] == 0

def test_stale_agent_placement_still_counts_no_failover():
    # Heartbeat lag (stale, not down) keeps the placement; no failover load.
    obs = _obs(_agents(**{A1: {"live": False, "liveness": "stale",
                               "loaded": {"llama": ["m1"]}}}))
    assert pl.plan(_desired([E]), obs, _ledger(), now=100000.0) == []
    assert pl.entry_status(_desired([E]), obs)["m1/llama"]["placed"] == 1

def test_fresh_ledger_placement_on_dead_agent_does_not_count():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 990.0}
    obs = _obs(_agents(**{A1: {"live": False}}))
    assert pl._effective_placements(E, "m1/llama", obs, led, 1000.0) == []

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

def test_hosts_sleep_config_never_plans_sleep():
    # hosts sleep config plans nothing.
    des = _desired([]); des["hosts"] = {A1: {"sleep_after_idle_min": 30}}
    obs = _obs(_agents(**{A1: {"idle_since": 1000.0}}))
    assert pl.plan(des, obs, _ledger(), now=1000.0 + 31 * 60) == []

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
    assert st["m1/llama"] == {"placed": 0, "want": 1,
                              "blocked": "model size unknown (set entry size MB)"}

def test_entry_status_insufficient_vram():
    obs = _obs(_agents(**{A1: {"vram_free_mb": 4000}, A2: {"vram_free_mb": 4000}}))
    st = pl.entry_status(_desired([E]), obs)
    row = st["m1/llama"]
    assert (row["placed"], row["want"]) == (0, 1)
    # 8000 model + 1024 headroom = 9024 needed; best candidate has 4000.
    assert row["blocked"].startswith("insufficient free VRAM/RAM")
    assert "9024" in row["blocked"] and "4000" in row["blocked"]

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
    row = st["m1/llama"]
    assert (row["placed"], row["want"]) == (1, 2)
    assert row["blocked"].startswith("insufficient free VRAM/RAM")

def test_entry_status_matches_plan_intra_pass_vram_budget():
    # One lms agent (multi-model host), 9200 MB free; two 1-replica entries
    # each need 8000+1024. Only the higher-priority entry actually fits —
    # entry_status must not disagree with plan() by calling the loser "pending".
    hi = {**E, "model": "hi", "provider": "lms", "priority": 1}
    lo = {**E, "model": "lo", "provider": "lms", "priority": 200}
    sizes = {"lms:hi": 8000, "lms:lo": 8000}
    one = {A1: {**_agents()[A1], "provider_caps": ["lms"], "vram_free_mb": 9200,
                "loaded": {"lms": []}}}
    obs = {"agents": one, "model_sizes_mb": sizes}
    st = pl.entry_status(_desired([lo, hi]), obs)
    assert st["hi/lms"] == {"placed": 0, "want": 1, "blocked": None}
    row = st["lo/lms"]
    assert (row["placed"], row["want"]) == (0, 1)
    # Best reflects the intra-pass budget: 9200 - 8000 already committed.
    assert row["blocked"].startswith("insufficient free VRAM/RAM")
    assert "1200" in row["blocked"]
    # And plan() agrees: only the winner gets a load action.
    acts = pl.plan(_desired([lo, hi]), obs, _ledger(), now=1000.0)
    assert [a.model for a in acts] == ["hi"]

# ── offload-aware fit: gpu_layers==0 -> RAM budget, not VRAM (#472/#475) ──

def test_ram_fit_places_cpu_served_model_with_zero_vram():
    # gpu_layers=0 (CPU-served): fits via ram_free_mb even though VRAM=0.
    obs = _obs(_agents(**{A1: {"vram_free_mb": 0, "ram_free_mb": 20000},
                          A2: {"vram_free_mb": 0, "ram_free_mb": 0}}),
              gpu_layers={"llama:m1": 0})
    acts = pl.plan(_desired([E]), obs, _ledger(), now=1000.0)
    assert len(acts) == 1 and acts[0].kind == "load" and acts[0].agent_id == A1

def test_ram_fit_rejects_when_no_ram_free():
    obs = _obs(_agents(**{A1: {"vram_free_mb": 0, "ram_free_mb": 4000},
                          A2: {"vram_free_mb": 0, "ram_free_mb": 4000}}),
              gpu_layers={"llama:m1": 0})
    assert pl.plan(_desired([E]), obs, _ledger(), now=1000.0) == []

def test_gpu_layers_unknown_still_uses_vram_path():
    # No model_gpu_layers entry for m1 -> unknown -> current VRAM behavior;
    # ample RAM must NOT rescue a placement when VRAM is exhausted.
    obs = _obs(_agents(**{A1: {"vram_free_mb": 0, "ram_free_mb": 20000},
                          A2: {"vram_free_mb": 0, "ram_free_mb": 20000}}))
    assert pl.plan(_desired([E]), obs, _ledger(), now=1000.0) == []

def test_gpu_layers_positive_still_uses_vram_path():
    obs = _obs(_agents(**{A1: {"vram_free_mb": 0, "ram_free_mb": 20000},
                          A2: {"vram_free_mb": 0, "ram_free_mb": 20000}}),
              gpu_layers={"llama:m1": 32})
    assert pl.plan(_desired([E]), obs, _ledger(), now=1000.0) == []

def test_ram_budget_contention_between_two_entries_same_pass():
    # One agent, 9200 MB RAM free (VRAM=0); two CPU-served entries each need
    # 8000+1024 RAM. Only the higher-priority entry fits (separate from VRAM).
    hi = {**E, "model": "hi", "priority": 1}
    lo = {**E, "model": "lo", "priority": 200}
    sizes = {"llama:hi": 8000, "llama:lo": 8000}
    layers = {"llama:hi": 0, "llama:lo": 0}
    one = {A1: {**_agents()[A1], "vram_free_mb": 0, "ram_free_mb": 9200}}
    obs = {"agents": one, "model_sizes_mb": sizes, "model_gpu_layers": layers}
    acts = pl.plan(_desired([lo, hi]), obs, _ledger(), now=1000.0)
    assert [a.model for a in acts] == ["hi"]

def test_ram_and_vram_budgets_tracked_separately_same_pass():
    # One dual-cap agent: a CPU-served llama entry (RAM) and an lms entry
    # (VRAM) both fit in the same pass — their budgets don't compete.
    ram_entry = {**E, "model": "cpu-model", "priority": 1}
    vram_entry = {**E, "model": "gpu-model", "provider": "lms", "priority": 2}
    sizes = {"llama:cpu-model": 8000, "lms:gpu-model": 8000}
    layers = {"llama:cpu-model": 0}
    one = {A1: {**_agents()[A1], "provider_caps": ["llama", "lms"],
                "vram_free_mb": 9200, "ram_free_mb": 9200,
                "loaded": {"llama": [], "lms": []}}}
    obs = {"agents": one, "model_sizes_mb": sizes, "model_gpu_layers": layers}
    acts = pl.plan(_desired([ram_entry, vram_entry]), obs, _ledger(), now=1000.0)
    assert sorted(a.model for a in acts) == ["cpu-model", "gpu-model"]

# ── entry_status: RAM-path blocked reason ───────────────────────────────

def test_entry_status_insufficient_ram():
    obs = _obs(_agents(**{A1: {"vram_free_mb": 0, "ram_free_mb": 4000},
                          A2: {"vram_free_mb": 0, "ram_free_mb": 4000}}),
              gpu_layers={"llama:m1": 0})
    st = pl.entry_status(_desired([E]), obs)
    row = st["m1/llama"]
    assert (row["placed"], row["want"]) == (0, 1)
    assert row["blocked"].startswith("insufficient free VRAM/RAM")
    assert "4000" in row["blocked"]

def test_entry_status_ram_fit_not_blocked():
    obs = _obs(_agents(**{A1: {"vram_free_mb": 0, "ram_free_mb": 20000},
                          A2: {"vram_free_mb": 0, "ram_free_mb": 0}}),
              gpu_layers={"llama:m1": 0})
    st = pl.entry_status(_desired([E]), obs)
    assert st["m1/llama"] == {"placed": 0, "want": 1, "blocked": None}


# --- #479 follow-up: per-candidate budget + numeric blocked message ---

M1 = "e" * 32

def _lms_agent(**over):
    a = {"provider_caps": ["lms"], "live": True, "vram_total_mb": 0,
         "vram_free_mb": 0, "ram_free_mb": 8000, "loaded": {"lms": []},
         "server_state": None, "idle_since": None, "saturation": {}}
    a.update(over)
    return a

def test_gpu_less_candidate_budgets_ram_not_vram():
    # LM Studio on unified memory: no GPU reported -> RAM budget, not 0-VRAM.
    e = {**E, "provider": "lms"}
    obs = _obs({M1: _lms_agent()}, sizes={"lms:m1": 100})
    st = pl.entry_status(_desired([e]), obs)
    assert st["m1/lms"]["blocked"] is None
    acts = pl.plan(_desired([e]), obs, _ledger(), now=1000.0)
    assert len(acts) == 1 and acts[0].kind == "load" and acts[0].agent_id == M1

def test_gpu_less_candidate_small_ram_still_blocks():
    e = {**E, "provider": "lms"}
    obs = _obs({M1: _lms_agent(ram_free_mb=900)}, sizes={"lms:m1": 100})
    st = pl.entry_status(_desired([e]), obs)
    assert st["m1/lms"]["blocked"] is not None
    assert pl.plan(_desired([e]), obs, _ledger(), now=1000.0) == []

def test_blocked_message_carries_need_and_best_numbers():
    # 100 MB model + 1024 headroom = 1124 needed; best candidate has 1100.
    e = {**E, "provider": "lms"}
    obs = _obs({M1: _lms_agent(vram_total_mb=8000, vram_free_mb=1100,
                               ram_free_mb=0)},
               sizes={"lms:m1": 100})
    b = pl.entry_status(_desired([e]), obs)["m1/lms"]["blocked"]
    assert b is not None and "1124" in b and "1100" in b

def test_partial_placement_unknown_size_reports_size_unknown():
    # size=None with one replica already placed must say "size unknown",
    # not fabricate "need 1024 MB" from headroom alone.
    e = {**E, "provider": "vllm", "min_replicas": 2, "max_replicas": 2}
    agents = {
        A1: {"provider_caps": ["vllm"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "ram_free_mb": 0, "loaded": {"vllm": ["m1"]},
             "server_state": None, "idle_since": None, "saturation": {}},
        A2: {"provider_caps": ["vllm"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 2000, "ram_free_mb": 0, "loaded": {"vllm": []},
             "server_state": None, "idle_since": None, "saturation": {}}}
    obs = {"agents": agents, "model_sizes_mb": {"vllm:other": 9000},
           "model_gpu_layers": {}}
    st = pl.entry_status(_desired([e]), obs)
    row = st["m1/vllm"]
    assert (row["placed"], row["want"]) == (1, 2)
    assert row["blocked"] == "model size unknown (set entry size MB)"

# ── #500: single-resident hosts + in-flight placements ──────────────────

def test_single_resident_providers_mirror_registry():
    import providers
    assert set(pl.SINGLE_RESIDENT_PROVIDERS) == {"llama", "vllm"}
    assert set(pl.SINGLE_RESIDENT_PROVIDERS) == {
        n for n, s in providers.PROVIDERS.items() if s.single_resident}


def test_single_resident_no_same_pass_co_placement():
    # Two llama entries, one roomy host: only the higher-priority entry may
    # take it — llama load displaces the resident model.
    hi = {**E, "model": "hi", "priority": 1}
    lo = {**E, "model": "lo", "priority": 200}
    sizes = {"llama:hi": 4000, "llama:lo": 4000}
    one = {A1: {**_agents()[A1]}}
    obs = {"agents": one, "model_sizes_mb": sizes}
    acts = pl.plan(_desired([lo, hi]), obs, _ledger(), now=1000.0)
    assert [a.model for a in acts] == ["hi"]

def test_single_resident_two_entries_spread_across_hosts():
    hi = {**E, "model": "hi", "priority": 1}
    lo = {**E, "model": "lo", "priority": 200}
    sizes = {"llama:hi": 4000, "llama:lo": 4000}
    obs = {"agents": _agents(), "model_sizes_mb": sizes}
    acts = pl.plan(_desired([lo, hi]), obs, _ledger(), now=1000.0)
    assert sorted(a.model for a in acts) == ["hi", "lo"]
    assert len({a.agent_id for a in acts}) == 2

def test_single_resident_blocks_placement_over_managed_model():
    # A1 already serves managed model m2 — entry m1 must not displace it.
    m1 = {**E, "model": "m1", "priority": 1}
    m2 = {**E, "model": "m2", "priority": 2}
    one = {A1: {**_agents()[A1], "loaded": {"llama": ["m2"]}}}
    obs = {"agents": one,
           "model_sizes_mb": {"llama:m1": 4000, "llama:m2": 4000}}
    acts = pl.plan(_desired([m1, m2]), obs, _ledger(), now=1000.0)
    assert acts == []
    st = pl.entry_status(_desired([m1, m2]), obs)
    assert st["m2/llama"]["blocked"] is None
    assert "another managed llama model" in st["m1/llama"]["blocked"]

def test_single_resident_can_displace_unmanaged_model():
    one = {A1: {**_agents()[A1], "loaded": {"llama": ["unmanaged"]}}}
    obs = {"agents": one, "model_sizes_mb": {"llama:m1": 4000}}
    acts = pl.plan(_desired([E]), obs, _ledger(), now=1000.0)
    assert [a.kind for a in acts] == ["load"] and acts[0].agent_id == A1

def test_residents_ignore_down_agents_frozen_samples():
    led = _ledger(); led["placed_at"]["m2/llama"] = {A2: 990.0}
    obs = _obs(_agents(**{A1: {"live": False, "loaded": {"llama": ["m1"]}},
                          A2: {"live": False, "liveness": "down"}}))
    assert pl._residents(_desired([E, {**E, "model": "m2"}]), obs, led, 1000.0) == {}
    obs["agents"][A2]["liveness"] = "stale"
    assert pl._residents(_desired([E, {**E, "model": "m2"}]), obs, led, 1000.0) == {
        ("llama", A2): "m2"}

def test_lms_multi_model_co_placement_still_allowed():
    e1 = {**E, "model": "x", "provider": "lms", "priority": 1}
    e2 = {**E, "model": "y", "provider": "lms", "priority": 2}
    one = {A1: {**_agents()[A1], "provider_caps": ["lms"],
                "loaded": {"lms": []}}}
    obs = {"agents": one, "model_sizes_mb": {"lms:x": 4000, "lms:y": 4000}}
    acts = pl.plan(_desired([e1, e2]), obs, _ledger(), now=1000.0)
    assert sorted(a.model for a in acts) == ["x", "y"]

def test_in_flight_placement_blocks_duplicate_on_second_host():
    # Load issued on A1 10s ago, model not yet visible: no duplicate on A2.
    led = _ledger()
    led["placed_at"] = {"m1/llama": {A1: 990.0}}
    led["last_action_ts"] = {A1: 990.0}
    obs = _obs(_agents())
    assert pl.plan(_desired([E]), obs, led, now=1000.0) == []

def test_stale_placed_at_does_not_count_as_placed():
    led = _ledger()
    led["placed_at"] = {"m1/llama": {A1: 1000.0 - pl.PLACEMENT_FRESH_S - 10}}
    obs = _obs(_agents())
    acts = pl.plan(_desired([E]), obs, led, now=1000.0)
    assert [a.kind for a in acts] == ["load"]

def test_entry_status_counts_in_flight_placements():
    led = _ledger()
    led["placed_at"] = {"m1/llama": {A1: 990.0}}
    obs = _obs(_agents())
    st = pl.entry_status(_desired([E]), obs, led, 1000.0)
    assert st["m1/llama"] == {"placed": 1, "want": 1, "blocked": None}

def test_in_flight_placement_reserves_single_resident_host():
    # m2 must not target A1 while m1's load is still in flight there.
    m1 = {**E, "model": "m1", "priority": 1}
    m2 = {**E, "model": "m2", "priority": 2}
    led = _ledger()
    led["placed_at"] = {"m1/llama": {A1: 990.0}}
    led["last_action_ts"] = {A1: 990.0}
    one = {A1: _agents()[A1]}
    obs = {"agents": one,
           "model_sizes_mb": {"llama:m1": 4000, "llama:m2": 4000}}
    acts = pl.plan(_desired([m1, m2]), obs, led, now=1000.0)
    assert acts == []

def test_vllm_autoscale_down_never_planned():
    e = {"model": "m1", "provider": "vllm", "placement": "auto",
         "failover": "semi", "priority": 100, "min_replicas": 1,
         "max_replicas": 2,
         "autoscale": {"target_saturation": 0.75, "up_window_s": 120,
                       "down_window_s": 900}}
    agents = {
        A1: {"provider_caps": ["vllm"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "ram_free_mb": 0,
             "loaded": {"vllm": ["m1"]}, "server_state": None,
             "saturation": {"vllm": 0.05}}}
    hist = [(0.0, 0.05), (500.0, 0.05), (1000.0, 0.05)]
    obs = {"agents": agents, "model_sizes_mb": {"vllm:m1": 8000},
           "sat_history": {"m1/vllm": hist}}
    led = _ledger()
    led["placed_at"] = {"m1/vllm": {A1: 0.0}}
    assert pl.plan(_desired([e]), obs, led, now=1000.0) == []


# ── #715: surplus replicas above max_replicas ────────────────────────────

def _both_loaded(**over):
    return _agents(**{aid: {"loaded": {"llama": ["m1"]}, **over.get(aid, {})}
                      for aid in (A1, A2)})

def test_surplus_replica_reclaimed_from_lru_copy():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 5000.0}
    acts = pl.plan(_desired([E]), _obs(_both_loaded()), led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A1)]
    assert "surplus replica (2/1)" in acts[0].reason
    assert acts[0].auto is False                      # failover: semi -> proposal

def test_surplus_reclaim_unledgered_copy_sorts_oldest():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A2: 5000.0}
    acts = pl.plan(_desired([E]), _obs(_both_loaded()), led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A1)]

def test_surplus_reclaim_keeps_the_routed_copy():
    # Empty ledger (manager restarted): the copy the route pin targets survives.
    obs = {**_obs(_both_loaded()), "route_pins": {"llama": {"m1": A1}}}
    acts = pl.plan(_desired([E]), obs, _ledger(), now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A2)]
    # The routed copy survives even when it is the LRU one (manual re-pin).
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 5000.0}
    acts = pl.plan(_desired([E]), obs, led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A2)]
    obs["route_pins"] = {"llama": {"m1": A2}}
    acts = pl.plan(_desired([E]), obs, led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A1)]

def test_surplus_reclaim_placement_pin_beats_route_pin():
    obs = {**_obs(_both_loaded()), "route_pins": {"llama": {"m1": A2}}}
    acts = pl.plan(_desired([{**E, "placement": A1}]), obs, _ledger(), now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A2)]

def test_surplus_reclaim_auto_when_entry_auto_and_enabled():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 5000.0}
    acts = pl.plan(_desired([{**E, "failover": "auto"}]), _obs(_both_loaded()),
                   led, now=100000.0)
    assert acts[0].kind == "scale_down" and acts[0].auto is True

def test_surplus_reclaim_waits_for_in_flight_load():
    # A2's copy exists only as a fresh ledger row (load still in flight).
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 99990.0}
    obs = _obs(_agents(**{A1: {"loaded": {"llama": ["m1"]}}}))
    assert pl.plan(_desired([E]), obs, led, now=100000.0) == []

def test_surplus_reclaim_waits_for_stale_replica():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 5000.0}
    obs = _obs(_both_loaded(**{A1: {"live": False, "liveness": "stale"}}))
    assert pl.plan(_desired([E]), obs, led, now=100000.0) == []

def test_surplus_reclaim_respects_cooldown_and_one_per_pass():
    A3 = "c" * 32
    ag = _both_loaded()
    ag[A3] = {**ag[A1], "loaded": {"llama": ["m1"]}}
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 200.0, A3: 300.0}
    led["last_action_ts"][A1] = 99950.0                    # A1 inside COOLDOWN_S
    acts = pl.plan(_desired([E]), _obs(ag), led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A2)]
    led["last_action_ts"] = {}
    acts = pl.plan(_desired([E]), _obs(ag), led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A1)]

def test_surplus_reclaim_never_planned_for_vllm():
    e = {**E, "provider": "vllm"}
    ag = {aid: {**a, "provider_caps": ["vllm"], "loaded": {"vllm": ["m1"]},
                "server_state": None} for aid, a in _agents().items()}
    led = _ledger(); led["placed_at"]["m1/vllm"] = {A1: 100.0, A2: 5000.0}
    obs = {"agents": ag, "model_sizes_mb": {"vllm:m1": 8000}}
    assert pl.plan(_desired([e]), obs, led, now=100000.0) == []

def test_unloadable_providers_mirror_registry():
    assert "llama" in pl.UNLOADABLE_PROVIDERS and "lms" in pl.UNLOADABLE_PROVIDERS
    assert "vllm" not in pl.UNLOADABLE_PROVIDERS

def test_surplus_above_max_on_autoscaled_entry_is_a_single_down():
    A3 = "c" * 32
    e = {**E, "max_replicas": 2,
         "autoscale": {"target_saturation": 0.75, "up_window_s": 120,
                       "down_window_s": 900}}
    ag = _both_loaded()
    ag[A3] = {**ag[A1], "loaded": {"llama": ["m1"]}}
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 200.0, A3: 300.0}
    hist = [(0.0, 0.05), (500.0, 0.05), (100000.0, 0.05)]
    obs = {**_obs(ag), "sat_history": {"m1/llama": hist}}
    acts = pl.plan(_desired([e]), obs, led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A1)]
    assert "surplus" in acts[0].reason

def test_no_reclaim_at_or_below_max_replicas():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 5000.0}
    e = {**E, "max_replicas": 2}
    assert pl.plan(_desired([e]), _obs(_both_loaded()), led, now=100000.0) == []

def test_surplus_reclaim_never_unloads_the_pinned_host():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 5000.0}
    acts = pl.plan(_desired([{**E, "placement": A1}]), _obs(_both_loaded()),
                   led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A2)]

def test_autoscale_down_never_unloads_the_pinned_host():
    e = {**E, "placement": A1, "max_replicas": 2,
         "autoscale": {"target_saturation": 0.75, "up_window_s": 120,
                       "down_window_s": 900}}
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 5000.0}
    hist = [(0.0, 0.05), (500.0, 0.05), (100000.0, 0.05)]
    obs = {**_obs(_both_loaded()), "sat_history": {"m1/llama": hist}}
    acts = pl.plan(_desired([e]), obs, led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A2)]
    assert "autoscale down" in acts[0].reason

def test_autoscale_down_waits_for_in_flight_or_stale_copies():
    e = {**E, "max_replicas": 3,
         "autoscale": {"target_saturation": 0.75, "up_window_s": 120,
                       "down_window_s": 900}}
    A3 = "c" * 32
    ag = _both_loaded(**{A1: {"live": False, "liveness": "stale"}})
    ag[A3] = {**ag[A2], "loaded": {"llama": []}}         # A3: fresh ledger row only
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 10.0, A2: 5000.0, A3: 99990.0}
    hist = [(0.0, 0.05), (500.0, 0.05), (100000.0, 0.05)]
    obs = {**_obs(ag), "sat_history": {"m1/llama": hist}}
    assert pl.plan(_desired([e]), obs, led, now=100000.0) == []
    ag[A1].update(live=True, liveness="live")
    led["placed_at"]["m1/llama"].pop(A3)
    acts = pl.plan(_desired([e]), obs, led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A1)]

def test_scale_down_skips_pair_in_unload_backoff():
    A3 = "c" * 32
    ag = _both_loaded()
    ag[A3] = {**ag[A1], "loaded": {"llama": ["m1"]}}
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 200.0, A3: 300.0}
    led["unload_backoff"] = {"m1/llama": {A1: 100300.0}}
    acts = pl.plan(_desired([E]), _obs(ag), led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A2)]

def test_scale_down_falls_through_to_next_lru_when_first_is_cooling():
    A3 = "c" * 32
    ag = _both_loaded()
    ag[A3] = {**ag[A1], "loaded": {"llama": ["m1"]}}
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 200.0, A3: 300.0}
    led["last_action_ts"][A1] = 99950.0
    acts = pl.plan(_desired([E]), _obs(ag), led, now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("scale_down", A2)]

def test_routed_copy_never_the_fallback_while_another_copy_exists():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 5000.0}
    led["unload_backoff"] = {"m1/llama": {A1: 100300.0}}
    obs = {**_obs(_both_loaded()), "route_pins": {"llama": {"m1": A2}}}
    assert pl.plan(_desired([E]), obs, led, now=100000.0) == []

def test_build_observed_route_pins_ignores_non_dict_values():
    import autopilot as ap
    deps = {"agents": lambda: {"agents": {}, "global": {"llama_model_pins": ["bad"]}},
            "liveness": lambda a: "live", "provider_snapshot": lambda p, a: {},
            "saturation": lambda p, a: {}, "model_sizes": lambda: {},
            "model_gpu_layers": lambda: {}}
    assert ap.build_observed(deps)["route_pins"]["llama"] == {}

def test_fresh_placed_skips_confirmed_pairs():
    led = _ledger(); led["placed_at"]["m1/llama"] = {A1: 990.0}
    assert pl._effective_placements(E, "m1/llama", _obs(_agents()), led, 1000.0) == [A1]
    led["confirmed"] = {"m1/llama": {A1}}
    assert pl._effective_placements(E, "m1/llama", _obs(_agents()), led, 1000.0) == []

# ── an unmanaged resident on a single-resident host is displaceable ─────────

def _one(loaded, **over):
    return {A1: {**_agents()[A1], "loaded": {"llama": loaded}, "vram_free_mb": 1024, **over}}

def test_unmanaged_resident_vram_is_credited_for_displacement():
    obs = _obs(_one(["other"]), sizes={"llama:m1": 8000, "llama:other": 20000})
    acts = pl.plan(_desired([E]), obs, _ledger(), now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("load", A1)]
    assert acts[0].reason.endswith("(displacing other)")
    assert pl.entry_status(_desired([E]), obs)["m1/llama"] == {
        "placed": 0, "want": 1, "blocked": None}

def test_unmanaged_resident_of_unknown_size_credits_used_vram():
    obs = _obs(_one(["other"], vram_free_mb=500))            # 23500 used
    acts = pl.plan(_desired([E]), obs, _ledger(), now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("load", A1)]

def test_credit_is_clamped_to_used_vram_when_the_resident_is_partly_out():
    # Resident claims 20000 MB on disk but only 3000 MB sit in VRAM.
    obs = _obs(_one(["other"], vram_free_mb=21000),
               sizes={"llama:m1": 23500, "llama:other": 20000})
    assert pl.plan(_desired([E]), obs, _ledger(), now=100000.0) == []
    st = pl.entry_status(_desired([E]), obs)["m1/llama"]
    assert st["blocked"].startswith("insufficient free VRAM/RAM") and "24000 MB free" in st["blocked"]

def test_managed_resident_gives_no_credit():
    e2 = {**E, "model": "m2"}
    obs = _obs(_one(["m1"]), sizes={"llama:m1": 8000, "llama:m2": 8000})
    residents = pl._residents(_desired([E, e2]), obs, _ledger(), 100000.0)
    managed = {("llama", "m1"), ("llama", "m2")}
    assert pl._displace_credit(e2, A1, residents, managed, obs) == 0
    assert pl._displace_credit(E, A1, residents, managed, obs) == 0     # own model
    unmanaged = {("llama", "m2")}
    assert pl._displace_credit(e2, A1, residents, unmanaged, obs) == 8000

def test_credit_never_crosses_providers_on_a_shared_gpu():
    # llama resident on the GPU; an lms entry (multi-resident) must not spend it.
    e = {**E, "provider": "lms", "model": "x"}
    ag = _one(["other"], provider_caps=["llama", "lms"])
    ag[A1]["loaded"]["lms"] = []
    obs = {"agents": ag, "model_sizes_mb": {"lms:x": 8000, "llama:other": 20000}}
    assert pl.plan(_desired([e]), obs, _ledger(), now=100000.0) == []
    # vllm entry: another single-resident provider, still not its resident.
    ev = {**E, "provider": "vllm", "model": "v"}
    ag[A1]["provider_caps"] = ["llama", "vllm"]; ag[A1]["loaded"]["vllm"] = []
    obs = {"agents": ag, "model_sizes_mb": {"vllm:v": 8000, "llama:other": 20000}}
    assert pl.plan(_desired([ev]), obs, _ledger(), now=100000.0) == []

def test_two_single_resident_providers_credit_only_their_own_resident():
    ag = _one(["other"], provider_caps=["llama", "vllm"], vram_free_mb=2000)
    ag[A1]["loaded"]["vllm"] = ["vother"]                     # both unknown size
    obs = {"agents": ag, "model_sizes_mb": {"llama:m1": 8000}}
    residents = pl._residents(_desired([E]), obs, _ledger(), 100000.0)
    assert pl._displace_credit(E, A1, residents, {("llama", "m1")}, obs) == 22000
    acts = pl.plan(_desired([E]), obs, _ledger(), now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("load", A1)]

def test_unified_memory_host_credits_the_ram_budget():
    ag = _one(["other"], vram_total_mb=0, vram_free_mb=0, ram_free_mb=1024)
    obs = _obs(ag, sizes={"llama:m1": 8000, "llama:other": 20000})
    acts = pl.plan(_desired([E]), obs, _ledger(), now=100000.0)
    assert [(a.kind, a.agent_id) for a in acts] == [("load", A1)]
    ag[A1]["loaded"]["llama"] = ["unknown-size"]
    obs = _obs(ag, sizes={"llama:m1": 8000})                  # RAM credit needs a size
    assert pl.plan(_desired([E]), obs, _ledger(), now=100000.0) == []

def test_displacement_credit_is_spent_once_per_pass():
    # Two entries want the same host; only the first can displace the resident.
    e2 = {**E, "model": "m2", "priority": 200}
    obs = _obs(_one(["other"]), sizes={"llama:m1": 8000, "llama:m2": 8000,
                                       "llama:other": 20000})
    acts = pl.plan(_desired([E, e2]), obs, _ledger(), now=100000.0)
    assert [(a.model, a.agent_id) for a in acts] == [("m1", A1)]

def test_no_credit_when_the_resident_sits_on_the_other_budget():
    # m1 is CPU-served (RAM budget); the VRAM-resident 'other' frees no RAM.
    obs = _obs(_one(["other"], ram_free_mb=1024),
               sizes={"llama:m1": 8000, "llama:other": 20000},
               gpu_layers={"llama:m1": 0})
    residents = pl._residents(_desired([E]), obs, _ledger(), 100000.0)
    assert pl._displace_credit(E, A1, residents, {("llama", "m1")}, obs) == 0
    assert pl.plan(_desired([E]), obs, _ledger(), now=100000.0) == []
