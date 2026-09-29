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


def test_autotune_routes_refuse_vllm_but_benchmark_routes_proxy_it(monkeypatch):
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
    assert manager_mod.AUTOTUNE_PROVIDERS == ("llama", "lms")
    r = c.get("/api/llm/autotune/preflight?provider=vllm")
    assert r.status_code == 400 and "has no autotune tools" in r.get_json()["error"]
    assert c.post("/api/benchmark/cancel?provider=vllm").status_code == 200
    assert calls == [("vllm", "/vllm/bench/cancel")]
