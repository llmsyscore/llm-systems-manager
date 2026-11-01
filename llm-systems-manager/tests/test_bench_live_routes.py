"""#879: live-bench history store, baselines, prune, proxies, agent-token gate."""
from __future__ import annotations

import sqlite3

import pytest

import bench_live as bl

AGENT = {"agent_id": "a" * 32, "token": "tok"}


def _doc(run_id, model="org/m:Q4", pred=100.0, levels=1, spread=None):
    d = {"run_id": run_id, "model_id": model, "ok": True, "bench": "qualitative",
            "config": {"bench": "qualitative", "concurrency": list(range(1, levels + 1))},
            "levels": [{"level": i + 1, "concurrency": i + 1, "wall_s": 5.0,
                        "rows": [], "all": {"pred_tps": pred, "prompt_tps": 2000.0, "latency_s": 3.0,
                                            "accept_rate": None, "agg_pred_tps": pred * (i + 1), "completion_tokens": 100}}
                       for i in range(levels)],
            "energy_wh": 1.0, "wh_per_ktok": 0.3}
    if spread is not None:
        d["levels"][0]["all"]["spread"] = {"pred_tps": spread}
    return d


@pytest.fixture
def app(tmp_path):
    from flask import Flask
    app = Flask(__name__)
    calls = []

    def proxy(kind, method, path, **kw):
        calls.append((kind, method, path, kw.get("json")))
        return {"ok": True, "proxied": path}
    bl.register_routes(app, None, db_path=str(tmp_path / "t.db"), proxy=proxy,
                       agent_by_token=lambda t: AGENT if t == "tok" else None,
                       request_agent=lambda p: AGENT, note_tool_start=lambda p, t: None)
    c = app.test_client()
    c.calls = calls
    return c


def test_store_requires_agent_token(app):
    r = app.post("/api/benchmark/live/store", json=_doc("r1"))
    assert r.status_code == 403
    r = app.post("/api/benchmark/live/store", json=_doc("r1"), headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.get_json()["ok"]


def test_store_validation_and_size(app):
    h = {"Authorization": "Bearer tok"}
    assert app.post("/api/benchmark/live/store", json={"model_id": "m"}, headers=h).status_code == 400
    big = _doc("r2"); big["levels"][0]["rows"] = [{"x": "y" * 300000}]
    assert app.post("/api/benchmark/live/store", json=big, headers=h).status_code == 413


def test_runs_listing_get_baseline_delete_prune(app):
    h = {"Authorization": "Bearer tok"}
    for i in range(55):
        app.post("/api/benchmark/live/store", json=_doc(f"r{i}", pred=float(i)), headers=h)
    lst = app.get("/api/benchmark/live/runs?model_id=org/m:Q4").get_json()
    assert lst["ok"] and len(lst["runs"]) == 50 and lst["runs"][0]["run_id"] == "r54"
    assert "result_json" not in lst["runs"][0] and lst["runs"][0]["gen_tps"] == 54.0
    assert app.get("/api/benchmark/live/runs/r0").status_code == 404  # pruned
    one = app.get("/api/benchmark/live/runs/r54").get_json()
    assert one["ok"] and one["run"]["levels"][0]["all"]["pred_tps"] == 54.0
    assert app.post("/api/benchmark/live/runs/r10/baseline").get_json()["ok"]
    assert app.post("/api/benchmark/live/runs/r11/baseline").get_json()["ok"]
    lst = app.get("/api/benchmark/live/runs?model_id=org/m:Q4").get_json()
    assert [r["run_id"] for r in lst["runs"] if r["baseline"]] == ["r11"]
    for i in range(55, 70):
        app.post("/api/benchmark/live/store", json=_doc(f"r{i}"), headers=h)
    lst = app.get("/api/benchmark/live/runs?model_id=org/m:Q4").get_json()
    assert any(r["run_id"] == "r11" for r in lst["runs"])  # baseline survives pruning
    deleted_one = app.delete("/api/benchmark/live/runs/r11").get_json()
    assert deleted_one["ok"]
    assert app.get("/api/benchmark/live/runs/r11").status_code == 404
    cleared = app.delete("/api/benchmark/live/runs?model_id=org/m:Q4").get_json()
    assert cleared["deleted"] > 0
    assert app.get("/api/benchmark/live/runs?model_id=org/m:Q4").get_json()["runs"] == []


def test_duplicate_run_id_ignored(app):
    h = {"Authorization": "Bearer tok"}
    app.post("/api/benchmark/live/store", json=_doc("dup", pred=1.0), headers=h)
    app.post("/api/benchmark/live/store", json=_doc("dup", pred=2.0), headers=h)
    assert app.get("/api/benchmark/live/runs/dup").get_json()["run"]["levels"][0]["all"]["pred_tps"] == 1.0


def test_proxies(app):
    assert app.get("/api/benchmark/live/preflight").get_json()["proxied"] == "/llama/bench/live/preflight"
    assert app.post("/api/benchmark/live/setup", json={"prefetch": ["qualitative"]}).get_json()["proxied"] == "/llama/bench/live/setup"
    r = app.post("/api/benchmark/live/run", json={"model_id": "m", "bench": "qualitative"}).get_json()
    assert r["proxied"] == "/llama/bench/live/run"
    assert app.calls[-1][3] == {"model_id": "m", "bench": "qualitative"}


def test_read_run_returns_meta_and_doc(app, tmp_path):
    import sqlite3
    h = {"Authorization": "Bearer tok"}
    app.post("/api/benchmark/live/store", json=_doc("rr1"), headers=h)
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    meta, doc = bl.read_run(conn, "rr1")
    assert meta["run_id"] == "rr1" and meta["agent_id"] == AGENT["agent_id"] and doc["levels"]
    assert bl.read_run(conn, "missing") is None


SP = {"n": 8, "min": 90.0, "p25": 95.0, "p50": 100.0, "p75": 105.0, "max": 110.0}


def test_runs_speed_latest_expose_gen_spread(app):
    h = {"Authorization": "Bearer tok"}
    app.post("/api/benchmark/live/store", json=_doc("r-old"), headers=h)
    app.post("/api/benchmark/live/store", json=_doc("r-new", spread=SP), headers=h)
    runs = app.get("/api/benchmark/live/runs?model_id=org/m:Q4").get_json()["runs"]
    by = {r["run_id"]: r for r in runs}
    assert by["r-new"]["gen_spread"] == SP and by["r-old"]["gen_spread"] is None
    speed = app.get("/api/benchmark/live/speed?model_id=org/m:Q4").get_json()["hosts"]
    assert speed[0]["gen_spread"] == SP
    latest = app.get("/api/benchmark/live/latest").get_json()["models"]["org/m:Q4"]
    assert latest["gen_spread"] == SP


def test_gen_spread_of_tolerates_old_and_odd_docs():
    assert bl.gen_spread_of(_doc("x", spread=SP)) == SP
    assert bl.gen_spread_of(_doc("x")) is None
    assert bl.gen_spread_of({}) is None and bl.gen_spread_of({"levels": []}) is None
    assert bl.gen_spread_of({"levels": [{"all": {"spread": None}}]}) is None


def test_latest_per_agent_parses_no_result_docs(app, tmp_path, monkeypatch):
    h = {"Authorization": "Bearer tok"}
    app.post("/api/benchmark/live/store", json=_doc("r-old", spread=SP), headers=h)
    app.post("/api/benchmark/live/store", json=_doc("r-new"), headers=h)
    calls = []
    real = bl.gen_spread_of
    monkeypatch.setattr(bl, "gen_spread_of", lambda doc: calls.append(1) or real(doc))
    rows = bl.latest_per_agent(sqlite3.connect(tmp_path / "t.db"), "org/m:Q4")
    assert [r["run_id"] for r in rows] == ["r-new"] and rows[0]["gen_spread"] is None
    assert len(calls) == 0
    app.post("/api/benchmark/live/store", json=_doc("r-newest", spread=SP), headers=h)
    calls.clear()
    rows = bl.latest_per_agent(sqlite3.connect(tmp_path / "t.db"), "org/m:Q4")
    assert [r["run_id"] for r in rows] == ["r-newest"] and rows[0]["gen_spread"] == SP
    assert len(calls) == 0


def test_store_fills_gen_spread_column(app, tmp_path):
    import json
    h = {"Authorization": "Bearer tok"}
    app.post("/api/benchmark/live/store", json=_doc("s-yes", spread=SP), headers=h)
    app.post("/api/benchmark/live/store", json=_doc("s-no"), headers=h)
    conn = sqlite3.connect(tmp_path / "t.db")
    got = dict(conn.execute("SELECT run_id, gen_spread_json FROM bench_live_runs").fetchall())
    assert json.loads(got["s-yes"]) == SP and got["s-no"] is None


def test_store_records_llama_build_from_doc_or_manager_fallback(tmp_path):
    """#907: the run's llama.cpp build is stored from the doc, else from the manager's last sample."""
    from flask import Flask
    app = Flask(__name__)
    bl.register_routes(app, None, db_path=str(tmp_path / "b.db"), proxy=lambda *a, **k: {"ok": True},
                       agent_by_token=lambda t: AGENT if t == "tok" else None,
                       request_agent=lambda p: AGENT, note_tool_start=lambda p, t: None,
                       llama_build_of=lambda aid: "b7000-mgr")
    c = app.test_client()
    h = {"Authorization": "Bearer tok"}
    assert c.post("/api/benchmark/live/store", json={**_doc("r1"), "llama_build": "b6990-agent"}, headers=h).status_code == 200
    assert c.post("/api/benchmark/live/store", json=_doc("r2"), headers=h).status_code == 200
    rows = bl.speed_table(str(tmp_path / "b.db"), "org/m:Q4")
    assert rows[0]["llama_build"] == "b7000-mgr"          # newest run (r2) used the fallback
    runs = {r["run_id"]: r["llama_build"] for r in bl.latest_per_agent(sqlite3.connect(tmp_path / "b.db"), "org/m:Q4")}
    assert runs == {"r2": "b7000-mgr"}
    sp = c.get("/api/benchmark/live/speed?model_id=org/m:Q4").get_json()
    assert sp["hosts"][0]["llama_build"] == "b7000-mgr"
    stored = sqlite3.connect(tmp_path / "b.db").execute(
        "SELECT run_id, llama_build FROM bench_live_runs ORDER BY id").fetchall()
    assert stored == [("r1", "b6990-agent"), ("r2", "b7000-mgr")]


def test_latest_is_newest_ok_run_per_model_for_the_selected_agent(app):
    h = {"Authorization": "Bearer tok"}
    app.post("/api/benchmark/live/store", json=_doc("r1", model="org/a", pred=50.0), headers=h)
    app.post("/api/benchmark/live/store", json=_doc("r2", model="org/a", pred=60.0), headers=h)
    app.post("/api/benchmark/live/store", json=_doc("r3", model="org/b", pred=70.0), headers=h)
    bad = _doc("r4", model="org/b", pred=90.0); bad["ok"] = False
    app.post("/api/benchmark/live/store", json=bad, headers=h)
    r = app.get("/api/benchmark/live/latest")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["agent_id"] == AGENT["agent_id"]
    assert set(d["models"]) == {"org/a", "org/b"}
    assert d["models"]["org/a"]["run_id"] == "r2" and d["models"]["org/a"]["gen_tps"] == 60.0
    assert d["models"]["org/b"]["run_id"] == "r3" and d["models"]["org/b"]["bench"] == "qualitative"
    assert d["models"]["org/a"]["ts"] and d["models"]["org/a"]["hostname"]
    assert app.get("/api/benchmark/live/latest?agent_id=" + "f" * 32).get_json()["models"] == {}


def test_live_run_goes_through_start_or_queue_when_wired(tmp_path):
    from flask import Flask, jsonify
    app = Flask(__name__)
    seen = []

    def start_or_queue(provider, tool, path, cancel, body):
        seen.append((provider, tool, path, cancel, body))
        return jsonify({"ok": True, "queued": True, "job_id": "j1", "position": 1, "wait_for": "Autotune on gpu-01"}), 202
    bl.register_routes(app, None, db_path=str(tmp_path / "q.db"), proxy=lambda *a, **k: {"ok": False},
                       agent_by_token=lambda t: None, request_agent=lambda p: AGENT,
                       note_tool_start=lambda p, t: None, start_or_queue=start_or_queue)
    c = app.test_client()
    r = c.post("/api/benchmark/live/run", json={"model_id": "org/m", "bench": "qualitative", "provider": "llama"})
    assert r.status_code == 202 and r.get_json()["job_id"] == "j1"
    assert seen == [("llama", "benchmark", "/llama/bench/live/run", "/llama/bench/cancel",
                     {"model_id": "org/m", "bench": "qualitative"})]


def _builds_app(tmp_path, quiet=None, fleet=False):
    from flask import Flask
    app = Flask(__name__)
    calls, started = [], []

    def proxy(kind, method, path, **kw):
        calls.append((kind, method, path, kw.get("json")))
        return {"ok": True, "proxied": path}
    extra = {}
    if fleet:
        hosts = [{"agent_id": AGENT["agent_id"], "hostname": "alpha", "online": True, "model": "org/m:Q4",
                  "state": "awake"}]
        extra = {"fleet_hosts": lambda provider=None: hosts,
                 "run_on_agent": lambda aid, body, provider="llama": (started.append(body), (True, "run-a"))[-1],
                 "cancel_on_agent": lambda aid, provider="llama": True}
    bl.register_routes(app, None, db_path=str(tmp_path / "b.db"), proxy=proxy,
                       agent_by_token=lambda t: AGENT if t == "tok" else None,
                       request_agent=lambda p: AGENT, note_tool_start=lambda p, t: None,
                       quiet_build_change=quiet, **extra)
    c = app.test_client()
    c.calls, c.started = calls, started
    return c


def test_live_builds_needs_admin(tmp_path, monkeypatch):
    import auth
    c = _builds_app(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(auth, "effective_role", lambda: "operator")
        r = c.post("/api/benchmark/live/run", json={"model_id": "m", "builds": ["/x/llama-server"]})
        assert r.status_code == 403 and r.get_json()["error"] == "switching builds needs an admin"
        assert c.calls == []
        r = c.post("/api/benchmark/live/run", json={"model_id": "m", "builds": []})
        assert r.status_code == 200 and c.calls[-1][2] == "/llama/bench/live/run"
        m.setattr(auth, "effective_role", lambda: "admin")
        r = c.post("/api/benchmark/live/run", json={"model_id": "m", "builds": ["/x/llama-server"]})
        assert r.status_code == 200
        assert c.calls[-1][3] == {"model_id": "m", "builds": ["/x/llama-server"]}


def test_live_builds_arms_quiet(tmp_path, monkeypatch):
    import auth
    seen = []
    c = _builds_app(tmp_path, quiet=seen.append)
    with monkeypatch.context() as m:
        m.setattr(auth, "effective_role", lambda: "admin")
        c.post("/api/benchmark/live/run", json={"model_id": "m"})
        assert seen == []
        c.post("/api/benchmark/live/run", json={"model_id": "m", "builds": ["/x/llama-server"]})
        assert seen == [AGENT["agent_id"]]
    h = {"Authorization": "Bearer tok"}
    c.post("/api/benchmark/live/store", json=_doc("q1"), headers=h)
    assert seen == [AGENT["agent_id"]]
    d = _doc("q2"); d.update(build="b2", build_path="/y/llama-server")
    c.post("/api/benchmark/live/store", json=d, headers=h)
    assert seen == [AGENT["agent_id"]] * 2


def test_fleet_strips_builds(tmp_path):
    c = _builds_app(tmp_path, fleet=True)
    r = c.post("/api/benchmark/live/fleet", json={"model_id": "org/m:Q4",
                                                  "config": {"bench": "qualitative", "builds": ["/x/llama-server"]}})
    assert r.status_code == 200
    import time
    t0 = time.time()
    while not c.started and time.time() - t0 < 3:
        time.sleep(0.02)
    assert c.started and "builds" not in c.started[0]
    assert c.started[0]["bench"] == "qualitative"
