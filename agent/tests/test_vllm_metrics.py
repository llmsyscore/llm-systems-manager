# agent/tests/test_vllm_metrics.py
"""#125: vLLM Prometheus parsing + field derivation (pure helpers, no server)."""
from __future__ import annotations

from tests._vllm_load import load_vllm

vllm = load_vllm()

SAMPLE = """# HELP vllm:num_requests_running Number of requests currently running.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="meta-llama/Llama-3-8B"} 2.0
vllm:num_requests_waiting{model_name="meta-llama/Llama-3-8B"} 1.0
vllm:gpu_cache_usage_perc{model_name="meta-llama/Llama-3-8B"} 0.42
vllm:prompt_tokens_total{model_name="meta-llama/Llama-3-8B"} 1000.0
vllm:generation_tokens_total{model_name="meta-llama/Llama-3-8B"} 5000.0
"""


def test_parse_prom_families_sums_and_skips_comments():
    fams = vllm._parse_prom_families(SAMPLE)
    assert fams["vllm:num_requests_running"] == [2.0]
    assert fams["vllm:generation_tokens_total"] == [5000.0]
    assert not any(k.startswith("#") for k in fams)


def test_parse_prom_families_ignores_trailing_timestamp():
    fams = vllm._parse_prom_families(
        'vllm:num_requests_running{m="x"} 2.0 1712345678901\n'
        "vllm:bare_metric 3.0 1712345678901\n")
    assert fams["vllm:num_requests_running"] == [2.0]
    assert fams["vllm:bare_metric"] == [3.0]


def test_parse_prom_families_drops_non_finite():
    fams = vllm._parse_prom_families("vllm:x 1.0\nvllm:y +Inf\nvllm:z NaN\n")
    assert fams["vllm:x"] == [1.0]
    assert "vllm:y" not in fams and "vllm:z" not in fams


def test_derive_fields_and_counter_rates():
    fams = vllm._parse_prom_families(SAMPLE)
    prev = {"mono": 0.0, "gen": 4000.0, "prompt": 500.0}
    out = vllm._derive_vllm_fields(fams, prev, now_mono=10.0)
    assert out["requests_running"] == 2
    assert out["requests_waiting"] == 1
    assert out["kv_cache_usage_pct"] == 42.0
    assert out["tokens_per_second"] == 100.0        # (5000-4000)/10
    assert out["prompt_tokens_per_second"] == 50.0  # (1000-500)/10
    assert out["total_tokens_generated"] == 5000
    assert out["total_tokens_prompted"] == 1000


def test_derive_fields_first_sample_has_no_rates():
    fams = vllm._parse_prom_families(SAMPLE)
    out = vllm._derive_vllm_fields(fams, None, now_mono=10.0)
    assert out["tokens_per_second"] is None
    assert out["prompt_tokens_per_second"] is None


def test_counter_reset_yields_none_rates():
    fams = vllm._parse_prom_families(SAMPLE)
    prev = {"mono": 0.0, "gen": 9000.0, "prompt": 2000.0}  # counters went backwards
    out = vllm._derive_vllm_fields(fams, prev, now_mono=10.0)
    assert out["tokens_per_second"] is None


def test_kv_cache_new_family_name_supported():
    fams = vllm._parse_prom_families('vllm:kv_cache_usage_perc{m="x"} 0.5\n')
    out = vllm._derive_vllm_fields(fams, None, now_mono=1.0)
    assert out["kv_cache_usage_pct"] == 50.0


def test_agent_core_wiring_present():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "llm-systems-agent.py").read_text()
    assert "collect_vllm_for_metrics" in src
    assert "_push_vllm_payload" in src
    assert '"provider": "vllm"' in src
