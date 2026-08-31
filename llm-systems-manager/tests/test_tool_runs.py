# llm-systems-manager/tests/test_tool_runs.py
"""#770: cross-tool run ledger — POST/GET/DELETE /api/tools/runs."""
from __future__ import annotations

import sqlite3

import manager_mod


def _mem_db():
    return sqlite3.connect(":memory:", check_same_thread=False)


def _client():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def _setup(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    return conn


def test_record_and_list_roundtrip(monkeypatch):
    _setup(monkeypatch)
    c = _client()
    r = c.post("/api/tools/runs", json={
        "tool": "benchmark", "model_id": "org/m",
        "gen_tps": 42.5, "ppt_tps": 900.0, "bench_tool": "llama-bench"})
    assert r.get_json()["ok"] is True
    r = c.post("/api/tools/runs", json={
        "tool": "autotune", "model_id": "org/m2", "ok": False,
        "ctx_size": 32768, "free_mb": 1010, "iters": 6, "converged": True})
    assert r.get_json()["ok"] is True
    runs = c.get("/api/tools/runs").get_json()["runs"]
    assert [x["tool"] for x in runs] == ["autotune", "benchmark"]
    assert runs[0]["ok"] is False
    assert runs[0]["summary"]["ctx_size"] == 32768
    assert runs[0]["summary"]["converged"] is True
    assert runs[1]["ok"] is True
    assert runs[1]["summary"]["gen_tps"] == 42.5
    assert runs[1]["summary"]["bench_tool"] == "llama-bench"


def test_unknown_tool_and_missing_model_400(monkeypatch):
    _setup(monkeypatch)
    c = _client()
    assert c.post("/api/tools/runs", json={"tool": "nmap", "model_id": "m"}).status_code == 400
    assert c.post("/api/tools/runs", json={"tool": "benchmark"}).status_code == 400
    assert c.post("/api/tools/runs", json={
        "tool": "benchmark", "model_id": "m", "provider": "bogus"}).status_code == 400


def test_provider_stored_and_totals_reported(monkeypatch):
    _setup(monkeypatch)
    c = _client()
    c.post("/api/tools/runs", json={"tool": "benchmark", "model_id": "m", "provider": "vllm"})
    c.post("/api/tools/runs", json={"tool": "benchmark", "model_id": "m2"})
    c.post("/api/tools/runs", json={"tool": "autotune", "model_id": "m3"})
    d = c.get("/api/tools/runs?limit=1").get_json()
    assert d["runs"][0]["provider"] == "llama"
    assert d["totals"] == {"benchmark": 2, "autotune": 1}
    assert [r["provider"] for r in c.get("/api/tools/runs").get_json()["runs"]] == \
        ["llama", "llama", "vllm"]


def test_summary_drops_nonfinite_and_nested(monkeypatch):
    _setup(monkeypatch)
    c = _client()
    r = c.post("/api/tools/runs", json={
        "tool": "benchmark", "model_id": "m",
        "gen_tps": float("inf"), "bench_tool": "llama-bench",
        "blob": "x" * 5000, "nested": {"a": 1}, "listy": [1, 2]})
    assert r.get_json()["ok"] is True
    s = c.get("/api/tools/runs").get_json()["runs"][0]["summary"]
    assert "gen_tps" not in s
    assert "nested" not in s and "listy" not in s
    assert s["bench_tool"] == "llama-bench"
    assert len(s["blob"]) == 200


def test_retention_cap_prunes_oldest(monkeypatch):
    conn = _setup(monkeypatch)
    c = _client()
    monkeypatch.setattr(manager_mod, "_TOOL_RUNS_CAP", 5)
    for i in range(8):
        c.post("/api/tools/runs", json={"tool": "benchmark", "model_id": f"m{i}"})
    n = conn.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0]
    assert n == 5
    runs = c.get("/api/tools/runs").get_json()["runs"]
    assert runs[0]["model_id"] == "m7"
    assert runs[-1]["model_id"] == "m3"


def test_limit_clamped_and_clear(monkeypatch):
    _setup(monkeypatch)
    c = _client()
    for i in range(3):
        c.post("/api/tools/runs", json={"tool": "autotune", "model_id": f"m{i}"})
    assert len(c.get("/api/tools/runs?limit=1").get_json()["runs"]) == 1
    assert len(c.get("/api/tools/runs?limit=abc").get_json()["runs"]) == 3
    assert len(c.get("/api/tools/runs?limit=0").get_json()["runs"]) == 1
    assert c.delete("/api/tools/runs").get_json()["ok"] is True
    assert c.get("/api/tools/runs").get_json()["runs"] == []


def test_prune_is_per_tool_and_latest_survives_flood(monkeypatch):
    conn = _setup(monkeypatch)
    c = _client()
    monkeypatch.setattr(manager_mod, "_TOOL_RUNS_CAP", 5)
    c.post("/api/tools/runs", json={"tool": "benchmark", "model_id": "bm",
                                    "gen_tps": 33.0})
    for i in range(8):
        c.post("/api/tools/runs", json={"tool": "autotune", "model_id": f"a{i}"})
    counts = dict(conn.execute(
        "SELECT tool, COUNT(*) FROM tool_runs GROUP BY tool"))
    assert counts == {"benchmark": 1, "autotune": 5}
    d = c.get("/api/tools/runs?limit=3").get_json()
    assert all(r["tool"] == "autotune" for r in d["runs"])
    assert d["latest"]["benchmark"]["model_id"] == "bm"
    assert d["latest"]["benchmark"]["summary"]["gen_tps"] == 33.0
    assert d["totals"]["benchmark"] == 1


def test_first_upgrade_seeds_ledger_from_model_benchmarks(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    conn.execute(
        "INSERT INTO model_benchmarks (model_id, agent_id, provider,"
        " avg_gen_tps, avg_ppt_tps, avg_pg_tps, bench_tool, switches, ts)"
        " VALUES ('old/m', 'ag', 'vllm', 40.5, 900.0, NULL, 'vllm-bench-serve',"
        " '[]', '2026-08-01T00:00:00+00:00')")
    conn.execute("DROP TABLE tool_runs")
    conn.commit()
    manager_mod.init_db()
    runs = _client().get("/api/tools/runs").get_json()["runs"]
    assert len(runs) == 1
    assert runs[0]["tool"] == "benchmark" and runs[0]["provider"] == "vllm"
    assert runs[0]["summary"]["gen_tps"] == 40.5
    assert runs[0]["summary"]["bench_tool"] == "vllm-bench-serve"
    # Re-running init_db must NOT re-seed (table already exists).
    _client().delete("/api/tools/runs")
    manager_mod.init_db()
    assert _client().get("/api/tools/runs").get_json()["runs"] == []
