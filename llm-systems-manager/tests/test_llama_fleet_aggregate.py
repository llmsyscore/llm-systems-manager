"""llama fleet rollup: GPU/mac_power, throughput, online/awake/stale paths (#697, #699)."""
from __future__ import annotations

import time

from providers.llama import _fleet_aggregate


def _wrap(sample, age_s=0.0):
    return {"sample": sample, "last_seen": time.time() - age_s}


def _llama(**kw):
    base = {"state": "awake", "model": "m1", "tokens_per_second": 10.0,
            "prompt_tokens_per_second": 5.0, "n_ctx": 8192,
            "total_tokens_generated": 100, "total_tokens_prompted": 50}
    base.update(kw)
    return base


def test_gpu_rollup_sums_power_and_takes_max_temp_vram():
    out = _fleet_aggregate({
        "a": _wrap({"llama": _llama(),
                    "gpu": {"temperature_c": 60.0, "vram_usage_percent": 40.0, "power_watts": 200.0}}),
        "b": _wrap({"llama": _llama(model="m2"),
                    "gpu": {"temperature_c": 75.0, "vram_usage_percent": 30.0, "power_watts": 150.0}}),
    })
    assert out["gpu"] == {"max_temp_c": 75.0, "max_vram_pct": 40.0,
                          "total_power_watts": 350.0, "thermal_crit_count": 0}


def test_apple_host_uses_mac_power_and_thermal_pressure():
    out = _fleet_aggregate({
        "mac": _wrap({"llama": _llama(), "gpu": {},
                      "mac_power": {"soc_total_w": 42.5, "gpu_w": 30.0,
                                    "thermal_pressure": "Serious"}}),
        "pc": _wrap({"llama": _llama(model="m2"),
                     "gpu": {"temperature_c": 50.0, "power_watts": 100.0}}),
    })
    assert out["gpu"]["total_power_watts"] == 142.5
    assert out["gpu"]["thermal_crit_count"] == 1
    assert out["gpu"]["max_temp_c"] == 50.0


def test_thermal_crit_prefers_agent_ordinal():
    out = _fleet_aggregate({
        "a": _wrap({"llama": _llama(), "mac_power": {"thermal_pressure": "Serious",
                                                    "thermal_pressure_n": 0}}),
        "b": _wrap({"llama": _llama(), "mac_power": {"thermal_pressure": "Nominal",
                                                    "thermal_pressure_n": 3}}),
    })
    assert out["gpu"]["thermal_crit_count"] == 1


def test_power_precedence_matches_energy_extract_power():
    out = _fleet_aggregate({
        "wall": _wrap({"llama": _llama(),
                       "gpu": {"power_watts": 100.0},
                       "liquidctl": {"psu": {"Estimated input power": {"value": 300.0}}}}),
    })
    assert out["gpu"]["total_power_watts"] == 300.0  # PSU wall > GPU


def test_offline_hosts_contribute_nothing_to_gpu_or_throughput():
    out = _fleet_aggregate({
        "on": _wrap({"llama": _llama(), "gpu": {"temperature_c": 60.0, "power_watts": 100.0}}),
        "off": _wrap({"llama": _llama(model="m2", tokens_per_second=99.0),
                      "gpu": {"temperature_c": 95.0, "power_watts": 500.0},
                      "mac_power": {"thermal_pressure": "Critical"}}, age_s=999),
    })
    assert out["agent_count_total"] == 2
    assert out["agent_count_online"] == 1
    assert out["gpu"] == {"max_temp_c": 60.0, "max_vram_pct": 0.0,
                          "total_power_watts": 100.0, "thermal_crit_count": 0}
    assert out["throughput"] == {"total_tps": 10.0, "total_pps": 5.0}
    assert out["active_models"] == ["m1"]


def test_throughput_totals_and_awake_count():
    out = _fleet_aggregate({
        "a": _wrap({"llama": _llama(tokens_per_second=10.0, prompt_tokens_per_second=1.0)}),
        "b": _wrap({"llama": _llama(model="m2", tokens_per_second=2.5, prompt_tokens_per_second=0.5)}),
        "c": _wrap({"llama": _llama(model="m3", state="sleeping", tokens_per_second=None,
                                  prompt_tokens_per_second=None)}),
    })
    assert out["throughput"] == {"total_tps": 12.5, "total_pps": 1.5}
    assert out["agent_count_online"] == 3
    assert out["awake_agent_count"] == 2
    assert out["active_models"] == ["m1", "m2"]  # sleeping host's model is not active
    assert out["active_model_count"] == 2


def test_offline_row_reports_stale_and_nulls():
    out = _fleet_aggregate({"x": _wrap({"llama": _llama()}, age_s=999)})
    row = out["agents"][0]
    assert row["online"] is False
    assert row["state"] == "stale"
    assert row["model"] is None and row["ctx"] is None
    assert row["total_tokens_generated"] is None
    assert row["age_s"] >= 999


def test_rows_carry_per_host_power_and_thermal_flag():
    out = _fleet_aggregate({
        "on": _wrap({"llama": _llama(), "gpu": {"power_watts": 120.0},
                     "mac_power": {"thermal_pressure": "Critical"}}),
        "off": _wrap({"llama": _llama(), "gpu": {"power_watts": 50.0}}, age_s=999),
    })
    rows = {r["agent_id"]: r for r in out["agents"]}
    assert rows["on"]["power_watts"] == 120.0 and rows["on"]["thermal_crit"] is True
    assert rows["off"]["power_watts"] is None and rows["off"]["thermal_crit"] is False


def test_online_row_carries_ctx_and_token_totals():
    out = _fleet_aggregate({"x": _wrap({"llama": _llama()})})
    row = out["agents"][0]
    assert row["state"] == "awake" and row["model"] == "m1"
    assert row["ctx"] == 8192
    assert (row["total_tokens_generated"], row["total_tokens_prompted"]) == (100, 50)


def test_empty_fleet():
    out = _fleet_aggregate({})
    assert out["agent_count_total"] == 0 and out["agents"] == []
    assert out["gpu"]["total_power_watts"] == 0.0
