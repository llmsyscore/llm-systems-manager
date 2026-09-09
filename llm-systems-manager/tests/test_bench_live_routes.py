"""#879: live-bench history store, baselines, prune, proxies, agent-token gate."""
from __future__ import annotations

import pytest

import bench_live as bl

AGENT = {"agent_id": "a" * 32, "token": "tok"}


def _doc(run_id, model="org/m:Q4", pred=100.0, levels=1):
    return {"run_id": run_id, "model_id": model, "ok": True, "bench": "qualitative",
            "config": {"bench": "qualitative", "concurrency": list(range(1, levels + 1))},
            "levels": [{"level": i + 1, "concurrency": i + 1, "wall_s": 5.0,
                        "rows": [], "all": {"pred_tps": pred, "prompt_tps": 2000.0, "latency_s": 3.0,
                                            "accept_rate": None, "agg_pred_tps": pred * (i + 1), "completion_tokens": 100}}
                       for i in range(levels)],
            "energy_wh": 1.0, "wh_per_ktok": 0.3}


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
