"""#472: desired-state validation + normalization."""
from __future__ import annotations
import pytest
import autopilot as ap

def test_minimal_entry_normalizes_defaults():
    st = ap.validate_state({"enabled": True, "entries": [
        {"model": "m1", "provider": "llama"}], "hosts": {}})
    e = st["entries"][0]
    assert e["failover"] == "semi" and e["min_replicas"] == 1
    assert e["max_replicas"] == 1 and e["placement"] == "auto"
    assert "autoscale" not in e

def test_autoscale_defaults_applied_when_range_open():
    st = ap.validate_state({"enabled": False, "entries": [
        {"model": "m1", "provider": "llama", "max_replicas": 3}], "hosts": {}})
    a = st["entries"][0]["autoscale"]
    assert a == {"target_saturation": 0.75, "up_window_s": 120,
                 "down_window_s": 900}

@pytest.mark.parametrize("bad", [
    {"model": "", "provider": "llama"},
    {"model": "m", "provider": "ollama"},
    {"model": "m", "provider": "llama", "failover": "yolo"},
    {"model": "m", "provider": "llama", "min_replicas": 0},
    {"model": "m", "provider": "llama", "min_replicas": 3, "max_replicas": 2},
])
def test_invalid_entries_rejected(bad):
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [bad], "hosts": {}})

def test_duplicate_model_provider_rejected():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [
            {"model": "m", "provider": "llama"},
            {"model": "m", "provider": "llama"}], "hosts": {}})

def test_host_policy_validated():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [],
                           "hosts": {"a" * 32: {"sleep_after_idle_min": -5}}})
