"""#468: timing/energy/aggregation math."""
from __future__ import annotations

import pytest

import report_card as rc


def test_rep_metrics_computes_throughputs():
    m = rc.rep_metrics({"ttft_s": 0.5, "prompt_tokens": 512,
                        "gen_tokens": 128, "gen_duration_s": 4.0})
    assert m["prefill_tps"] == pytest.approx(1024.0)
    assert m["gen_tps"] == pytest.approx(32.0)


def test_rep_metrics_zero_durations_do_not_divide_by_zero():
    m = rc.rep_metrics({"ttft_s": 0.0, "prompt_tokens": 512,
                        "gen_tokens": 0, "gen_duration_s": 0.0})
    assert m["prefill_tps"] == 0.0 and m["gen_tps"] == 0.0


def test_run_metrics_takes_medians():
    reps = [{"ttft_s": t, "prompt_tokens": 512, "gen_tokens": 128,
             "gen_duration_s": d} for t, d in ((0.4, 4.0), (0.5, 2.0), (0.9, 8.0))]
    m = rc.run_metrics(reps)
    assert m["ttft_s"] == pytest.approx(0.5)
    assert m["gen_tps"] == pytest.approx(32.0)  # medians of 32, 64, 16


def test_energy_metrics_happy_path():
    e = rc.energy_metrics(avg_watts=200.0, gen_tps=40.0, price_kwh=0.15)
    assert e["tokens_per_joule"] == pytest.approx(0.2)
    # (200/1000 * 0.15) $/h / (40*3600 tok/h) * 1e6 = 0.2083 $/Mtok
    assert e["usd_per_mtok"] == pytest.approx(0.2083, abs=1e-3)


def test_energy_metrics_none_without_power():
    e = rc.energy_metrics(None, 40.0, 0.15)
    assert e["tokens_per_joule"] is None and e["usd_per_mtok"] is None


def test_energy_metrics_none_without_throughput():
    e = rc.energy_metrics(200.0, 0.0, 0.15)
    assert e["tokens_per_joule"] is None and e["usd_per_mtok"] is None


def test_aggregate_gpus_sums_and_labels():
    g = rc.aggregate_gpus([
        {"name": "RTX 4090", "vram_total_mb": 24576, "vram_used_mb": 20000,
         "power_w": 350.0},
        {"name": "RTX 4090", "vram_total_mb": 24576, "vram_used_mb": 18000,
         "power_w": 340.0}])
    assert g["gpu_config"] == "2× RTX 4090"
    assert g["vram_total_mb"] == 49152 and g["power_w"] == pytest.approx(690.0)


def test_aggregate_single_gpu_plain_label():
    g = rc.aggregate_gpus([{"name": "7900 XTX", "vram_total_mb": 24560,
                            "vram_used_mb": 100, "power_w": None}])
    assert g["gpu_config"] == "7900 XTX" and g["power_w"] is None


def test_aggregate_mixed_names_lists_both():
    g = rc.aggregate_gpus([
        {"name": "RTX 4090", "vram_total_mb": 24576, "vram_used_mb": 1,
         "power_w": 300.0},
        {"name": "RTX 3090", "vram_total_mb": 24576, "vram_used_mb": 1,
         "power_w": 250.0}])
    assert g["gpu_config"] == "RTX 3090 + RTX 4090"
    assert g["power_w"] == pytest.approx(550.0)


def test_aggregate_empty_is_all_none():
    g = rc.aggregate_gpus([])
    assert g["gpu_config"] is None and g["power_w"] is None
    assert g["vram_total_mb"] is None and g["vram_used_mb"] is None
