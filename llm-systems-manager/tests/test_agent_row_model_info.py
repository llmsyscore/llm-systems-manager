"""Overall agent rows carry loaded-model details — ctx + token totals (#593)."""
from __future__ import annotations

import time

from providers.llama import _fleet_aggregate as llama_agg
from providers.lms import _fleet_aggregate as lms_agg
from providers.vllm import _fleet_aggregate as vllm_agg
import gateway_usage


def _wrap(sample, age_s=0.0):
    return {"sample": sample, "last_seen": time.time() - age_s}


def test_llama_rows_carry_ctx_and_totals():
    out = llama_agg({"a" * 32: _wrap({
        "llama": {"state": "awake", "model": "qwen3-30b", "n_ctx": 32768,
                  "total_tokens_generated": 1234567,
                  "total_tokens_prompted": 89012},
    })})
    row = out["agents"][0]
    assert row["ctx"] == 32768
    assert row["total_tokens_generated"] == 1234567
    assert row["total_tokens_prompted"] == 89012


def test_llama_rows_null_guard_old_agents():
    out = llama_agg({"b" * 32: _wrap({
        "llama": {"state": "awake", "model": "m"},
    })})
    row = out["agents"][0]
    assert row["ctx"] is None
    assert row["total_tokens_generated"] is None


def test_vllm_rows_carry_ctx_and_totals():
    out = vllm_agg({"c" * 32: _wrap({
        "vllm": {"state": "running", "model": "nemotron",
                 "max_model_len": 131072,
                 "total_tokens_generated": 555, "total_tokens_prompted": 44},
    })})
    row = out["agents"][0]
    assert row["ctx"] == 131072
    assert row["total_tokens_generated"] == 555
    assert row["total_tokens_prompted"] == 44


def test_vllm_ctx_absent_when_server_down():
    out = vllm_agg({"d" * 32: _wrap({
        "vllm": {"state": "down", "max_model_len": 131072},
    })})
    assert out["agents"][0]["ctx"] is None


def test_lms_rows_carry_max_active_ctx_and_gateway_totals():
    aid = "e" * 32
    gateway_usage.record(aid, 300, 900)
    try:
        out = lms_agg({aid: _wrap({
            "server": {"on": True},
            "ps": [
                {"model": "small", "status": "IDLE", "context": 8192},
                {"model": "big", "status": "IDLE", "context": 32768},
                {"model": "stopped", "status": "STOPPED", "context": 999999},
            ],
        })})
        row = out["agents"][0]
        assert row["ctx"] == 32768
        assert row["total_tokens_generated"] == 900
        assert row["total_tokens_prompted"] == 300
    finally:
        gateway_usage._counters.pop(aid, None)


def test_lms_rows_without_context_or_usage_stay_null():
    out = lms_agg({"f" * 32: _wrap({
        "server": {"on": True},
        "ps": [{"model": "m", "status": "IDLE"}],
    })})
    row = out["agents"][0]
    assert row["ctx"] is None
    assert row["total_tokens_generated"] is None
