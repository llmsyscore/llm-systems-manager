"""Overall agent rows carry loaded model names for LMS (#571)."""
from __future__ import annotations

import time

from providers.lms import _fleet_aggregate


def _wrap(sample, age_s=0.0):
    return {"sample": sample, "last_seen": time.time() - age_s}


def test_agent_rows_carry_loaded_model_names():
    out = _fleet_aggregate({"a" * 32: _wrap({
        "server": {"on": True},
        "ps": [
            {"model": "qwen3-30b", "status": "IDLE"},
            {"model": "old-model", "status": "STOPPED"},
        ],
    })})
    row = out["agents"][0]
    assert row["loaded_models"] == ["qwen3-30b"]
    assert row["loaded_model_count"] == 1


def test_offline_rows_report_no_models():
    out = _fleet_aggregate({"b" * 32: _wrap({
        "ps": [{"model": "m", "status": "IDLE"}],
    }, age_s=99999)})
    assert out["agents"][0]["loaded_models"] == []


def test_ps_rows_without_model_names_are_skipped():
    out = _fleet_aggregate({"c" * 32: _wrap({
        "server": {"on": True},
        "ps": [{"status": "IDLE"}, {"model": "", "status": "IDLE"}],
    })})
    assert out["agents"][0]["loaded_models"] == []
    assert out["agents"][0]["loaded_model_count"] == 2


# --- GPU/power rollup + online-only throughput (#702, #703) ---

def test_gpu_block_reads_nested_system_gpu_and_mac_power():
    out = _fleet_aggregate({
        "pc": _wrap({"server": {"on": True}, "ps": [],
                     "system": {"gpu": {"temperature_c": 70.0, "vram_usage_percent": 55.0,
                                        "power_watts": 180.0}}}),
        "mac": _wrap({"server": {"on": True}, "ps": [], "system": {"gpu": {}},
                      "mac_power": {"soc_total_w": 25.0, "thermal_pressure": "Critical"}}),
        "off": _wrap({"system": {"gpu": {"temperature_c": 99.0, "power_watts": 900.0}}}, age_s=999),
    })
    assert out["gpu"] == {"max_temp_c": 70.0, "max_vram_pct": 55.0,
                          "total_power_watts": 205.0, "thermal_crit_count": 1}
    rows = {r["agent_id"]: r for r in out["agents"]}
    assert rows["pc"]["power_watts"] == 180.0 and rows["mac"]["power_watts"] == 25.0
    assert rows["mac"]["thermal_crit"] is True and rows["off"]["power_watts"] is None


def test_throughput_only_counts_online_agents(monkeypatch):
    import gateway_usage
    seen = {}

    def fake_rates(ids, max_age_s=60.0):
        seen["ids"] = list(ids)
        return {"total_tps": 1.0, "total_pps": 0.0}
    monkeypatch.setattr(gateway_usage, "fleet_rates", fake_rates)
    out = _fleet_aggregate({
        "on": _wrap({"ps": []}),
        "off": _wrap({"ps": []}, age_s=999),
    })
    assert seen["ids"] == ["on"]
    assert out["throughput"]["total_tps"] == 1.0
