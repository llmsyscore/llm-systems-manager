"""#884: fleet-wide live benchmark job + measured speed table."""
from __future__ import annotations

import time

import pytest

import bench_live as bl

A1 = {"agent_id": "a" * 32, "token": "t1", "hostname": "alpha"}
A2 = {"agent_id": "b" * 32, "token": "t2", "hostname": "bravo"}
A3 = {"agent_id": "c" * 32, "token": "t3", "hostname": "charlie"}
MODEL = "org/m:Q4"
TOK = {A1["token"]: A1, A2["token"]: A2, A3["token"]: A3}


def _doc(run_id, pred, agent_ok=True):
    return {"run_id": run_id, "model_id": MODEL, "ok": agent_ok, "bench": "throughput_1k",
            "config": {"bench": "throughput_1k", "concurrency": [1, 2]},
            "levels": [{"level": 1, "concurrency": 1, "wall_s": 5.0, "rows": [],
                        "all": {"pred_tps": pred, "prompt_tps": 400.0, "latency_s": 10.0, "accept_rate": None,
                                "agg_pred_tps": pred, "completion_tokens": 100}},
                       {"level": 2, "concurrency": 2, "wall_s": 5.0, "rows": [],
                        "all": {"pred_tps": pred - 5, "prompt_tps": 390.0, "latency_s": 12.0, "accept_rate": None,
                                "agg_pred_tps": pred * 1.8, "completion_tokens": 100}}],
            "energy_wh": 1.0, "wh_per_ktok": 0.5}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from flask import Flask
    monkeypatch.setattr(bl, "FLEET_POLL_S", 0.05)
    app = Flask(__name__)
    hosts = [{"agent_id": A1["agent_id"], "hostname": "alpha", "online": True, "model": MODEL, "state": "awake"},
             {"agent_id": A2["agent_id"], "hostname": "bravo", "online": True, "model": MODEL, "state": "awake"},
             {"agent_id": A3["agent_id"], "hostname": "charlie", "online": True, "model": "other", "state": "awake"}]
    started, cancelled = [], []

    def run_on_agent(aid, body):
        started.append((aid, body))
        if aid == A2["agent_id"] and body.get("fail"):
            return False, "Another benchmark or autotune is in progress"
        return True, f"run-{aid[:1]}"

    bl.register_routes(app, None, db_path=str(tmp_path / "t.db"), proxy=lambda *a, **k: {"ok": True},
                       agent_by_token=lambda t: TOK.get(t), request_agent=lambda p: A1,
                       note_tool_start=lambda p, t: None, fleet_hosts=lambda: hosts,
                       run_on_agent=run_on_agent, cancel_on_agent=lambda aid: cancelled.append(aid) or True)
    c = app.test_client()
    c.started, c.cancelled, c.hosts = started, cancelled, hosts
    return c


def _store(c, agent, doc):
    assert c.post("/api/benchmark/live/store", json=doc, headers={"Authorization": f"Bearer {agent['token']}"}).status_code == 200


def _wait(c, job_id, pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/benchmark/live/fleet/{job_id}").get_json()["job"]
        if pred(j):
            return j
        time.sleep(0.02)
    raise AssertionError("fleet job did not reach the expected state")


def test_hosts_marks_loaded(env):
    r = env.get(f"/api/benchmark/live/hosts?model_id={MODEL}").get_json()
    assert r["ok"] and [h["hostname"] for h in r["hosts"]] == ["alpha", "bravo", "charlie"]
    assert [h["loaded"] for h in r["hosts"]] == [True, True, False]


def test_fleet_job_runs_ranks_and_reports(env):
    r = env.post("/api/benchmark/live/fleet", json={"model_id": MODEL, "config": {"bench": "throughput_1k", "concurrency": [1, 2]}}).get_json()
    assert r["ok"] and len(r["hosts"]) == 2
    job = r["job_id"]
    _wait(env, job, lambda j: all(h["status"] == "running" for h in j["hosts"]))
    assert sorted(a for a, _ in env.started) == sorted([A1["agent_id"], A2["agent_id"]])
    assert all(b["model_id"] == MODEL and b["concurrency"] == [1, 2] for _, b in env.started)
    _store(env, A2, _doc("run-b", 70.0))
    _store(env, A1, _doc("run-a", 60.0))
    j = _wait(env, job, lambda j: j["done"])
    by = {h["agent_id"]: h for h in j["hosts"]}
    assert by[A1["agent_id"]]["status"] == "done" and by[A1["agent_id"]]["gen_tps"] == 60.0
    assert by[A2["agent_id"]]["agg_max_tps"] == pytest.approx(70.0 * 1.8) and by[A2["agent_id"]]["wh_per_ktok"] == 0.5
    assert j["ranking"] == [A2["agent_id"], A1["agent_id"]]
    sp = env.get(f"/api/benchmark/live/speed?model_id={MODEL}").get_json()
    assert [h["hostname"] for h in sp["hosts"]] == ["bravo", "alpha"] and sp["hosts"][0]["gen_tps"] == 70.0


def test_fleet_rejects_unloaded_or_unknown_hosts(env):
    r = env.post("/api/benchmark/live/fleet", json={"model_id": MODEL, "agents": [A3["agent_id"]], "config": {}})
    assert r.status_code == 400
    r = env.post("/api/benchmark/live/fleet", json={"model_id": "nothing-loaded", "config": {}})
    assert r.status_code == 400


def test_fleet_failed_start_and_cancel(env):
    r = env.post("/api/benchmark/live/fleet", json={"model_id": MODEL, "config": {"fail": True}}).get_json()
    j = _wait(env, r["job_id"], lambda j: any(h["status"] == "failed" for h in j["hosts"]))
    failed = next(h for h in j["hosts"] if h["status"] == "failed")
    assert failed["agent_id"] == A2["agent_id"] and "in progress" in failed["error"]
    _wait(env, r["job_id"], lambda j: next(h for h in j["hosts"] if h["agent_id"] == A1["agent_id"])["status"] == "running")
    c = env.post(f"/api/benchmark/live/fleet/{r['job_id']}/cancel").get_json()
    assert c["ok"] and c["cancelled"] == [A1["agent_id"]] and env.cancelled == [A1["agent_id"]]
    j = _wait(env, r["job_id"], lambda j: j["done"] and j["cancelled"])
    assert next(h for h in j["hosts"] if h["agent_id"] == A1["agent_id"])["status"] == "cancelled"


def test_fleet_unknown_job_404(env):
    assert env.get("/api/benchmark/live/fleet/nope").status_code == 404
    assert env.post("/api/benchmark/live/fleet/nope/cancel").status_code == 404


def test_fleet_flags_matrix_ignored_by_stale_agent(env):
    cfg = {"bench": "throughput_1k", "concurrency": [1, 2], "matrix": {"benches": ["throughput_1k"], "osls": [256]}}
    r = env.post("/api/benchmark/live/fleet", json={"model_id": MODEL, "config": cfg}).get_json()
    job = r["job_id"]
    _wait(env, job, lambda j: all(h["status"] == "running" for h in j["hosts"]))
    _store(env, A1, _doc("run-a", 60.0))  # doc's stored config has no "matrix" key
    _store(env, A2, _doc("run-b", 70.0))
    j = _wait(env, job, lambda j: j["done"])
    by = {h["agent_id"]: h for h in j["hosts"]}
    assert by[A1["agent_id"]]["matrix_ignored"] is True
    assert by[A2["agent_id"]]["matrix_ignored"] is True


def test_speed_uses_newest_ok_run_per_agent(env):
    _store(env, A1, _doc("s1", 10.0))
    _store(env, A1, _doc("s2", 30.0))
    _store(env, A1, _doc("s3", 99.0, agent_ok=False))
    _store(env, A2, _doc("s4", 20.0))
    sp = env.get(f"/api/benchmark/live/speed?model_id={MODEL}").get_json()
    assert [(h["hostname"], h["gen_tps"], h["run_id"]) for h in sp["hosts"]] == [("alpha", 30.0, "s2"), ("bravo", 20.0, "s4")]
    assert env.get("/api/benchmark/live/speed").status_code == 400
