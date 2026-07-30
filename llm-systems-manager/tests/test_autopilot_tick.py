"""#472: tick — auto executes, semi proposes, failure backoff, stale expiry."""
from __future__ import annotations
import threading
import autopilot as ap
import autopilot_planner as pl

A1, A2 = "a" * 32, "b" * 32

def _mk(auto=False, exec_ok=True):
    calls = []
    state = {"enabled": True, "hosts": {}, "entries": [
        {"model": "m1", "provider": "llama", "placement": "auto",
         "failover": "auto" if auto else "semi", "priority": 1,
         "min_replicas": 1, "max_replicas": 1}]}
    observed = {"agents": {
        A1: {"provider_caps": ["llama"], "live": True, "vram_total_mb": 24000,
             "vram_free_mb": 20000, "loaded": {"llama": []},
             "server_state": "awake", "idle_since": None, "saturation": {}}},
        "model_sizes_mb": {"llama:m1": 8000}, "sat_history": {}}
    r = ap.Reconciler(get_state=lambda: state,
                      build_observed=lambda: observed,
                      executor=lambda a: (calls.append(a), exec_ok)[1])
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
    r.tick(now=1000.0 + pl.COOLDOWN_S + 1)
    assert A1 not in r.ledger["placed_at"].get("m1/llama", {})

def test_placed_at_not_pruned_for_dead_agent():
    r, _, obs = _mk(auto=False)
    obs["agents"][A1]["live"] = False
    r.ledger["placed_at"]["m1/llama"] = {A1: 500.0}
    r.tick(now=1000.0)
    assert r.ledger["placed_at"]["m1/llama"][A1] == 500.0

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
