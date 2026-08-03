"""#470: pure extraction + summary math for the energy module."""
from __future__ import annotations

import pytest

import energy as en


# ── extract_power ────────────────────────────────────────────────────

FLAT_PSU = {"host": "box", "gpu": {"power_watts": 250.0},
            "liquidctl": {"psu": {"Estimated input power":
                                  {"value": 402.5, "unit": "W"}}}}
LMS_SHAPE = {"system": {"host": "mac", "gpu": {}, "liquidctl": {}},
             "ps": [], "hardware": {"name": "studio"}}


def test_power_prefers_psu_wall_watts():
    assert en.extract_power(FLAT_PSU) == (402.5, "psu")


def test_power_falls_back_to_gpu():
    assert en.extract_power({"gpu": {"power_watts": 199.0}}) == (199.0, "gpu")


def test_power_mac_soc_between_psu_and_gpu():
    s = {"mac_power": {"soc_total_w": 18.4}, "gpu": {"power_watts": 5.0}}
    assert en.extract_power(s) == (18.4, "mac")


def test_power_reads_nested_system_block():
    s = {"system": {"gpu": {"power_watts": 77.0}}}
    assert en.extract_power(s) == (77.0, "gpu")


def test_power_none_when_absent():
    assert en.extract_power(LMS_SHAPE) == (None, None)
    assert en.extract_power({}) == (None, None)
    assert en.extract_power({"gpu": {"power_watts": None}}) == (None, None)


def test_power_ignores_malformed_psu_value():
    s = {"liquidctl": {"psu": {"Estimated input power": "402"}},
         "gpu": {"power_watts": 100.0}}
    assert en.extract_power(s) == (100.0, "gpu")


# ── extract_busy ─────────────────────────────────────────────────────

def test_busy_on_llama_requests_processing():
    assert en.extract_busy({"llama": {"requests_processing": 1}}) is True
    assert en.extract_busy({"llama": {"requests_processing": 0}}) is False


def test_busy_on_llama_tps():
    assert en.extract_busy({"llama": {"tokens_per_second": 32.5}}) is True


def test_busy_on_vllm_running():
    assert en.extract_busy({"vllm": {"requests_running": 2}}) is True
    assert en.extract_busy({"vllm": {"tokens_per_second": 0.0}}) is False


def test_busy_on_lms_ps_status():
    assert en.extract_busy({"ps": [{"status": "GENERATING"}]}) is True
    assert en.extract_busy({"ps": [{"status": "IDLE"}]}) is False
    assert en.extract_busy({"ps": [{"status": "STOPPED"}, {}]}) is False


def test_busy_false_on_empty_sample():
    assert en.extract_busy({}) is False


# ── counters ─────────────────────────────────────────────────────────

def test_counters_read_llama_and_vllm_blocks():
    s = {"llama": {"total_tokens_generated": 100,
                   "total_tokens_prompted": 40},
         "vllm": {"total_tokens_generated": 7}}
    got = en.extract_counters(s)
    assert got["llama"] == {"gen": 100, "prompt": 40}
    assert got["vllm"] == {"gen": 7, "prompt": None}


def test_counters_empty_without_token_telemetry():
    assert en.extract_counters(LMS_SHAPE) == {}
    assert en.extract_counters({"llama": {"total_tokens_generated": None}}) == {}


def test_counter_delta_first_sight_counts_nothing():
    assert en.counter_delta(None, 500) == (0, 500)


def test_counter_delta_freezes_on_none():
    assert en.counter_delta(500, None) == (0, 500)


def test_counter_delta_normal_increase():
    assert en.counter_delta(500, 620) == (120, 620)


def test_counter_delta_reset_counts_since_restart():
    assert en.counter_delta(500, 30) == (30, 30)


# ── hostname ─────────────────────────────────────────────────────────

def test_hostname_flat_nested_and_hardware():
    assert en.extract_hostname(FLAT_PSU) == "box"
    assert en.extract_hostname(LMS_SHAPE) == "mac"
    assert en.extract_hostname({"hardware": {"name": "studio"}}) == "studio"
    assert en.extract_hostname({}) is None


# ── summarize ────────────────────────────────────────────────────────

def _row(agent="a", hour=0, observed=3600.0, active=1800.0, power=3600.0,
         wh=200.0, active_wh=150.0, gen=1_000_000, prompt=500_000,
         host="box", source="psu"):
    return {"hour_ts": hour, "agent_id": agent, "hostname": host,
            "observed_s": observed, "active_s": active, "power_s": power,
            "energy_wh": wh, "active_energy_wh": active_wh,
            "tokens_gen": gen, "tokens_prompt": prompt,
            "power_source": source, "samples": 360}


def test_summarize_totals_and_mtok():
    s = en.summarize([_row()], window_s=3600.0, price_kwh=0.20,
                     cloud_in=0.15, cloud_out=0.60)
    t = s["totals"]
    assert t["kwh"] == 0.2
    assert t["cost_usd"] == pytest.approx(0.04)
    assert t["active_kwh"] == 0.15 and t["idle_kwh"] == pytest.approx(0.05)
    assert t["active_pct"] == 50.0
    assert t["coverage_pct"] == 100.0
    assert t["avg_watts"] == 200.0
    # Fully-loaded: $0.04 for 1 Mtok generated; marginal: active share only.
    assert t["usd_per_mtok"] == pytest.approx(0.04)
    assert t["usd_per_mtok_active"] == pytest.approx(0.03)
    # Cloud: 0.5 Mtok in @0.15 + 1 Mtok out @0.60 = 0.675 → 0.67 rounded.
    assert t["cloud_cost_usd"] == pytest.approx(0.67)
    assert s["savings_usd"] == pytest.approx(0.63)


def test_summarize_per_host_breakdown_and_sort():
    rows = [_row(agent="a", wh=50.0), _row(agent="b", wh=400.0, host="big")]
    s = en.summarize(rows, 3600.0, 0.15, 0.15, 0.60)
    assert [h["agent_id"] for h in s["hosts"]] == ["b", "a"]
    assert s["hosts"][0]["hostname"] == "big"
    assert s["totals"]["kwh"] == pytest.approx(0.45)


def test_summarize_no_power_host_degrades():
    rows = [_row(power=0.0, wh=0.0, active_wh=0.0, source=None)]
    s = en.summarize(rows, 3600.0, 0.15, 0.15, 0.60)
    t = s["totals"]
    assert t["kwh"] is None and t["cost_usd"] is None
    assert t["avg_watts"] is None and t["has_power"] is False
    assert t["usd_per_mtok"] is None
    assert t["has_tokens"] is True and t["cloud_cost_usd"] > 0
    assert s["savings_usd"] is None


def test_summarize_no_tokens_host_degrades():
    rows = [_row(gen=0, prompt=0)]
    s = en.summarize(rows, 3600.0, 0.15, 0.15, 0.60)
    t = s["totals"]
    assert t["has_tokens"] is False
    assert t["usd_per_mtok"] is None and t["cloud_cost_usd"] == 0.0
    assert s["savings_usd"] is None
    assert t["kwh"] == 0.2


def test_summarize_partial_coverage():
    s = en.summarize([_row(observed=900.0)], window_s=3600.0,
                     price_kwh=0.15, cloud_in=0.15, cloud_out=0.60)
    assert s["totals"]["coverage_pct"] == 25.0


def test_fleet_coverage_scales_with_host_count():
    rows = [_row(agent="a", observed=3600.0), _row(agent="b", observed=3600.0)]
    s = en.summarize(rows, 3600.0, 0.15, 0.15, 0.60)
    assert s["totals"]["coverage_pct"] == 100.0
    rows = [_row(agent="a", observed=3600.0), _row(agent="b", observed=1800.0)]
    s = en.summarize(rows, 3600.0, 0.15, 0.15, 0.60)
    # Two hosts, one half-observed: 5400s over 2×3600s = 75%, not 100%.
    assert s["totals"]["coverage_pct"] == 75.0
    assert {h["agent_id"]: h["coverage_pct"] for h in s["hosts"]} == {
        "a": 100.0, "b": 50.0}


def test_summarize_empty_rows():
    s = en.summarize([], 3600.0, 0.15, 0.15, 0.60)
    assert s["totals"]["kwh"] is None
    assert s["hosts"] == [] and s["savings_usd"] is None


def test_summarize_zero_price_gives_zero_cost_not_none():
    s = en.summarize([_row()], 3600.0, 0.0, 0.15, 0.60)
    assert s["totals"]["cost_usd"] == 0.0
    assert s["totals"]["usd_per_mtok"] == 0.0


# ── month bounds ─────────────────────────────────────────────────────

def test_month_bounds_utc_calendar():
    start, end = en.month_bounds("2026-07")
    assert (end - start) == 31 * 86400
    start, end = en.month_bounds("2026-02")
    assert (end - start) == 28 * 86400


def test_month_bounds_rejects_garbage():
    with pytest.raises(ValueError):
        en.month_bounds("garbage")
    with pytest.raises(ValueError):
        en.month_bounds("2026-13")


# ── #496: fleet $/Mtok must compare matched hosts only ───────────────

def test_fleet_mtok_none_when_energy_and_tokens_disjoint():
    # Host a: power but no tokens; host b: tokens but no power.
    rows = [_row(agent="a", wh=100.0, active_wh=80.0, gen=0, prompt=0),
            _row(agent="b", power=0.0, wh=0.0, active_wh=0.0,
                 gen=16, prompt=0, source=None)]
    s = en.summarize(rows, 3600.0, 0.15, 0.15, 0.60)
    t = s["totals"]
    assert t["usd_per_mtok"] is None
    assert t["usd_per_mtok_active"] is None
    assert t["mtok_energy_coverage_pct"] == 0.0
    assert s["savings_usd"] is None


def test_fleet_mtok_from_matched_subset_only():
    # Host a reports both; host b is power-only and must not inflate $/Mtok.
    rows = [_row(agent="a", wh=10.0, active_wh=10.0, gen=1_000_000, prompt=0),
            _row(agent="b", wh=990.0, active_wh=0.0, gen=0, prompt=0)]
    s = en.summarize(rows, 3600.0, 0.15, 0.15, 0.60)
    t = s["totals"]
    # 10 Wh @ $0.15/kWh = $0.0015 over 1 Mtok.
    assert t["usd_per_mtok"] == pytest.approx(0.0015, abs=1e-4)
    assert t["mtok_energy_coverage_pct"] == 1.0
    assert s["savings_usd"] is None


def test_fleet_mtok_and_savings_intact_when_fully_matched():
    s = en.summarize([_row()], 3600.0, 0.20, 0.15, 0.60)
    t = s["totals"]
    assert t["usd_per_mtok"] == pytest.approx(0.04)
    assert t["usd_per_mtok_active"] == pytest.approx(0.03)
    assert t["mtok_energy_coverage_pct"] == 100.0
    assert s["savings_usd"] == pytest.approx(0.63)
