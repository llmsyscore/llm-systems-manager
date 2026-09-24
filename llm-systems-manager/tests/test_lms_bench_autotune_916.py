"""#916: live-bench + autotune routes take a provider; LM Studio load preferences store and merge."""
from __future__ import annotations

import pytest

import bench_live as bl
import lms_load_prefs
import manager_mod

A_LLAMA = {"agent_id": "a" * 32, "token": "tok-llama"}
A_LMS = {"agent_id": "b" * 32, "token": "tok-lms"}
TOK = {a["token"]: a for a in (A_LLAMA, A_LMS)}


def _doc(run_id, model, provider=None, pred=50.0):
    d = {"run_id": run_id, "model_id": model, "ok": True, "bench": "throughput_1k",
         "config": {"bench": "throughput_1k", "concurrency": [1]},
         "levels": [{"level": 1, "concurrency": 1, "wall_s": 5.0, "rows": [],
                     "all": {"pred_tps": pred, "prompt_tps": 900.0, "latency_s": 3.0, "accept_rate": None,
                             "agg_pred_tps": pred, "completion_tokens": 100}}]}
    if provider:
        d["provider"] = provider
    return d


@pytest.fixture
def app(tmp_path):
    from flask import Flask
    app = Flask(__name__)
    calls, started = [], []
    hosts = {"llama": [{"agent_id": A_LLAMA["agent_id"], "hostname": "gpu-01", "online": True, "model": "org/m:Q4",
                        "state": "awake", "provider": "llama"}],
             "lms": [{"agent_id": A_LMS["agent_id"], "hostname": "mac-01", "online": True, "model": "qwen3.5-9b@q6_k",
                      "models": ["qwen3.5-9b@q6_k", "gemma-3-4b"], "state": "awake", "provider": "lms"}]}

    def proxy(kind, method, path, **kw):
        calls.append((kind, method, path, kw.get("json")))
        return {"ok": True, "proxied": path}

    def fleet_hosts(provider=None):
        return hosts["llama"] + hosts["lms"] if provider is None else list(hosts.get(provider) or [])

    def run_on_agent(aid, body, provider="llama"):
        started.append((aid, provider, body))
        return True, f"run-{provider}"

    bl.register_routes(app, None, db_path=str(tmp_path / "t.db"), proxy=proxy,
                       agent_by_token=lambda t: TOK.get(t),
                       request_agent=lambda p: A_LMS if p == "lms" else A_LLAMA,
                       note_tool_start=lambda p, t: (p, t), fleet_hosts=fleet_hosts,
                       run_on_agent=run_on_agent, cancel_on_agent=lambda aid, provider="llama": True,
                       llama_build_of=lambda aid: "b999", valid_provider=lambda p: p in ("llama", "lms", "vllm"))
    c = app.test_client()
    c.calls, c.started = calls, started
    return c


def test_preflight_setup_run_proxy_to_the_picked_provider(app):
    app.get("/api/benchmark/live/preflight")
    app.get("/api/benchmark/live/preflight?provider=lms")
    app.post("/api/benchmark/live/setup?provider=lms", json={"prefetch": ["throughput_1k"]})
    app.post("/api/benchmark/live/run", json={"provider": "lms", "model_id": "qwen3.5-9b@q6_k", "bench": "throughput_1k"})
    assert [(k, p) for k, _m, p, _j in app.calls] == [
        ("llama", "/llama/bench/live/preflight"), ("lms", "/lms/bench/live/preflight"),
        ("lms", "/lms/bench/live/setup"), ("lms", "/lms/bench/live/run")]
    assert "provider" not in app.calls[-1][3]          # the body's provider is routing, not run config


def test_unknown_or_unbenchable_provider_is_refused(app):
    assert app.get("/api/benchmark/live/preflight?provider=nope").status_code == 400
    assert app.get("/api/benchmark/live/preflight?provider=vllm").status_code == 400
    assert app.get("/api/benchmark/live/hosts?model_id=x&provider=vllm").status_code == 400


def test_hosts_and_fleet_follow_the_provider(app):
    r = app.get("/api/benchmark/live/hosts?model_id=gemma-3-4b&provider=lms").get_json()
    assert r["provider"] == "lms" and r["hosts"] == [
        {"agent_id": A_LMS["agent_id"], "hostname": "mac-01", "online": True, "loaded": True, "state": "awake", "provider": "lms"}]
    r = app.get("/api/benchmark/live/hosts?model_id=gemma-3-4b").get_json()
    assert [h["loaded"] for h in r["hosts"]] == [False]
    j = app.post("/api/benchmark/live/fleet", json={"provider": "lms", "model_id": "qwen3.5-9b@q6_k",
                                                    "config": {"bench": "throughput_1k", "provider": "lms"}}).get_json()
    assert j["ok"]
    import time
    for _ in range(50):
        if app.started:
            break
        time.sleep(0.02)
    assert app.started and app.started[0][1] == "lms" and "provider" not in app.started[0][2]
    job = app.get(f"/api/benchmark/live/fleet/{j['job_id']}").get_json()["job"]
    assert job["provider"] == "lms"


def test_store_keeps_the_provider_and_build_only_for_llama(app):
    h_lms = {"Authorization": f"Bearer {A_LMS['token']}"}
    h_ll = {"Authorization": f"Bearer {A_LLAMA['token']}"}
    assert app.post("/api/benchmark/live/store", json=_doc("r-lms", "qwen3.5-9b@q6_k", "lms"), headers=h_lms).get_json()["stored"]
    assert app.post("/api/benchmark/live/store", json=_doc("r-ll", "org/m:Q4"), headers=h_ll).get_json()["stored"]
    assert app.post("/api/benchmark/live/store", json=_doc("r-bad", "org/m:Q4", "weird"), headers=h_ll).get_json()["stored"]
    lms_runs = app.get("/api/benchmark/live/runs?provider=lms").get_json()["runs"]
    assert [r["run_id"] for r in lms_runs] == ["r-lms"] and lms_runs[0]["provider"] == "lms" and lms_runs[0]["llama_build"] == ""
    ll_runs = app.get("/api/benchmark/live/runs").get_json()["runs"]
    assert {r["run_id"]: r["provider"] for r in ll_runs} == {"r-ll": "llama", "r-bad": "llama"}
    assert all(r["llama_build"] == "b999" for r in ll_runs)
    speed = app.get("/api/benchmark/live/speed?model_id=qwen3.5-9b@q6_k").get_json()["hosts"]
    assert speed[0]["hostname"] == "mac-01" and speed[0]["provider"] == "lms"
    latest = app.get("/api/benchmark/live/latest?provider=lms").get_json()
    assert latest["agent_id"] == A_LMS["agent_id"] and "qwen3.5-9b@q6_k" in latest["models"]


def test_old_databases_gain_the_provider_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE bench_live_runs (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, model_id TEXT NOT NULL,"
                 " agent_id TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 1, baseline INTEGER NOT NULL DEFAULT 0,"
                 " gen_tps REAL, ppt_tps REAL, latency_s REAL, accept_rate REAL, wh_per_ktok REAL, config_json TEXT NOT NULL,"
                 " result_json TEXT NOT NULL)")
    conn.execute("INSERT INTO bench_live_runs (run_id, model_id, agent_id, ts, config_json, result_json) VALUES ('r0','m','a','t','{}','{}')")
    conn.commit()
    bl.init_table(conn)
    meta, _doc_ = bl.read_run(conn, "r0")
    assert meta["provider"] == "llama" and meta["llama_build"] == ""


# ── manager tool routes ──

def _client(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    manager_mod.app.config["TESTING"] = True
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_autotune_and_benchmark_routes_take_a_provider(monkeypatch):
    calls = []

    def fake_proxy(kind, method, path, **kw):
        calls.append((kind, method, path, (kw.get("json") or {}).get("provider", "absent")))
        return manager_mod.jsonify({"ok": True})
    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    c = _client(monkeypatch)
    c.get("/api/llm/autotune/preflight")
    c.get("/api/llm/autotune/preflight?provider=lms")
    c.post("/api/llm/autotune/run?provider=lms", json={"model_ids": ["m"], "provider": "lms"})
    c.post("/api/llm/autotune/cancel?provider=lms")
    c.post("/api/benchmark/cancel?provider=lms")
    assert calls == [("llama", "GET", "/llama/autotune/preflight", "absent"), ("lms", "GET", "/lms/autotune/preflight", "absent"),
                     ("lms", "POST", "/lms/autotune/run", "absent"), ("lms", "POST", "/lms/autotune/cancel", "absent"),
                     ("lms", "POST", "/lms/bench/cancel", "absent")]
    assert c.get("/api/llm/autotune/preflight?provider=vllm").status_code == 400
    assert c.get("/api/llm/autotune/preflight?provider=bogus").status_code == 400
    assert c.get("/api/benchmark/stream?provider=bogus").status_code == 400


def test_load_prefs_clean_config_types():
    clean, err = lms_load_prefs.clean_config({"context_length": "32768", "flash_attention": "true", "parallel": 2,
                                              "speculative_draft_min_continue_probability": "0.75",
                                              "speculative_draft_model": " qwen-0.8b ", "eval_batch_size": None})
    assert err is None and clean == {"context_length": 32768, "flash_attention": True, "parallel": 2,
                                     "speculative_draft_min_continue_probability": 0.75, "speculative_draft_model": "qwen-0.8b"}
    assert lms_load_prefs.clean_config({"gpu_layers": 3})[1].startswith("unknown load option")
    assert lms_load_prefs.clean_config({"flash_attention": "maybe"})[1].startswith("invalid value")
    assert lms_load_prefs.clean_config({"parallel": True})[1].startswith("invalid value")


def test_load_prefs_routes_and_load_merge(monkeypatch, tmp_path):
    store = lms_load_prefs.PrefStore(tmp_path / "prefs.json")
    monkeypatch.setattr(lms_load_prefs, "STORE", store)
    monkeypatch.setattr(manager_mod.agent_registry, "default_agent_id_for", lambda p: "lms-agent-1")
    monkeypatch.setattr(manager_mod.agent_registry, "pinned_agent", lambda p, m: None)
    loads = []

    def fake_proxy(kind, method, path, **kw):
        loads.append((kind, path, kw.get("json")))
        return manager_mod.jsonify({"ok": True})
    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    c = _client(monkeypatch)
    r = c.put("/api/lmstudio/load-prefs", json={"model": "qwen3.5-9b@q6_k", "config": {"flash_attention": True, "parallel": "2"}, "note": "tuned"})
    assert r.status_code == 200 and r.get_json()["prefs"]["config"] == {"flash_attention": True, "parallel": 2}
    assert c.put("/api/lmstudio/load-prefs", json={"model": "m", "config": {"threads": 4}}).status_code == 400
    assert c.put("/api/lmstudio/load-prefs", json={"model": "m", "config": {}}).status_code == 400
    got = c.get("/api/lmstudio/load-prefs?model=qwen3.5-9b@q6_k").get_json()
    assert got["agent_id"] == "lms-agent-1" and got["prefs"]["note"] == "tuned"
    assert "qwen3.5-9b@q6_k" in c.get("/api/lmstudio/load-prefs").get_json()["models"]
    # A load without those keys picks the saved ones up; an explicit key wins.
    c.post("/api/lmstudio/load", json={"model": "qwen3.5-9b@q6_k"})
    c.post("/api/lmstudio/load", json={"model": "qwen3.5-9b@q6_k", "parallel": 4})
    c.post("/api/lmstudio/load", json={"model": "other"})
    assert loads[0][2] == {"flash_attention": True, "parallel": 2, "model": "qwen3.5-9b@q6_k"}
    assert loads[1][2] == {"flash_attention": True, "parallel": 4, "model": "qwen3.5-9b@q6_k"}
    assert loads[2][2] == {"model": "other"}
    # The picker's agent scopes the record; a different agent has none.
    assert c.get("/api/lmstudio/load-prefs?model=qwen3.5-9b@q6_k&agent=lms-agent-2").get_json()["prefs"] is None
    deleted = c.delete("/api/lmstudio/load-prefs?model=qwen3.5-9b@q6_k").get_json()
    assert deleted["deleted"] is True
    assert c.get("/api/lmstudio/load-prefs?model=qwen3.5-9b@q6_k").get_json()["prefs"] is None


def test_fleet_hosts_lists_lm_studio_loaded_ids(monkeypatch):
    agents = {"agents": {
        "L1": {"status": "approved", "hostname": "gpu-01", "capabilities": {"llama": True}},
        "M1": {"status": "approved", "hostname": "mac-01", "capabilities": {"lms": True}},
        "M2": {"status": "pending", "hostname": "mac-02", "capabilities": {"lms": True}},
    }}
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents", lambda: agents)
    import time as _t
    now = _t.time()
    samples = {("llama", "L1"): {"last_seen": now, "sample": {"llama": {"model": "org/m:Q4", "state": "awake"}}},
               ("lms", "M1"): {"last_seen": now, "sample": {"ps": [{"identifier": "qwen3.5-9b@q6_k", "status": "IDLE"},
                                                                     {"identifier": "gemma-3-4b", "status": "IDLE"}]}}}
    monkeypatch.setattr(manager_mod.provider_state.STORE, "get", lambda p, a: samples.get((p, a)))
    rows = manager_mod._fleet_hosts("lms")
    assert len(rows) == 1 and rows[0]["models"] == ["qwen3.5-9b@q6_k", "gemma-3-4b"] and rows[0]["state"] == "awake"
    both = manager_mod._fleet_hosts(None)
    assert [r["provider"] for r in both] == ["llama", "lms"]


def test_lms_load_and_unload_follow_the_agent_that_holds_the_model(monkeypatch, tmp_path):
    """#1118: an unpinned model goes to the agent serving it, then one listing it, else the primary."""
    monkeypatch.setattr(lms_load_prefs, "STORE", lms_load_prefs.PrefStore(tmp_path / "prefs.json"))
    monkeypatch.setattr(manager_mod.agent_registry, "default_agent_id_for", lambda p: "lms-agent-1")
    monkeypatch.setattr(manager_mod.agent_registry, "pinned_agent", lambda p, m: {"agent_id": "lms-pin"} if m == "pinned-model" else None)
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents", lambda: {"agents": {
        "lms-agent-1": {"status": "approved"}, "lms-agent-2": {"status": "approved"}, "lms-gone": {"status": "disabled"}}})
    serving = {"lms:qwen3-8b": {"lms-agent-2"}, "lms:stale": {"lms-gone"}}
    catalog = {"lms:qwen3-8b": {"lms-agent-1", "lms-agent-2"}, "lms:listed-only": {"lms-agent-2"}, "lms:stale": {"lms-gone"}}
    monkeypatch.setattr(manager_mod.gateway, "_serving_agent_ids", lambda p, m: set(serving.get(f"{p}:{m}") or ()))
    monkeypatch.setattr(manager_mod.gateway, "_catalog_agent_ids", lambda p, m: set(catalog.get(f"{p}:{m}") or ()))
    calls = []

    def fake_proxy(kind, method, path, **kw):
        calls.append((path, kw.get("agent_id")))
        return manager_mod.jsonify({"ok": True})
    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    c = _client(monkeypatch)
    c.post("/api/lmstudio/unload", json={"model": "qwen3-8b"})
    c.post("/api/lmstudio/load", json={"model": "qwen3-8b"})
    c.post("/api/lmstudio/load", json={"model": "listed-only"})
    c.post("/api/lmstudio/unload", json={"model": "stale"})
    c.post("/api/lmstudio/unload", json={"model": "unknown"})
    c.post("/api/lmstudio/unload", json={"model": "pinned-model"})
    c.post("/api/lmstudio/unload?agent=lms-agent-1", json={"model": "qwen3-8b"})
    assert calls == [("/lms/unload", "lms-agent-2"), ("/lms/load", "lms-agent-2"), ("/lms/load", "lms-agent-2"),
                     ("/lms/unload", "lms-agent-1"), ("/lms/unload", "lms-agent-1"), ("/lms/unload", "lms-pin"),
                     ("/lms/unload", "lms-agent-1")]
