"""#472: tick — auto executes, semi proposes, failure backoff, stale expiry."""
from __future__ import annotations
import copy
import threading
import autopilot as ap
import autopilot_planner as pl

A1, A2 = "a" * 32, "b" * 32

def _mk(auto=False, exec_ok=True, loaded=(False,), route_sync=None, placed_at=None,
        route_pins=None, fresh=False):
    """fresh=True hands the reconciler a deep copy per observation, like prod."""
    calls = []
    state = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "auto" if auto else "semi", "priority": 1,
         "min_replicas": 1, "max_replicas": 1}]}
    observed = {"agents": {
        aid: {"provider_caps": ["llama"], "live": True, "vram_total_mb": 24000,
              "vram_free_mb": 20000, "loaded": {"llama": ["m1"] if has else []},
              "server_state": "awake", "idle_since": None, "saturation": {}}
        for aid, has in zip((A1, A2), loaded)},
        "model_sizes_mb": {"llama:m1": 8000}, "sat_history": {},
        "route_pins": route_pins or {}}
    r = ap.Reconciler(get_state=lambda: state,
                      build_observed=(lambda: copy.deepcopy(observed)) if fresh
                      else (lambda: observed),
                      executor=lambda a: (calls.append(a), exec_ok)[1],
                      route_sync=route_sync)
    if placed_at:
        r.ledger["placed_at"]["m1/llama"] = dict(placed_at)
    return r, calls, observed

def test_semi_action_becomes_proposal_not_executed():
    r, calls, _ = _mk(auto=False)
    r.tick(now=1000.0)
    assert calls == [] and len(r.proposals()) == 1
    assert r.proposals()[0]["action"]["kind"] == "load"

def test_auto_action_executes_and_updates_ledger():
    r, calls, _ = _mk(auto=True)
    r.tick(now=1000.0)
    assert len(calls) == 1
    assert r.ledger["last_action_ts"][A1] == 1000.0
    assert A1 in r.ledger["placed_at"]["m1/llama"]

def test_apply_executes_proposal():
    r, calls, _ = _mk(auto=False)
    r.tick(now=1000.0)
    r.apply(r.proposals()[0]["id"])
    assert len(calls) == 1

def test_executor_failure_sets_backoff():
    r, _, _ = _mk(auto=True, exec_ok=False)
    r.tick(now=1000.0)
    assert r.ledger["backoff_until"]["m1/llama"] == 1300.0

def test_stale_proposals_dropped_when_state_satisfied():
    r, _, obs = _mk(auto=False)
    r.tick(now=1000.0)
    assert r.proposals()
    obs["agents"][A1]["loaded"]["llama"] = ["m1"]     # now satisfied
    r.tick(now=1040.0)
    assert r.proposals() == []

def test_duplicate_proposals_not_stacked():
    r, _, _ = _mk(auto=False)
    r.tick(now=1000.0); r.tick(now=1030.0)
    assert len(r.proposals()) == 1


def test_proposals_read_does_not_block_on_held_lock():
    """#472 Task 7 review: proposals() must read the lock-free snapshot,
    not self._lock, so GET can't stall behind a long-running tick/apply."""
    r, _, _ = _mk(auto=False)
    r.tick(now=1000.0)
    expected = r.proposals()
    r._lock.acquire()
    try:
        done = threading.Event()
        result = {}

        def _read():
            result["proposals"] = r.proposals()
            done.set()

        t = threading.Thread(target=_read)
        t.start()
        finished = done.wait(timeout=2.0)
        t.join(timeout=2.0)
    finally:
        r._lock.release()
    assert finished, "proposals() blocked while the main lock was held"
    assert result["proposals"] == expected


# --- Supplementary: ledger maintenance (#472 Task 4 review deferral) ---
# Covers placed_at pruning, sat_history ring, and in_flight_migrations.

def test_placed_at_survives_within_cooldown_grace_window():
    r, _, _ = _mk(auto=False)
    r.ledger["placed_at"]["m1/llama"] = {A1: 1000.0}
    r.tick(now=1030.0)                                  # +30s, model still not visible
    assert r.ledger["placed_at"]["m1/llama"][A1] == 1000.0

def test_placed_at_pruned_after_grace_window_on_live_agent():
    r, _, _ = _mk(auto=False)
    r.ledger["placed_at"]["m1/llama"] = {A1: 1000.0}
    r.tick(now=1000.0 + pl.PLACEMENT_FRESH_S + 1)
    assert A1 not in r.ledger["placed_at"].get("m1/llama", {})

def test_placed_at_not_pruned_for_dead_agent():
    r, _, obs = _mk(auto=False)
    obs["agents"][A1]["live"] = False
    r.ledger["placed_at"]["m1/llama"] = {A1: 500.0}
    r.tick(now=1000.0)
    assert r.ledger["placed_at"]["m1/llama"][A1] == 500.0

def test_placed_at_pruned_for_unregistered_agent_after_grace():
    r, _, obs = _mk(auto=False)
    gone = "z" * 32
    r.ledger["placed_at"]["m1/llama"] = {gone: 500.0, A1: 990.0}
    r.tick(now=1000.0)
    assert gone not in r.ledger["placed_at"]["m1/llama"]
    assert r.ledger["placed_at"]["m1/llama"][A1] == 990.0   # within grace

def test_sat_history_resets_when_no_live_placement():
    r, _, obs = _mk(auto=False)
    obs["agents"][A1]["loaded"] = {"llama": ["m1"]}
    obs["agents"][A1]["saturation"] = {"llama": 0.9}
    r.tick(now=1000.0)
    assert r._sat_history["m1/llama"] == [(1000.0, 0.9)]
    obs["agents"][A1]["live"] = False
    r.tick(now=1030.0)
    assert r._sat_history["m1/llama"] == []

def test_sat_history_ring_accumulates_and_trims_to_20_minutes():
    state = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "semi", "priority": 1,
         "min_replicas": 1, "max_replicas": 1}]}
    observed = {"agents": {
        A1: {"provider_caps": ["llama"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "loaded": {"llama": ["m1"]},
             "server_state": "awake", "idle_since": None,
             "saturation": {"llama": 0.9}}},
        "model_sizes_mb": {"llama:m1": 8000}, "sat_history": {}}
    r = ap.Reconciler(get_state=lambda: state, build_observed=lambda: observed,
                      executor=lambda a: True)
    r.tick(now=1000.0)
    r.tick(now=1100.0)
    assert r._sat_history["m1/llama"] == [(1000.0, 0.9), (1100.0, 0.9)]
    r.tick(now=2250.0)                     # 1000.0 is now > 20min stale
    assert [t for t, _ in r._sat_history["m1/llama"]] == [1100.0, 2250.0]

def test_sat_history_ignores_dead_agents_stale_saturation():
    # Dead A1 carries a frozen 0.95 saturation (#711).
    state = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "semi", "priority": 1,
         "min_replicas": 1, "max_replicas": 2}]}
    A2 = "b" * 32
    observed = {"agents": {
        A1: {"provider_caps": ["llama"], "live": False, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "loaded": {"llama": ["m1"]},
             "server_state": "awake", "idle_since": None,
             "saturation": {"llama": 0.95}},
        A2: {"provider_caps": ["llama"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "loaded": {"llama": ["m1"]},
             "server_state": "awake", "idle_since": None,
             "saturation": {"llama": 0.2}}},
        "model_sizes_mb": {"llama:m1": 8000}, "sat_history": {}}
    r = ap.Reconciler(get_state=lambda: state, build_observed=lambda: observed,
                      executor=lambda a: True)
    r.tick(now=1000.0)
    assert r._sat_history["m1/llama"] == [(1000.0, 0.2)]

def test_in_flight_migrations_tracked_during_failover_execution():
    seen = {}
    state = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "auto", "priority": 1,
         "min_replicas": 1, "max_replicas": 1}]}
    observed = {"agents": {
        A1: {"provider_caps": ["llama"], "live": False, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "loaded": {"llama": []},
             "server_state": "awake", "idle_since": None, "saturation": {}},
        A2: {"provider_caps": ["llama"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "loaded": {"llama": []},
             "server_state": "awake", "idle_since": None, "saturation": {}}},
        "model_sizes_mb": {"llama:m1": 8000}, "sat_history": {}}

    def executor(action):
        seen["in_flight"] = r.ledger["in_flight_migrations"]
        seen["reason"] = action.reason
        return True

    r = ap.Reconciler(get_state=lambda: state, build_observed=lambda: observed,
                      executor=executor)
    r.tick(now=1000.0)
    assert seen["reason"].startswith("failover:")
    assert seen["in_flight"] == 1
    assert r.ledger["in_flight_migrations"] == 0

def test_tick_invokes_route_sync_with_state_and_ledger():
    seen = []
    state = {"enabled": True, "hosts": {}, "entries": []}
    observed = {"agents": {}, "model_sizes_mb": {}, "sat_history": {}}
    r = ap.Reconciler(get_state=lambda: state,
                      build_observed=lambda: observed,
                      executor=lambda a: True,
                      route_sync=lambda d, o, led, now: seen.append((d, led, now)))
    r.tick(now=1000.0)
    assert seen and seen[0][0] is state
    assert seen[0][1] is r.ledger and seen[0][2] == 1000.0

def test_tick_survives_route_sync_failure():
    def _boom(d, o, led, now):
        raise RuntimeError("sync failed")
    r = ap.Reconciler(get_state=lambda: {"enabled": True, "hosts": {},
                                         "entries": []},
                      build_observed=lambda: {"agents": {},
                                              "model_sizes_mb": {},
                                              "sat_history": {}},
                      executor=lambda a: True, route_sync=_boom)
    out = r.tick(now=1000.0)
    assert out["actions"] == []

# ── #715: surplus scale_down in tick ───────────────────────────────────────

def test_auto_surplus_scale_down_hides_unloaded_copy_from_route_sync():
    seen = []
    r, calls, observed = _mk(
        auto=True, loaded=(True, True), placed_at={A1: 100.0, A2: 500.0},
        route_sync=lambda d, o, led, now: seen.append(
            pl._effective_placements(d["entries"][0], "m1/llama", o, led, now)))
    r.tick(now=1000.0)
    assert [a.kind for a in calls] == ["scale_down"] and calls[0].agent_id == A1
    assert seen == [[A2]]
    assert observed["agents"][A1]["loaded"]["llama"] == []
    assert A1 not in r.ledger["placed_at"]["m1/llama"]

def test_failed_surplus_scale_down_backs_off_that_pair_only():
    r, calls, _ = _mk(auto=True, exec_ok=False, loaded=(True, True),
                      placed_at={A1: 100.0, A2: 500.0},
                      route_pins={"llama": {"m1": A2}})
    r.tick(now=1000.0)
    assert [a.kind for a in calls] == ["scale_down"] and calls[0].agent_id == A1
    assert r.ledger["unload_backoff"]["m1/llama"] == {A1: 1300.0}
    assert "m1/llama" not in r.ledger["backoff_until"]
    assert A1 not in r.ledger["last_action_ts"]
    # Inside the window A1 is skipped and the routed copy A2 is never the fallback.
    calls.clear()
    r.tick(now=1030.0)
    assert calls == []
    r.tick(now=1400.0)
    assert [a.agent_id for a in calls] == [A1]

def test_unload_backoff_pruned_with_entry_and_agent():
    r, _, observed = _mk(auto=True, loaded=(True, True))
    r.ledger["unload_backoff"] = {"m1/llama": {A1: 5000.0, "z" * 32: 5000.0},
                                  "gone/llama": {A1: 5000.0}}
    r.tick(now=1000.0)
    assert r.ledger["unload_backoff"] == {"m1/llama": {A1: 5000.0}}

# ── the in-flight credit ends once the agent confirms the load ─────────────

def test_confirmed_placement_loses_fresh_credit_after_observed_unload():
    r, calls, observed = _mk(auto=True)
    r.tick(now=1000.0)                                   # load issued
    assert [a.kind for a in calls] == ["load"]
    observed["agents"][A1]["loaded"]["llama"] = ["m1"]   # agent confirms it
    out = r.tick(now=1030.0)
    assert A1 in r.ledger["confirmed"]["m1/llama"]
    assert out["entry_status"]["m1/llama"]["placed"] == 1
    observed["agents"][A1]["loaded"]["llama"] = []       # operator unloads it
    out = r.tick(now=1060.0)
    assert out["entry_status"]["m1/llama"]["placed"] == 0  # credit gone at once
    assert [a.kind for a in calls] == ["load"]           # A1 still in COOLDOWN_S
    out = r.tick(now=1000.0 + pl.COOLDOWN_S + 10)
    assert [a.kind for a in calls] == ["load", "load"]   # re-placed after cooldown
    assert A1 not in r.ledger["confirmed"].get("m1/llama", ())
    assert r.ledger["placed_at"]["m1/llama"][A1] == 1000.0 + pl.COOLDOWN_S + 10
    assert out["entry_status"]["m1/llama"]["placed"] == 1  # new load in flight

def test_unconfirmed_placement_keeps_fresh_credit_until_window_ends():
    r, calls, _ = _mk(auto=True)
    r.tick(now=1000.0)
    out = r.tick(now=1030.0)                             # still loading, not visible
    assert [a.kind for a in calls] == ["load"]
    assert out["entry_status"]["m1/llama"]["placed"] == 1
    assert A1 not in r.ledger["confirmed"].get("m1/llama", ())

def test_failed_wake_does_not_back_off_the_entry():
    r, calls, observed = _mk(auto=True)
    observed["agents"][A1]["server_state"] = "sleeping"
    r._exec = lambda a: (calls.append(a), a.kind != "wake")[1]
    r.tick(now=1000.0)
    assert [a.kind for a in calls] == ["wake", "load"]
    assert "m1/llama" not in r.ledger["backoff_until"]
    assert r.ledger["placed_at"]["m1/llama"][A1] == 1000.0

def test_confirmed_pruned_with_placed_at_and_entry():
    r, _, observed = _mk(auto=False)
    r.ledger["placed_at"]["m1/llama"] = {A1: 100.0}
    r.ledger["confirmed"] = {"m1/llama": {A1, "z" * 32}, "gone/llama": {A1}}
    r.tick(now=1000.0)                                   # A1 live, m1 not loaded
    assert r.ledger["confirmed"] == {}
    assert r.ledger_view["confirmed"] == {}

def test_applied_unload_clears_confirmation_without_a_tick():
    r, calls, observed = _mk(auto=False, loaded=(True, True),
                             placed_at={A1: 100.0, A2: 500.0})
    r.tick(now=1000.0)                                   # surplus -> proposal
    assert r.ledger["confirmed"]["m1/llama"] == {A1, A2}
    pid = r.proposals()[0]["id"]
    assert r.apply(pid, now=1001.0)["ok"] is True
    assert calls[-1].kind == "scale_down" and calls[-1].agent_id == A1
    assert A1 not in r.ledger["placed_at"]["m1/llama"]
    assert r.ledger["confirmed"]["m1/llama"] == {A2}
    assert r.ledger_view["confirmed"]["m1/llama"] == {A2}

# ── #729: a blank provider sample is debounced before a confirmed placement drops ──

def _confirmed(fresh=True, **kw):
    """Reconciler with m1 confirmed on A1 at t=1030 and A1 out of cooldown by t=1200."""
    r, calls, observed = _mk(auto=True, fresh=fresh, **kw)
    r.tick(now=1000.0)                                   # load issued
    observed["agents"][A1]["loaded"]["llama"] = ["m1"]
    r.tick(now=1030.0)                                   # confirmed
    assert r.ledger["confirmed"]["m1/llama"] == {A1}
    return r, calls, observed

def _blank_llama(observed, aid=A1):
    observed["agents"][aid]["loaded"]["llama"] = []
    observed["agents"][aid]["server_state"] = None

def test_blank_llama_sample_keeps_confirmed_placement_within_grace():
    r, calls, observed = _confirmed()
    _blank_llama(observed)
    out = r.tick(now=1200.0)                             # cooldown over, sample blank
    assert out["entry_status"]["m1/llama"]["placed"] == 1
    assert [a.kind for a in calls] == ["load"]           # no re-load
    assert r.ledger["confirmed"]["m1/llama"] == {A1}
    assert r.ledger["blank_since"]["m1/llama"][A1] == 1200.0
    assert r.ledger_view["blank_since"]["m1/llama"][A1] == 1200.0
    observed["agents"][A1]["loaded"]["llama"] = ["m1"]   # sample recovers
    observed["agents"][A1]["server_state"] = "awake"
    r.tick(now=1230.0)
    assert r.ledger["blank_since"] == {}
    assert r.ledger["confirmed"]["m1/llama"] == {A1}

def test_blank_samples_past_grace_drop_the_placement_and_reload():
    r, calls, observed = _confirmed()
    _blank_llama(observed)
    r.tick(now=1200.0)
    out = r.tick(now=1200.0 + ap.BLANK_GRACE_S)
    assert out["entry_status"]["m1/llama"]["placed"] == 1  # new load in flight
    assert [a.kind for a in calls] == ["load", "load"]
    assert r.ledger["placed_at"]["m1/llama"][A1] == 1200.0 + ap.BLANK_GRACE_S
    assert r.ledger["blank_since"] == {}

def test_grace_does_not_reset_while_the_sample_stays_blank():
    """An expired grace is not re-armed on later ticks while the placed_at
    row is still fresh enough to keep the pair confirmed."""
    r, calls, observed = _confirmed()
    r._exec = lambda a: (calls.append(a), False)[1]      # re-load attempts fail
    _blank_llama(observed)
    r.tick(now=1040.0)                                   # since=1040
    out = r.tick(now=1040.0 + ap.BLANK_GRACE_S)          # expired, row still < FRESH
    assert out["entry_status"]["m1/llama"]["placed"] == 0  # no in-flight credit either
    assert [a.kind for a in calls] == ["load", "load"]   # re-load tried, failed
    assert r.ledger["confirmed"]["m1/llama"] == {A1}
    assert r.ledger["blank_since"]["m1/llama"][A1] == 1040.0
    out = r.tick(now=1040.0 + ap.BLANK_GRACE_S + 30)     # entry in backoff
    assert r.ledger["blank_since"]["m1/llama"][A1] == 1040.0   # not re-armed
    assert out["entry_status"]["m1/llama"]["placed"] == 0
    r.tick(now=1000.0 + pl.PLACEMENT_FRESH_S)            # placed_at row expires
    assert r.ledger["confirmed"] == {} and r.ledger["blank_since"] == {}

def test_awake_server_with_no_model_is_a_real_unload_not_a_blip():
    r, calls, observed = _confirmed()
    observed["agents"][A1]["loaded"]["llama"] = []       # server_state stays awake
    out = r.tick(now=1200.0)
    assert out["entry_status"]["m1/llama"]["placed"] == 1  # reloaded at once
    assert [a.kind for a in calls] == ["load", "load"]
    assert r.ledger["blank_since"] == {}

def test_another_model_on_the_host_drops_the_placement_at_once():
    r, calls, observed = _confirmed()
    observed["agents"][A1]["loaded"]["llama"] = ["other"]
    observed["agents"][A1]["server_state"] = None
    r.tick(now=1200.0)
    assert [a.kind for a in calls] == ["load", "load"]   # re-placed at once
    assert r.ledger["blank_since"] == {}
    assert A1 not in r.ledger["confirmed"].get("m1/llama", ())

def test_grace_keeps_the_resident_slot_against_other_entries():
    """During the grace another managed entry cannot take the single-resident host."""
    r, calls, observed = _confirmed()
    r._get_state().setdefault("entries").append(
        {"model": "m2", "provider": "llama", "placement": "auto",
         "failover": "auto", "priority": 2, "min_replicas": 1, "max_replicas": 1})
    observed["model_sizes_mb"]["llama:m2"] = 8000
    _blank_llama(observed)
    out = r.tick(now=1200.0)
    assert [(a.kind, a.model) for a in calls] == [("load", "m1")]
    assert out["entry_status"]["m2/llama"]["blocked"].startswith(
        "capable hosts already serve another managed llama model")

def test_observe_applies_grace_without_touching_the_ledger():
    r, calls, observed = _confirmed()
    _blank_llama(observed)
    r.tick(now=1200.0)
    obs = r.observe(now=1230.0)
    assert obs["agents"][A1]["loaded"]["llama"] == ["m1"]
    assert pl.entry_status(r._get_state(), obs, r.ledger_view, 1230.0)["m1/llama"]["placed"] == 1
    assert r.ledger["blank_since"]["m1/llama"][A1] == 1200.0
    obs = r.observe(now=1200.0 + ap.BLANK_GRACE_S)
    assert obs["agents"][A1]["loaded"]["llama"] == []
    r.ledger["blank_since"].clear(); r.ledger_view["blank_since"].clear()
    obs = r.observe(now=1230.0)                          # GET never records a new blip
    assert obs["agents"][A1]["loaded"]["llama"] == []
    assert r.ledger["blank_since"] == {}

def test_blank_lms_sample_is_debounced_but_a_partial_ps_is_not():
    r, calls, observed = _mk(auto=True, fresh=True)
    e = r._get_state()["entries"][0]
    e["provider"] = "lms"
    for a in observed["agents"].values():
        a["provider_caps"] = ["lms"]; a["loaded"] = {"lms": []}; a["server_state"] = None
    observed["model_sizes_mb"]["lms:m1"] = 8000
    r.tick(now=1000.0)
    observed["agents"][A1]["loaded"]["lms"] = ["m1", "x"]
    r.tick(now=1030.0)
    assert r.ledger["confirmed"]["m1/lms"] == {A1}
    observed["agents"][A1]["loaded"]["lms"] = []         # `lms ps` came back empty
    out = r.tick(now=1200.0)
    assert out["entry_status"]["m1/lms"]["placed"] == 1
    assert [a.kind for a in calls] == ["load"]
    observed["agents"][A1]["loaded"]["lms"] = ["x"]      # m1 really gone
    out = r.tick(now=1230.0)
    assert out["entry_status"]["m1/lms"]["placed"] == 1  # re-load issued now
    assert [a.kind for a in calls] == ["load", "load"]
    assert r.ledger["blank_since"] == {}

def test_applied_unload_and_pruning_clear_blank_since():
    r, calls, observed = _mk(auto=False, loaded=(True, True), fresh=True,
                             placed_at={A1: 100.0, A2: 500.0})
    r.tick(now=1000.0)                                   # surplus -> proposal on A1
    _blank_llama(observed)
    r.tick(now=1030.0)
    assert r.ledger["blank_since"]["m1/llama"] == {A1: 1030.0}
    r.apply(r.proposals()[0]["id"], now=1031.0)
    assert r.ledger["blank_since"] == {}
    r.ledger["blank_since"] = {"m1/llama": {A2: 1.0, "z" * 32: 1.0}, "gone/llama": {A1: 1.0}}
    observed["agents"][A2]["loaded"]["llama"] = ["m1"]
    r.tick(now=1060.0)
    assert r.ledger["blank_since"] == {}

def test_grace_covers_every_confirmed_entry_on_a_shared_host():
    r, calls, observed = _mk(auto=True, fresh=True)
    st = r._get_state()
    st["entries"][0]["provider"] = "lms"
    st["entries"].append({"model": "m2", "provider": "lms", "placement": "auto",
                          "failover": "auto", "priority": 2, "min_replicas": 1,
                          "max_replicas": 1})
    for a in observed["agents"].values():
        a["provider_caps"] = ["lms"]; a["loaded"] = {"lms": []}; a["server_state"] = None
    observed["model_sizes_mb"].update({"lms:m1": 4000, "lms:m2": 4000})
    r.ledger["placed_at"] = {"m1/lms": {A1: 900.0}, "m2/lms": {A1: 900.0}}
    observed["agents"][A1]["loaded"]["lms"] = ["m1", "m2"]
    r.tick(now=1000.0)
    assert r.ledger["confirmed"] == {"m1/lms": {A1}, "m2/lms": {A1}}
    observed["agents"][A1]["loaded"]["lms"] = []         # whole block blank
    out = r.tick(now=1030.0)
    assert {k: v["placed"] for k, v in out["entry_status"].items()} == {"m1/lms": 1, "m2/lms": 1}
    assert r.ledger["blank_since"] == {"m1/lms": {A1: 1030.0}, "m2/lms": {A1: 1030.0}}
    assert calls == []

def test_removed_entry_gets_no_grace_injection():
    r, calls, observed = _confirmed()
    r._get_state()["entries"].clear()                    # operator drops m1
    _blank_llama(observed)
    out = r.tick(now=1200.0)
    assert out["entry_status"] == {} and r.ledger["blank_since"] == {}
    assert r.ledger["confirmed"] == {}
    assert pl._residents({"entries": []}, r.observe(now=1200.0), r.ledger_view, 1200.0) == {}
