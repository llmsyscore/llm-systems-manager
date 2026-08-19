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
