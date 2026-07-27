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
    monkeypatch.setattr(rc, "_agent_sample", lambda aid: SAMPLE)
    snap = rc._snapshot_power("a" * 32)
    assert snap["psu_w"] == 412.0
    assert snap["gpus"] == [{"name": "RTX 4090", "power_w": 310.5,
                             "vram_used_mb": 20000, "vram_total_mb": 24576}]


def test_snapshot_power_without_psu(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample",
                        lambda aid: {"system": {"gpu": SAMPLE["system"]["gpu"]}})
    assert rc._snapshot_power("a" * 32)["psu_w"] is None


def test_snapshot_power_empty_sample(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample", lambda aid: {})
    snap = rc._snapshot_power("a" * 32)
    assert snap["psu_w"] is None and snap["gpus"] == []
