"""#472: desired-state validation + normalization."""
from __future__ import annotations
import pytest
import agent_registry
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
    {"model": "m", "provider": "llama", "placement": ["x"]},
    {"model": "m", "provider": "llama", "placement": {"a": 1}},
    {"model": "m", "provider": "llama", "placement": 5},
    {"model": "m", "provider": "llama", "placement": ""},
    {"model": "m", "provider": "llama", "min_replicas": ["x"]},
    {"model": "m", "provider": "llama", "max_replicas": ["x"]},
    {"model": "m", "provider": "llama", "priority": ["x"]},
    {"model": "m", "provider": "llama", "min_replicas": {"a": 1}},
])
def test_invalid_entries_rejected(bad):
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [bad], "hosts": {}})


def test_host_bad_numeric_type_rejected():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [],
                           "hosts": {"a" * 32: {"sleep_after_idle_min": ["x"]}}})

def test_duplicate_model_provider_rejected():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [
            {"model": "m", "provider": "llama"},
            {"model": "m", "provider": "llama"}], "hosts": {}})

def test_host_policy_validated():
    with pytest.raises(ValueError):
        ap.validate_state({"enabled": True, "entries": [],
                           "hosts": {"a" * 32: {"sleep_after_idle_min": -5}}})


def _patch_registry(monkeypatch):
    """Fake registry store standing in for agents.json, wired the same way
    load_agents()/save_agents() read/write it — set_state's writes are
    visible to the next get_state() call, same as the real lock/save path."""
    store = {"agents": {}, "global": {}}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: store)
    def _save(data):
        store["global"] = data.get("global", {})
        store["agents"] = data.get("agents", {})
    monkeypatch.setattr(agent_registry, "save_agents", _save)
    return store


def test_get_state_default_when_no_autopilot_key(monkeypatch):
    _patch_registry(monkeypatch)
    assert ap.get_state() == {"enabled": False, "entries": [], "hosts": {}}


def test_get_state_default_mutation_does_not_leak(monkeypatch):
    _patch_registry(monkeypatch)
    first = ap.get_state()
    first["entries"].append({"model": "phantom", "provider": "llama"})
    first["hosts"]["x"] = {"sleep_after_idle_min": 5}
    second = ap.get_state()
    assert second == {"enabled": False, "entries": [], "hosts": {}}


def test_set_state_get_state_roundtrip(monkeypatch):
    store = _patch_registry(monkeypatch)
    st = ap.validate_state({"enabled": True, "entries": [
        {"model": "m1", "provider": "llama"}], "hosts": {}})
    ap.set_state(st)
    assert store["global"]["autopilot"] == st
    assert ap.get_state() == st
