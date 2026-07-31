# llm-systems-manager/tests/test_llama_state_unloaded.py
"""#478: an '(unloaded)' llama model must display as no-model, not as loaded."""
from __future__ import annotations

import time

import manager_mod as M
from providers.llama import _fleet_aggregate, clean_display_model


def _payload_for(model, state="sleeping", monkeypatch=None):
    wrapper = {"sample": {"llama": {"state": state, "model": model}},
               "last_seen": time.time()}
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda kind, aid: wrapper)
    return M._build_llama_state_payload("agent-123")


def test_clean_display_model_unloaded_is_none():
    assert clean_display_model("qwen3-8b (unloaded)") is None


def test_clean_display_model_sleeping_keeps_name():
    assert clean_display_model("qwen3-8b (sleeping)") == "qwen3-8b"


def test_clean_display_model_plain_and_junk():
    assert clean_display_model("qwen3-8b") == "qwen3-8b"
    assert clean_display_model(None) is None
    assert clean_display_model("   ") is None


def test_state_payload_unloaded_model_is_none(monkeypatch):
    payload = _payload_for("qwen3-8b (unloaded)", monkeypatch=monkeypatch)
    assert payload["state"] == "sleeping"
    assert payload["model"] is None


def test_state_payload_sleeping_model_kept(monkeypatch):
    payload = _payload_for("qwen3-8b (sleeping)", monkeypatch=monkeypatch)
    assert payload["model"] == "qwen3-8b"


def test_fleet_aggregate_unloaded_row_model_is_none():
    samples = {"a1": {"sample": {"llama": {"state": "sleeping",
                                           "model": "qwen3-8b (unloaded)"}},
                      "last_seen": time.time()}}
    agg = _fleet_aggregate(samples)
    row = agg["agents"][0]
    assert row["online"] is True
    assert row["model"] is None
    assert agg["active_models"] == []


def test_fleet_aggregate_awake_model_still_counted():
    samples = {"a1": {"sample": {"llama": {"state": "awake",
                                           "model": "qwen3-8b"}},
                      "last_seen": time.time()}}
    agg = _fleet_aggregate(samples)
    assert agg["agents"][0]["model"] == "qwen3-8b"
    assert agg["active_models"] == ["qwen3-8b"]
