"""#472: hysteresis windows for scale decisions."""
from __future__ import annotations
import autopilot_planner as pl

E = {"model": "m1", "provider": "llama", "placement": "auto",
     "failover": "auto", "priority": 100, "min_replicas": 1,
     "max_replicas": 3,
     "autoscale": {"target_saturation": 0.75, "up_window_s": 120,
                   "down_window_s": 900}}

def _hist(pairs):
    return [(float(t), float(s)) for t, s in pairs]

def test_up_when_saturated_for_full_window():
    h = _hist([(880, 0.9), (940, 0.85), (1000, 0.95)])
    assert pl.evaluate_autoscale(E, ["a"], h, now=1000.0) == "up"

def test_hold_when_spike_shorter_than_window():
    h = _hist([(880, 0.2), (995, 0.9), (1000, 0.95)])
    assert pl.evaluate_autoscale(E, ["a"], h, now=1000.0) == "hold"

def test_down_after_long_quiet_window():
    h = _hist([(t, 0.1) for t in range(0, 1001, 100)])
    assert pl.evaluate_autoscale(E, ["a", "b"], h, now=1000.0) == "down"

def test_no_down_below_min_replicas():
    h = _hist([(t, 0.1) for t in range(0, 1001, 100)])
    assert pl.evaluate_autoscale(E, ["a"], h, now=1000.0) == "hold"

def test_no_up_at_max_replicas():
    h = _hist([(880, 0.9), (1000, 0.95)])
    assert pl.evaluate_autoscale(E, ["a", "b", "c"], h, now=1000.0) == "hold"

def test_hold_with_no_history():
    assert pl.evaluate_autoscale(E, ["a"], [], now=1000.0) == "hold"

# --- plan()-level wiring -----------------------------------------------

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

def _ledger(**over):
    base = {"last_action_ts": {}, "placed_at": {}, "in_flight_migrations": 0,
            "backoff_until": {}}
    base.update(over)
    return base

def _obs(agents, sizes=None, sat_history=None):
    obs = {"agents": agents, "model_sizes_mb": sizes or {"llama:m1": 8000}}
    if sat_history is not None:
        obs["sat_history"] = sat_history
    return obs

def test_plan_emits_scale_up_when_saturated_for_full_window():
    # A1 already holds the one required replica; saturated for the full
    # up-window (mirrors test_up_when_saturated_for_full_window's fixture).
    e = {**E, "min_replicas": 1, "max_replicas": 2}
    obs = _obs(_agents(**{A1: {"loaded": {"llama": ["m1"]}}}),
               sat_history={"m1/llama": _hist(
                   [(880, 0.9), (940, 0.85), (1000, 0.95)])})
    acts = pl.plan(_desired([e]), obs, _ledger(), now=1000.0)
    scale_ups = [a for a in acts if a.kind == "scale_up"]
    assert len(scale_ups) == 1
    assert scale_ups[0].agent_id == A2
    assert scale_ups[0].entry_key == "m1/llama"

def test_plan_scale_down_picks_lru_agent_from_placed_at():
    # Both A1 and A2 hold the model; quiet for the full down-window
    # (mirrors test_down_after_long_quiet_window's fixture). A1 was placed
    # first (ts=100), so it's the LRU replica to remove.
    e = {**E, "min_replicas": 1, "max_replicas": 3}
    obs = _obs(_agents(**{A1: {"loaded": {"llama": ["m1"]}},
                          A2: {"loaded": {"llama": ["m1"]}}}),
               sat_history={"m1/llama": _hist(
                   [(t, 0.1) for t in range(0, 1001, 100)])})
    led = _ledger()
    led["placed_at"]["m1/llama"] = {A1: 100.0, A2: 500.0}
    acts = pl.plan(_desired([e]), obs, led, now=1000.0)
    scale_downs = [a for a in acts if a.kind == "scale_down"]
    assert len(scale_downs) == 1
    assert scale_downs[0].agent_id == A1
