# llm-systems-manager/tests/test_bench_provider_store.py
"""#357: provider-aware benchmark store/results/delete + extra_json column."""
from __future__ import annotations

import json
import sqlite3

import manager_mod

OLD_SCHEMA = """
CREATE TABLE model_benchmarks (
    id          INTEGER PRIMARY KEY,
    model_id    TEXT NOT NULL,
    agent_id    TEXT NOT NULL DEFAULT '',
    avg_gen_tps REAL,
    avg_ppt_tps REAL,
    avg_pg_tps  REAL,
    bench_tool  TEXT,
    switches    TEXT,
    ts          TEXT,
    UNIQUE(model_id, agent_id)
)
"""


def _mem_db(schema=OLD_SCHEMA):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(schema)
    return conn


def _client():
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_init_db_adds_extra_json_idempotently(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(model_benchmarks)")]
    assert "extra_json" in cols
    manager_mod.init_db()  # second run must not raise
    cols2 = [r[1] for r in conn.execute("PRAGMA table_info(model_benchmarks)")]
    assert cols2.count("extra_json") == 1


def test_store_and_results_roundtrip_vllm_provider(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    c = _client()
    extra = {"p99_ttft_ms": 456.7, "backend": "vllm"}
    r = c.post("/api/benchmark/store", json={
        "model_id": "org/m", "provider": "vllm",
        "avg_gen_tps": 1063.9, "avg_pg_tps": 9573.2, "avg_ppt_tps": None,
        "bench_tool": "vllm-bench-serve",
        "switches": [{"flag": "--num-prompts", "value": "200"}],
        "extra_json": extra,
    })
    assert r.status_code == 200 and r.get_json()["ok"] is True
    row = conn.execute(
        "SELECT bench_tool, extra_json FROM model_benchmarks WHERE model_id='org/m'"
    ).fetchone()
    assert row[0] == "vllm-bench-serve"
    assert json.loads(row[1]) == extra

    res = c.get("/api/benchmark/results?provider=vllm")
    assert res.status_code == 200
    items = res.get_json()["results"]
    assert items[0]["model_id"] == "org/m"
    assert items[0]["extra_json"] == extra
    assert items[0]["avg_pg_tps"] == 9573.2


def test_store_unknown_provider_400(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    c = _client()
    r = c.post("/api/benchmark/store", json={"model_id": "m", "provider": "nope"})
    assert r.status_code == 400
    r2 = c.get("/api/benchmark/results?provider=nope")
    assert r2.status_code == 400


def test_results_default_provider_is_llama_shape_unchanged(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    c = _client()
    r = c.post("/api/benchmark/store", json={
        "model_id": "Qwen3-32B", "avg_gen_tps": 42.5, "avg_ppt_tps": 900.0,
        "bench_tool": "llama-bench", "switches": [],
    })
    assert r.get_json()["ok"] is True
    items = c.get("/api/benchmark/results").get_json()["results"]
    assert items[0]["avg_gen_tps"] == 42.5
    assert items[0]["extra_json"] is None


def test_provider_keyspaces_are_isolated(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    c = _client()
    c.post("/api/benchmark/store", json={"model_id": "org/m", "avg_gen_tps": 42.5})
    c.post("/api/benchmark/store", json={"model_id": "org/m", "provider": "vllm",
                                         "avg_gen_tps": 1063.9})
    assert conn.execute("SELECT COUNT(*) FROM model_benchmarks").fetchone()[0] == 2
    llama = c.get("/api/benchmark/results").get_json()["results"]
    vllm_rows = c.get("/api/benchmark/results?provider=vllm").get_json()["results"]
    assert llama[0]["avg_gen_tps"] == 42.5 and len(llama) == 1
    assert vllm_rows[0]["avg_gen_tps"] == 1063.9 and len(vllm_rows) == 1
    # provider-scoped delete leaves the other provider's row intact
    c.delete("/api/benchmark/results/org/m?provider=vllm")
    assert conn.execute(
        "SELECT provider FROM model_benchmarks").fetchall() == [("llama",)]


def test_provider_migration_tags_existing_rows_llama(monkeypatch):
    conn = _mem_db()
    conn.execute("INSERT INTO model_benchmarks (model_id, agent_id, avg_gen_tps) "
                 "VALUES ('old/m', '', 7.5)")
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    row = conn.execute(
        "SELECT provider, avg_gen_tps FROM model_benchmarks WHERE model_id='old/m'"
    ).fetchone()
    assert row == ("llama", 7.5)
    manager_mod.init_db()  # idempotent


def test_delete_with_provider(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    c = _client()
    c.post("/api/benchmark/store", json={"model_id": "org/m", "provider": "vllm"})
    r = c.delete("/api/benchmark/results/org/m?provider=vllm")
    assert r.get_json()["ok"] is True
    assert conn.execute("SELECT COUNT(*) FROM model_benchmarks").fetchone()[0] == 0


def test_vllm_bench_routes_registered():
    rules = {str(r) for r in manager_mod.app.url_map.iter_rules()}
    for path in ["/api/vllm/bench/run", "/api/vllm/bench/stream",
                 "/api/vllm/bench/cancel"]:
        assert path in rules, f"missing route {path}"


def test_vllm_bench_run_proxies_to_vllm(monkeypatch):
    calls = {}

    def fake_proxy(kind, method, path, **kw):
        calls.update(kind=kind, method=method, path=path)
        return {"ok": True}

    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    r = _client().post("/api/vllm/bench/run", json={"switches": []})
    assert r.status_code == 200
    assert calls == {"kind": "vllm", "method": "POST", "path": "/vllm/bench/run"}
