"""#468: per-provider bench URL + power sampling."""
from __future__ import annotations

import pytest

import report_card as rc

AGENT = {"agent_id": "a" * 32, "registered_from": "203.0.113.7",
         "hostname": "bench-host", "bind_url": "http://bench-host:9899",
         "token": "tok", "capabilities": {"llama": True}}


def test_llama_base_url_uses_agent_openai_passthrough():
    url, headers = rc.bench_base_url("llama", AGENT)
    # Agent route is /llama/openai/chat/completions — no /v1 segment.
    assert url.endswith("/llama/openai")
    assert headers["Authorization"] == "Bearer tok"


def test_vllm_base_url():
    url, _ = rc.bench_base_url("vllm", AGENT)
    assert url.endswith("/vllm/openai")


def test_lms_base_url_is_direct_lmstudio():
    url, headers = rc.bench_base_url("lms", AGENT)
    assert url == "http://203.0.113.7:1235/v1" and headers == {}


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        rc.bench_base_url("nope", AGENT)


def test_base_url_raises_without_callback_url():
    with pytest.raises(ValueError):
        rc.bench_base_url("llama", {"agent_id": "x", "token": "t"})


def test_power_sampler_prefers_psu_and_averages():
    samples = iter([{"psu_w": 100.0, "gpus": []},
                    {"psu_w": 300.0, "gpus": []}])
    s = rc.PowerSampler(sample_fn=lambda: next(samples), interval_s=0)
    s._tick()
    s._tick()
    out = s.stop()
    assert out["avg_watts"] == 200.0 and out["source"] == "psu"


def test_power_sampler_falls_back_to_gpu_sum():
    s = rc.PowerSampler(sample_fn=lambda: {
        "psu_w": None,
        "gpus": [{"name": "g", "power_w": 120.0, "vram_total_mb": 1,
                  "vram_used_mb": 1}]}, interval_s=0)
    s._tick()
    out = s.stop()
    assert out["avg_watts"] == 120.0 and out["source"] == "gpu"


def test_power_sampler_none_when_no_telemetry():
    s = rc.PowerSampler(sample_fn=lambda: {"psu_w": None, "gpus": []},
                        interval_s=0)
    s._tick()
    assert s.stop()["avg_watts"] is None


def test_power_sampler_survives_sampler_errors():
    def boom():
        raise RuntimeError("telemetry down")
    s = rc.PowerSampler(sample_fn=boom, interval_s=0)
    s._tick()
    assert s.stop()["avg_watts"] is None


def test_power_sampler_keeps_last_gpu_list():
    gpus = [{"name": "g", "power_w": 10.0, "vram_total_mb": 8, "vram_used_mb": 4}]
    s = rc.PowerSampler(sample_fn=lambda: {"psu_w": 50.0, "gpus": gpus},
                        interval_s=0)
    s._tick()
    assert s.stop()["gpus"] == gpus


# ── production snapshot shape ────────────────────────────────────────

SAMPLE = {"system": {
    "gpu": {"name": "RTX 4090", "power_watts": 310.5, "vram_used_mb": 20000,
            "vram_total_bytes": 25769803776},
    "liquidctl": {"psu": {"Estimated input power": {"value": 412.0,
                                                    "unit": "W"}}}}}


def test_snapshot_power_reads_psu_and_gpu(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample", lambda aid, prov=None: SAMPLE)
    snap = rc._snapshot_power("a" * 32)
    assert snap["psu_w"] == 412.0
    assert snap["gpus"] == [{"name": "RTX 4090", "power_w": 310.5,
                             "vram_used_mb": 20000, "vram_total_mb": 24576}]


def test_snapshot_power_without_psu(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample",
                        lambda aid, prov=None: {"system": {"gpu": SAMPLE["system"]["gpu"]}})
    assert rc._snapshot_power("a" * 32)["psu_w"] is None


def test_snapshot_power_empty_sample(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample", lambda aid, prov=None: {})
    snap = rc._snapshot_power("a" * 32)
    assert snap["psu_w"] is None and snap["gpus"] == []


class _FakeStore:
    def __init__(self, by_provider):
        self._by = by_provider

    def get(self, provider, agent_id):
        return self._by.get(provider)


def test_agent_sample_falls_back_to_other_providers(monkeypatch):
    # A vLLM-only host has no llama bucket, but its vllm payload carries the
    # same `system` block.
    import provider_state
    monkeypatch.setattr(provider_state, "STORE",
                        _FakeStore({"vllm": {"sample": SAMPLE}}))
    assert rc._agent_sample("a" * 32, "vllm") is SAMPLE
    assert rc._agent_sample("a" * 32) is SAMPLE


def test_agent_sample_prefers_the_runs_own_provider(monkeypatch):
    import provider_state
    other = {"system": {"gpu": {"name": "other"}}}
    monkeypatch.setattr(provider_state, "STORE",
                        _FakeStore({"llama": {"sample": other},
                                    "lms": {"sample": SAMPLE}}))
    assert rc._agent_sample("a" * 32, "lms") is SAMPLE
    assert rc._agent_sample("a" * 32, "llama") is other


def test_agent_sample_skips_payloads_without_system(monkeypatch):
    import provider_state
    monkeypatch.setattr(provider_state, "STORE",
                        _FakeStore({"llama": {"sample": {"ps": []}},
                                    "vllm": {"sample": SAMPLE}}))
    assert rc._agent_sample("a" * 32, "llama") is SAMPLE


def test_agent_sample_empty_when_nothing_reported(monkeypatch):
    import provider_state
    monkeypatch.setattr(provider_state, "STORE", _FakeStore({}))
    assert rc._agent_sample("a" * 32, "llama") == {}
