# llm-systems-manager/tests/test_vllm_provider_spec.py
"""#125: vLLM manager ProviderSpec registration + fleet aggregator +
registry/caps wiring."""
from __future__ import annotations

import time

import providers


def test_spec_registered():
    spec = providers.get("vllm")
    assert spec is not None
    assert spec.label == "vLLM"
    assert spec.capability_key == "vllm"
    assert spec.sub_tab_keys == ("vllm",)
    assert spec.default_picker == "first_approved"
    assert spec.aggregator is not None


def _wrap(sample, age=1.0):
    return {"sample": sample, "last_seen": time.time() - age}


def test_aggregator_rollup():
    spec = providers.get("vllm")
    samples = {
        "a1": _wrap({"gpu": {"power_watts": 250.0},
                     "vllm": {"state": "running", "model": "m1",
                              "requests_running": 2, "requests_waiting": 1,
                              "kv_cache_usage_pct": 40.0, "tokens_per_second": 10.0,
                              "prompt_tokens_per_second": 5.0}}),
        "a2": _wrap({"vllm": {"state": "down"}}),
        "a3": _wrap({"vllm": {"state": "running", "model": "m2",
                              "kv_cache_usage_pct": 60.0}}, age=999),  # offline
    }
    agg = spec.aggregator(samples)
    assert agg["provider"] == "vllm"
    assert agg["agent_count_total"] == 3
    assert agg["agent_count_online"] == 2
    assert agg["server_on_count"] == 1
    assert agg["requests_running_total"] == 2
    assert agg["requests_waiting_total"] == 1
    assert agg["throughput"]["total_tps"] == 10.0
    assert agg["throughput"]["total_pps"] == 5.0
    assert agg["max_kv_cache_pct"] == 40.0
    assert agg["total_gpu_power_watts"] == 250.0
    assert agg["active_models"] == ["m1"]
    assert agg["active_model_count"] == 1
    offline_row = next(r for r in agg["agents"] if r["agent_id"] == "a3")
    assert offline_row["online"] is False
    assert offline_row["model"] is None and offline_row["server_on"] is False


def test_aggregator_empty():
    agg = providers.get("vllm").aggregator({})
    assert agg["agent_count_total"] == 0
    assert agg["agents"] == []


def test_registry_helpers_accept_vllm():
    import agent_registry
    caps = agent_registry.approved_agent_caps()
    assert "vllm" in caps and "vllm_host" in caps


def test_migration_and_default_lists_include_vllm():
    import agent_registry
    data = {"global": {"primary_vllm_id": "abc123"}}
    assert agent_registry._migrate_agents_schema(data) is True
    assert data["global"]["default_vllm_id"] == "abc123"
    defaults = agent_registry._default_for_agent(
        {"global": {"default_vllm_id": "abc123"}}, "abc123")
    assert defaults == ["vllm"]


def test_registry_driven_loops_cover_all_providers():
    import agent_registry
    caps = agent_registry.approved_agent_caps()
    for name in providers.names():
        assert name in caps and f"{name}_host" in caps
    data = {"global": {f"primary_{n}_id": f"id-{n}" for n in providers.names()}}
    agent_registry._migrate_agents_schema(data)
    for name in providers.names():
        assert data["global"][f"default_{name}_id"] == f"id-{name}"


def test_gateway_paths_generated_from_registry():
    import gateway
    assert gateway._AGENT_PATHS["llama"] == {
        "chat/completions": "/llama/openai/chat/completions",
        "completions": "/llama/openai/completions",
    }
    assert gateway._AGENT_PATHS["vllm"] == {
        "chat/completions": "/vllm/openai/chat/completions",
        "completions": "/vllm/openai/completions",
    }
    assert gateway._MODELS_PATHS == {"llama": "/llama/models", "vllm": "/vllm/models"}
    assert "lms" not in gateway._AGENT_PATHS  # lms has no gateway


def test_api_config_emits_vllm_present():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "backend" / "llm-systems-manager.py").read_text()
    assert '"vllm_present"' in src
