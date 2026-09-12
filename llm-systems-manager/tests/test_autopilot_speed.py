"""#907: placement by the measured speed table (live benchmark rankings)."""
from __future__ import annotations
import sqlite3
import autopilot as ap
import autopilot_planner as pl
import bench_live

A1, A2, A3 = "a" * 32, "b" * 32, "c" * 32
NOW = 1_800_000_000.0
DAY = 86400.0

def _agent(**over):
    base = {"provider_caps": ["llama"], "live": True, "vram_total_mb": 24000,
            "vram_free_mb": 20000, "loaded": {"llama": []}, "server_state": "awake",
            "saturation": {}, "llama_build": "b7000"}
    base.update(over)
    return base

def _ledger():
    return {"last_action_ts": {}, "placed_at": {}, "in_flight_migrations": 0,
            "backoff_until": {}, "unload_backoff": {}, "confirmed": {}}

E = {"model": "m1", "provider": "llama", "placement": "auto", "failover": "semi",
     "priority": 100, "min_replicas": 1, "max_replicas": 1, "rank_by": "speed"}

def _row(tps, wh=None, age_days=1.0, build="b7000", host="h"):
    return {"gen_tps": tps, "wh_per_ktok": wh, "latency_s": 0.5, "ts": NOW - age_days * DAY,
            "llama_build": build, "hostname": host, "run_id": "r"}

def _obs(agents, speed):
    return {"agents": agents, "model_sizes_mb": {"llama:m1": 8000}, "model_gpu_layers": {},
            "speed": {"llama:m1": speed}}

def _desired(entry, **glob):
    return {"enabled": True, "entries": [entry], "hosts": {}, **glob}

# ── planner ordering ─────────────────────────────────────────────────

def test_fastest_measured_host_wins_over_dict_order():
    obs = _obs({A1: _agent(), A2: _agent()}, {A1: _row(40.0), A2: _row(65.0)})
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert [a.agent_id for a in acts] == [A2]
    assert "measured 65.0 t/s" in acts[0].reason

def test_energy_objective_prefers_lowest_wh_per_ktok():
    obs = _obs({A1: _agent(), A2: _agent()}, {A1: _row(40.0, wh=0.9), A2: _row(65.0, wh=1.5)})
    acts = pl.plan(_desired({**E, "rank_by": "energy"}), obs, _ledger(), NOW)
    assert acts[0].agent_id == A1

def test_capacity_objective_keeps_dict_order():
    obs = _obs({A1: _agent(), A2: _agent()}, {A1: _row(40.0), A2: _row(65.0)})
    acts = pl.plan(_desired({**E, "rank_by": "capacity"}), obs, _ledger(), NOW)
    assert acts[0].agent_id == A1

def test_capacity_check_still_gates_a_fast_host():
    obs = _obs({A1: _agent(), A2: _agent(vram_free_mb=4000)}, {A1: _row(40.0), A2: _row(65.0)})
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert acts[0].agent_id == A1

def test_stale_run_is_advisory_ranks_after_fresh_measurement():
    obs = _obs({A1: _agent(), A2: _agent()}, {A1: _row(40.0, age_days=2), A2: _row(65.0, age_days=45)})
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert acts[0].agent_id == A1

def test_speed_max_age_days_is_configurable():
    obs = _obs({A1: _agent(), A2: _agent()}, {A1: _row(40.0, age_days=2), A2: _row(65.0, age_days=45)})
    acts = pl.plan(_desired(E, speed_max_age_days=60), obs, _ledger(), NOW)
    assert acts[0].agent_id == A2

def test_different_build_is_advisory():
    obs = _obs({A1: _agent(), A2: _agent(llama_build="b7100")},
               {A1: _row(40.0), A2: _row(65.0, build="b7000")})
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert acts[0].agent_id == A1

def test_unknown_build_on_either_side_is_not_advisory():
    obs = _obs({A1: _agent(), A2: _agent(llama_build="")}, {A1: _row(40.0), A2: _row(65.0, build="")})
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert acts[0].agent_id == A2

def test_advisory_still_beats_an_unmeasured_host():
    obs = _obs({A1: _agent(), A2: _agent()}, {A2: _row(65.0, age_days=45)})
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert acts[0].agent_id == A2

def test_pinned_placement_ignores_the_table():
    obs = _obs({A1: _agent(), A2: _agent()}, {A1: _row(40.0), A2: _row(65.0)})
    acts = pl.plan(_desired({**E, "placement": A1}), obs, _ledger(), NOW)
    assert acts[0].agent_id == A1

def test_no_table_keeps_legacy_order_and_reason():
    obs = _obs({A1: _agent(), A2: _agent()}, {})
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert acts[0].agent_id == A1 and "measured" not in acts[0].reason

def test_scale_up_uses_the_ranking_too():
    e = {**E, "max_replicas": 2, "autoscale": {"target_saturation": 0.5, "up_window_s": 10, "down_window_s": 900}}
    obs = _obs({A1: _agent(loaded={"llama": ["m1"]}), A2: _agent(), A3: _agent()},
               {A2: _row(30.0), A3: _row(70.0)})
    obs["sat_history"] = {"m1/llama": [(NOW - 20, 0.9), (NOW - 5, 0.9)]}
    acts = pl.plan(_desired(e), obs, _ledger(), NOW)
    assert [a.kind for a in acts] == ["scale_up"] and acts[0].agent_id == A3

# ── entry_status: basis / pick / table ────────────────────────────────

def test_entry_status_reports_measured_pick_and_table():
    obs = _obs({A1: _agent(), A2: _agent()}, {A1: _row(40.0, host="hA"), A2: _row(65.0, wh=1.2, host="hB")})
    st = pl.entry_status(_desired(E), obs, _ledger(), NOW)["m1/llama"]
    assert st["basis"] == "measured" and st["pick"] == A2
    assert [r["agent_id"] for r in st["speed"]] == [A2, A1]
    assert st["speed"][0]["tier"] == "measured" and st["speed"][0]["hostname"] == "hB"
    assert st["speed"][0]["age_s"] == DAY

def test_entry_status_advisory_when_best_run_is_stale():
    obs = _obs({A1: _agent()}, {A1: _row(40.0, age_days=45)})
    st = pl.entry_status(_desired(E), obs, _ledger(), NOW)["m1/llama"]
    assert st["basis"] == "advisory" and st["pick"] == A1 and st["speed"][0]["tier"] == "advisory"

def test_entry_status_capacity_only_without_a_table():
    obs = _obs({A1: _agent()}, {})
    st = pl.entry_status(_desired(E), obs, _ledger(), NOW)["m1/llama"]
    assert st["basis"] == "capacity" and st["pick"] == A1 and st["speed"] == []

def test_entry_status_pick_skips_dead_hosts():
    obs = _obs({A1: _agent(), A2: _agent(live=False)}, {A1: _row(40.0), A2: _row(65.0)})
    st = pl.entry_status(_desired(E), obs, _ledger(), NOW)["m1/llama"]
    assert st["pick"] == A1

def test_entry_status_pinned_entry_has_no_basis():
    obs = _obs({A1: _agent(), A2: _agent()}, {A1: _row(40.0), A2: _row(65.0)})
    st = pl.entry_status(_desired({**E, "placement": A1}), obs, _ledger(), NOW)["m1/llama"]
    assert st["basis"] is None and st["pick"] == A1

# ── state validation ──────────────────────────────────────────────────

def test_state_defaults_rank_by_and_max_age():
    st = ap.validate_state({"enabled": True, "entries": [{"model": "m1", "provider": "llama"}], "hosts": {}})
    assert st["entries"][0]["rank_by"] == "speed" and st["speed_max_age_days"] == 30

def test_state_keeps_rank_by_and_max_age():
    st = ap.validate_state({"enabled": True, "speed_max_age_days": "7", "entries": [
        {"model": "m1", "provider": "llama", "rank_by": "energy"}], "hosts": {}})
    assert st["entries"][0]["rank_by"] == "energy" and st["speed_max_age_days"] == 7

def test_state_rejects_bad_rank_by_and_max_age():
    import pytest
    with pytest.raises(ValueError, match="rank_by"):
        ap.validate_state({"entries": [{"model": "m1", "provider": "llama", "rank_by": "fast"}]})
    with pytest.raises(ValueError, match="speed_max_age_days"):
        ap.validate_state({"entries": [], "speed_max_age_days": 0})

# ── build_observed: speed table + host build ─────────────────────────

def _deps(rows, build="b7000"):
    def agents():
        return {"agents": {A1: {"capabilities": {"llama": True}, "status": "approved", "hostname": "hA"},
                           A2: {"capabilities": {"llama": True}, "status": "approved", "hostname": "hB"}},
                "global": {"autopilot": {"entries": [{"model": "m1", "provider": "llama"},
                                                     {"model": "v1", "provider": "vllm"}]}}}
    return {"agents": agents, "liveness": lambda a: "live",
            "provider_snapshot": lambda prov, aid: {"sample": {"llama": {"build": build, "state": "awake"}}},
            "saturation": lambda prov, aid: {"value": None},
            "model_sizes": lambda: {}, "model_gpu_layers": lambda: {},
            "speed": lambda model_id: rows if model_id == "m1" else []}

def test_build_observed_carries_speed_rows_keyed_like_sizes():
    rows = [{"agent_id": A2, "gen_tps": 65.0, "latency_s": 0.4, "wh_per_ktok": 1.2, "run_id": "r2",
             "ts": "2026-09-09T21:01:18.993224+00:00", "llama_build": "b6990"},
            {"agent_id": "z" * 32, "gen_tps": 99.0, "ts": "2026-09-09T21:01:18+00:00"}]
    obs = ap.build_observed(_deps(rows))
    sp = obs["speed"]["llama:m1"]
    assert set(sp) == {A2}                      # unknown agents dropped
    assert sp[A2]["gen_tps"] == 65.0 and sp[A2]["hostname"] == "hB" and sp[A2]["llama_build"] == "b6990"
    from datetime import datetime
    assert abs(sp[A2]["ts"] - datetime.fromisoformat("2026-09-09T21:01:18.993224+00:00").timestamp()) < 1
    assert "vllm:v1" not in obs["speed"]
    assert obs["agents"][A1]["llama_build"] == "b7000"

def test_build_observed_without_speed_dep_is_empty_not_missing():
    d = _deps([]); d.pop("speed")
    assert ap.build_observed(d)["speed"] == {}

# ── bench_live: llama_build column ────────────────────────────────────

def test_bench_live_migrates_and_reads_llama_build(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE bench_live_runs (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
        model_id TEXT NOT NULL, agent_id TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 1,
        baseline INTEGER NOT NULL DEFAULT 0, gen_tps REAL, ppt_tps REAL, latency_s REAL, accept_rate REAL,
        wh_per_ktok REAL, config_json TEXT NOT NULL, result_json TEXT NOT NULL)""")
    conn.execute("INSERT INTO bench_live_runs (run_id, model_id, agent_id, ts, gen_tps, config_json, result_json)"
                 " VALUES ('r1','m1',?, '2026-09-09T00:00:00+00:00', 50.0, '{}', '{}')", (A1,))
    conn.commit()
    bench_live.init_table(conn)
    rows = bench_live.latest_per_agent(conn, "m1")
    assert rows[0]["llama_build"] == "" and rows[0]["gen_tps"] == 50.0
    conn.close()
    assert bench_live.speed_table(str(db), "m1")[0]["agent_id"] == A1
    assert bench_live.speed_table(str(tmp_path / "missing.db"), "m1") == []

def test_entry_status_pick_is_capacity_gated_like_plan():
    obs = _obs({A1: _agent(), A2: _agent(vram_free_mb=4000)}, {A1: _row(40.0), A2: _row(65.0)})
    st = pl.entry_status(_desired(E), obs, _ledger(), NOW)["m1/llama"]
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert st["pick"] == acts[0].agent_id == A1 and st["basis"] == "measured"

def test_entry_status_no_fit_means_no_pick_and_no_basis():
    obs = _obs({A1: _agent(vram_free_mb=4000)}, {A1: _row(40.0)})
    st = pl.entry_status(_desired(E), obs, _ledger(), NOW)["m1/llama"]
    assert st["pick"] is None and st["basis"] is None and st["blocked"]

def test_entry_status_placed_entry_reports_its_best_ranked_copy():
    obs = _obs({A1: _agent(loaded={"llama": ["m1"]}, vram_free_mb=2000), A2: _agent()},
               {A1: _row(40.0, age_days=45), A2: _row(65.0)})
    st = pl.entry_status(_desired(E), obs, _ledger(), NOW)["m1/llama"]
    assert st["placed"] == 1 and st["pick"] == A1 and st["basis"] == "advisory"

def test_load_reason_says_advisory_for_a_stale_run():
    obs = _obs({A1: _agent()}, {A1: _row(40.0, age_days=45)})
    acts = pl.plan(_desired(E), obs, _ledger(), NOW)
    assert "advisory 40.0 t/s" in acts[0].reason and "measured" not in acts[0].reason
