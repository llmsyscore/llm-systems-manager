"""#894: vLLM is a bench provider; fleet hosts list the served model."""
import time

import bench_live
import manager_mod


def test_vllm_is_a_bench_provider():
    assert bench_live.BENCH_PROVIDERS == ("llama", "lms", "vllm")


def test_fleet_hosts_lists_vllm_served_model(monkeypatch):
    agents = {"agents": {
        "L1": {"status": "approved", "hostname": "gpu-01", "capabilities": {"llama": True}},
        "M1": {"status": "approved", "hostname": "mac-01", "capabilities": {"lms": True}},
        "V1": {"status": "approved", "hostname": "vllm-01", "capabilities": {"vllm": True}},
        "V2": {"status": "approved", "hostname": "vllm-02", "capabilities": {"vllm": True}},
    }}
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents", lambda: agents)
    now = time.time()
    samples = {("llama", "L1"): {"last_seen": now, "sample": {"llama": {"model": "org/m:Q4", "state": "awake"}}},
               ("lms", "M1"): {"last_seen": now, "sample": {"ps": [{"identifier": "qwen3.5-9b@q6_k", "status": "IDLE"}]}},
               ("vllm", "V1"): {"last_seen": now, "sample": {"vllm": {"state": "running", "model": "Qwen/Qwen3-0.6B", "models": ["Qwen/Qwen3-0.6B"]}}},
               ("vllm", "V2"): {"last_seen": now, "sample": {"vllm": {"state": "down", "model": None, "models": []}}}}
    monkeypatch.setattr(manager_mod.provider_state.STORE, "get", lambda p, a: samples.get((p, a)))
    rows = manager_mod._fleet_hosts("vllm")
    assert [(r["agent_id"], r["model"], r["models"], r["state"], r["online"]) for r in rows] == [
        ("V1", "Qwen/Qwen3-0.6B", ["Qwen/Qwen3-0.6B"], "awake", True), ("V2", None, [], "idle", True)]
    marked = bench_live.hosts_for(rows, "Qwen/Qwen3-0.6B")
    assert [(h["agent_id"], h["loaded"], h["provider"]) for h in marked] == [("V1", True, "vllm"), ("V2", False, "vllm")]
    assert [r["provider"] for r in manager_mod._fleet_hosts(None)] == ["llama", "lms", "vllm", "vllm"]


def test_autotune_and_benchmark_routes_proxy_vllm(monkeypatch):
    calls = []

    def fake_proxy(kind, method, path, **kw):
        calls.append((kind, path))
        return manager_mod.jsonify({"ok": True})
    monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    manager_mod.app.config["TESTING"] = True
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    assert manager_mod.AUTOTUNE_PROVIDERS == ("llama", "lms", "vllm")
    assert c.get("/api/llm/autotune/preflight?provider=vllm").status_code == 200
    assert c.post("/api/llm/autotune/cancel?provider=vllm").status_code == 200
    assert c.post("/api/benchmark/cancel?provider=vllm").status_code == 200
    assert calls == [("vllm", "/vllm/autotune/preflight"), ("vllm", "/vllm/autotune/cancel"), ("vllm", "/vllm/bench/cancel")]


def test_benchmark_run_dispatches_offline_runner_by_provider(monkeypatch):
    starts = []
    monkeypatch.setattr(manager_mod, "_tool_start", lambda provider, tool, path, cancel, body, timeout=15, pre=None:
                        starts.append((provider, tool, path, cancel, body, pre)) or manager_mod.jsonify({"ok": True, "run_id": "r1"}))
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    manager_mod.app.config["TESTING"] = True
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    assert c.post("/api/benchmark/run?provider=vllm", json={"model": "org/m", "switches": [{"flag": "--num-prompts", "value": "20"}]}).status_code == 200
    assert c.post("/api/benchmark/run", json={"models": ["org/m"]}).status_code == 200
    r = c.post("/api/benchmark/run?provider=lms", json={})
    assert r.status_code == 400 and "no offline benchmark" in r.get_json()["error"]
    assert c.post("/api/benchmark/run?provider=bogus", json={}).status_code == 400
    assert starts == [("vllm", "benchmark", "/vllm/bench/run", "/vllm/bench/cancel", {"model": "org/m", "switches": [{"flag": "--num-prompts", "value": "20"}]}, None),
                      ("llama", "benchmark", "/llama/bench/run", "/llama/bench/cancel", {"models": ["org/m"]}, "/llama/server/stop")]


def test_vllm_autotune_overlay_routes_are_gone():
    rules = {str(r) for r in manager_mod.app.url_map.iter_rules()}
    assert not any(r.startswith("/api/vllm/autotune/") for r in rules)
    assert {"/api/vllm/bench/run", "/api/vllm/bench/stream", "/api/vllm/bench/cancel"} <= rules
