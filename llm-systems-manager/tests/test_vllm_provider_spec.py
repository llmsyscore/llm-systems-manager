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
        "a1": _wrap({"vllm": {"state": "running", "model": "m1",
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
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "backend" / "agent_registry.py").read_text()
    assert '("llama", "lms", "vllm")' in src            # migration tuple + set-primary allowlist
    assert '["llama", "lms", "vllm"]' in src            # _default_for_agent default list


def test_api_config_emits_vllm_present():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "backend" / "llm-systems-manager.py").read_text()
    assert '"vllm_present"' in src
