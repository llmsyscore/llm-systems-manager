"""#468: per-provider bench URL + power sampling."""
from __future__ import annotations

import pytest

import report_card as rc

AGENT = {"agent_id": "a" * 32, "registered_from": "203.0.113.7",
         "hostname": "bench-host", "bind_url": "http://bench-host:9899",
         "token": "tok", "capabilities": {"llama": True}}


def test_llama_base_url_uses_agent_openai_passthrough():
    url, headers = rc.bench_base_url("llama", AGENT, probe=lambda u: True)
    # Agent route is /llama/openai/chat/completions — no /v1 segment.
    assert url.endswith("/llama/openai")
    assert headers["Authorization"] == "Bearer tok"


def test_vllm_base_url():
    url, _ = rc.bench_base_url("vllm", AGENT, probe=lambda u: True)
    assert url.endswith("/vllm/openai")


def test_base_url_skips_an_unreachable_hostname_bind():
    # bind_url (hostname) is listed first; the manager often can't resolve
    # it and must fall through to the registered_from IP candidate.
    url, _ = rc.bench_base_url("llama", AGENT,
                               probe=lambda u: "203.0.113.7" in u)
    assert "203.0.113.7" in url and url.endswith("/llama/openai")


def test_base_url_raises_when_no_candidate_answers():
    import pytest as _pytest
    with _pytest.raises(ValueError, match="not reachable"):
        rc.bench_base_url("llama", AGENT, probe=lambda u: False)


def test_lms_base_url_is_direct_lmstudio():
    url, headers = rc.bench_base_url("lms", AGENT)
    assert url == "http://203.0.113.7:1235/v1" and headers == {}


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        rc.bench_base_url("nope", AGENT)


def test_base_url_raises_without_callback_url():
    with pytest.raises(ValueError):
        rc.bench_base_url("llama", {"agent_id": "x", "token": "t"})


def test_power_sampler_averages_within_one_source():
    samples = iter([{"watts": 100.0, "source": "psu", "gpus": []},
                    {"watts": 300.0, "source": "psu", "gpus": []}])
    s = rc.PowerSampler(sample_fn=lambda: next(samples), interval_s=0)
    s._tick()
    s._tick()
    out = s.stop()
    assert out["avg_watts"] == 200.0 and out["source"] == "psu"


def test_power_sampler_reports_gpu_watts():
    s = rc.PowerSampler(sample_fn=lambda: {
        "watts": 120.0, "source": "gpu",
        "gpus": [{"name": "g", "power_w": 120.0, "vram_total_mb": 1,
                  "vram_used_mb": 1}]}, interval_s=0)
    s._tick()
    out = s.stop()
    assert out["avg_watts"] == 120.0 and out["source"] == "gpu"


def test_power_sampler_reports_apple_soc_watts():
    s = rc.PowerSampler(sample_fn=lambda: {"watts": 38.5, "source": "mac",
                                           "gpus": []}, interval_s=0)
    s._tick()
    out = s.stop()
    assert out["avg_watts"] == 38.5 and out["source"] == "mac"


def test_power_sampler_never_averages_across_sources():
    # A PSU read that drops out mid-window must not be blended with the GPU
    # readings that replaced it; wall watts win and keep their own average.
    samples = iter([{"watts": 400.0, "source": "psu", "gpus": []},
                    {"watts": 300.0, "source": "gpu", "gpus": []},
                    {"watts": 420.0, "source": "psu", "gpus": []},
                    {"watts": 300.0, "source": "gpu", "gpus": []}])
    s = rc.PowerSampler(sample_fn=lambda: next(samples), interval_s=0)
    for _ in range(3):
        s._tick()
    out = s.stop()
    assert out["source"] == "psu" and out["avg_watts"] == 410.0


def test_power_sampler_none_when_no_telemetry():
    s = rc.PowerSampler(sample_fn=lambda: {"watts": None, "source": None,
                                           "gpus": []}, interval_s=0)
    s._tick()
    out = s.stop()
    assert out["avg_watts"] is None and out["source"] is None


def test_power_sampler_survives_sampler_errors():
    def boom():
        raise RuntimeError("telemetry down")
    s = rc.PowerSampler(sample_fn=boom, interval_s=0)
    s._tick()
    assert s.stop()["avg_watts"] is None


def test_power_sampler_reset_discards_warmup_samples():
    samples = iter([{"watts": 60.0, "source": "psu", "gpus": []},   # idle
                    {"watts": 200.0, "source": "psu", "gpus": []},  # warmup
                    {"watts": 210.0, "source": "psu", "gpus": []},  # reps
                    {"watts": 210.0, "source": "psu", "gpus": []}])  # final
    s = rc.PowerSampler(sample_fn=lambda: next(samples), interval_s=0)
    s._tick()
    s._tick()
    s.reset()
    s._tick()
    out = s.stop()
    assert out["avg_watts"] == 210.0 and out["source"] == "psu"


def test_power_sampler_keeps_last_gpu_list():
    gpus = [{"name": "g", "power_w": 10.0, "vram_total_mb": 8, "vram_used_mb": 4}]
    s = rc.PowerSampler(sample_fn=lambda: {"watts": 50.0, "source": "psu",
                                           "gpus": gpus}, interval_s=0)
    s._tick()
    assert s.stop()["gpus"] == gpus


def test_power_sampler_keeps_the_gpu_row_when_power_is_unmeasured():
    # VRAM/name still populate the card even with no wattage anywhere.
    gpus = [{"name": "g", "power_w": None, "vram_total_mb": 8,
             "vram_used_mb": 4}]
    s = rc.PowerSampler(sample_fn=lambda: {"watts": None, "source": None,
                                           "gpus": gpus}, interval_s=0)
    s._tick()
    out = s.stop()
    assert out["gpus"] == gpus and out["avg_watts"] is None


# ── production snapshot shape ────────────────────────────────────────

GPU = {"name": "RTX 4090", "power_watts": 310.5, "vram_used_mb": 20000,
       "vram_total_bytes": 25769803776}
PSU = {"psu": {"Estimated input power": {"value": 412.0, "unit": "W"}}}

# vllm/lms nest the host fields under "system".
SAMPLE = {"system": {"gpu": GPU, "liquidctl": PSU}}

# llama pushes the same host fields flat, alongside its own provider block.
FLAT_SAMPLE = {"gpu": GPU, "liquidctl": PSU, "host": "bench-host",
               "llama": {"requests_processing": 1}}

# Apple Silicon on llama: no PSU and no discrete GPU, SoC watts sit flat.
MAC_SAMPLE = {"mac_power": {"soc_total_w": 38.5}, "host": "studio",
              "llama": {"requests_processing": 1}}

# The LM Studio push nests `system` but keeps mac_power as its sibling.
LMS_MAC_SAMPLE = {"system": {"host": "studio"}, "ps": [],
                  "mac_power": {"soc_total_w": 41.0},
                  "hardware": {"name": "Mac Studio"}}

GPU_ROW = [{"name": "RTX 4090", "power_w": 310.5, "vram_used_mb": 20000,
            "vram_total_mb": 24576}]


def test_snapshot_power_reads_psu_and_gpu(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample", lambda aid, prov=None: SAMPLE)
    snap = rc._snapshot_power("a" * 32)
    assert snap["watts"] == 412.0 and snap["source"] == "psu"
    assert snap["gpus"] == GPU_ROW


def test_snapshot_power_reads_a_flat_llama_sample(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample", lambda aid, prov=None: FLAT_SAMPLE)
    snap = rc._snapshot_power("a" * 32)
    assert snap["watts"] == 412.0 and snap["source"] == "psu"
    assert snap["gpus"] == GPU_ROW


def test_snapshot_power_reads_apple_soc_watts(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample", lambda aid, prov=None: MAC_SAMPLE)
    snap = rc._snapshot_power("a" * 32)
    assert snap["watts"] == 38.5 and snap["source"] == "mac"
    assert snap["gpus"] == []


def test_snapshot_power_reads_soc_watts_beside_a_nested_system(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample",
                        lambda aid, prov=None: LMS_MAC_SAMPLE)
    snap = rc._snapshot_power("a" * 32)
    assert snap["watts"] == 41.0 and snap["source"] == "mac"


def test_snapshot_power_without_psu(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample", lambda aid, prov=None: {"gpu": GPU})
    snap = rc._snapshot_power("a" * 32)
    assert snap["watts"] == 310.5 and snap["source"] == "gpu"


def test_snapshot_power_empty_sample(monkeypatch):
    monkeypatch.setattr(rc, "_agent_sample", lambda aid, prov=None: {})
    snap = rc._snapshot_power("a" * 32)
    assert snap["watts"] is None and snap["source"] is None
    assert snap["gpus"] == []


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


def test_agent_sample_skips_payloads_without_telemetry(monkeypatch):
    import provider_state
    monkeypatch.setattr(provider_state, "STORE",
                        _FakeStore({"llama": {"sample": {"ps": []}},
                                    "vllm": {"sample": SAMPLE}}))
    assert rc._agent_sample("a" * 32, "llama") is SAMPLE


def test_agent_sample_accepts_a_flat_llama_payload(monkeypatch):
    # A llama-only host never sends a "system" key; its telemetry is flat.
    import provider_state
    monkeypatch.setattr(provider_state, "STORE",
                        _FakeStore({"llama": {"sample": FLAT_SAMPLE}}))
    assert rc._agent_sample("a" * 32, "llama") is FLAT_SAMPLE
    assert rc._agent_sample("a" * 32) is FLAT_SAMPLE


def test_agent_sample_accepts_a_flat_apple_soc_payload(monkeypatch):
    import provider_state
    monkeypatch.setattr(provider_state, "STORE",
                        _FakeStore({"llama": {"sample": MAC_SAMPLE}}))
    assert rc._agent_sample("a" * 32, "llama") is MAC_SAMPLE


def test_agent_sample_skips_a_bucket_whose_telemetry_is_unusable(monkeypatch):
    # liquidctl present but reporting no wall watts, and no GPU: not usable.
    import provider_state
    empty_psu = {"liquidctl": {"psu": {}}, "llama": {}}
    monkeypatch.setattr(provider_state, "STORE",
                        _FakeStore({"llama": {"sample": empty_psu},
                                    "vllm": {"sample": SAMPLE}}))
    assert rc._agent_sample("a" * 32, "llama") is SAMPLE


def test_agent_sample_empty_when_nothing_reported(monkeypatch):
    import provider_state
    monkeypatch.setattr(provider_state, "STORE", _FakeStore({}))
    assert rc._agent_sample("a" * 32, "llama") == {}
