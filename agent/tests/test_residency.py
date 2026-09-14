"""#966: pure residency model — aggregation, reconcile, projection, SSE deltas."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_PY = _AGENT_ROOT / "providers" / "residency.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load("residency", _PY)
T = 1000.0


def _m(provider, mid, status):
    return R.model_entry(provider, mid, status, T)


# ── llama_models_from_api ────────────────────────────────────────────

def test_router_statuses_map_and_props_sleeping_overrides_loaded():
    data = [{"id": "a", "status": {"value": "loaded"}},
            {"id": "b", "status": {"value": "loaded"}},
            {"id": "c", "status": {"value": "unloaded"}},
            {"id": "d", "status": {"value": "loading"}}]
    out = R.llama_models_from_api(data, {"a": True, "b": False}, T)
    assert {m["model_id"]: m["status"] for m in out} == {
        "a": "sleeping", "b": "loaded", "c": "unloaded", "d": "loading"}
    assert all(m["provider"] == "llama" and m["ts"] == T for m in out)


def test_single_model_build_without_status_is_loaded_unless_props_says_sleeping():
    data = [{"id": "solo"}]
    assert R.llama_models_from_api(data, {}, T)[0]["status"] == "loaded"
    assert R.llama_models_from_api(data, {"solo": True}, T)[0]["status"] == "sleeping"


def test_unknown_status_values_become_unknown_not_crash():
    out = R.llama_models_from_api([{"id": "x", "status": {"value": "weird"}}], {}, T)
    assert out[0]["status"] == "unknown"


# ── vllm / lms builders ──────────────────────────────────────────────

def test_vllm_models_up_and_down():
    models, server = R.vllm_models_from_api(["m1", "m2"], T)
    assert server == "up" and [m["status"] for m in models] == ["loaded", "loaded"]
    assert R.vllm_models_from_api([], T) == ([], "up")
    assert R.vllm_models_from_api(None, T) == ([], "down")


def test_lms_models_from_ps_rows():
    rows = [{"identifier": "i1", "model": "q/m", "status": "IDLE"}]
    models, server = R.lms_models_from_ps(rows, True, T)
    assert models[0]["model_id"] == "q/m" and models[0]["status"] == "loaded"
    assert server == "up"
    assert R.lms_models_from_ps(None, None, T) == ([], "unknown")
    assert R.lms_models_from_ps([], False, T) == ([], "down")


# ── aggregate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("models,servers,expect", [
    ([_m("llama", "a", "loaded")], {"llama": "up"}, "active"),
    ([_m("llama", "a", "sleeping"), _m("lms", "b", "loaded")], {"llama": "up", "lms": "up"}, "active"),
    ([_m("llama", "a", "loading")], {"llama": "up"}, "loading"),
    ([_m("llama", "a", "downloading")], {"llama": "up"}, "loading"),
    ([_m("llama", "a", "sleeping")], {"llama": "up"}, "sleeping"),
    ([_m("llama", "a", "sleeping"), _m("llama", "b", "loading")], {"llama": "up"}, "loading"),
    ([_m("llama", "a", "unloaded")], {"llama": "up"}, "idle"),
    ([], {"llama": "up", "vllm": "down"}, "idle"),
    ([], {"llama": "down", "vllm": "down"}, "off"),
    ([], {"llama": "unknown"}, "unknown"),
    ([_m("lms", "b", "unloaded")], {"llama": "unknown", "lms": "up"}, "unknown"),
    ([_m("lms", "b", "loaded")], {"llama": "unknown", "lms": "up"}, "active"),
    ([], {}, "unknown"),
])
def test_aggregate_table(models, servers, expect):
    assert R.aggregate(models, servers) == expect


# ── reconcile / unknown ticks ────────────────────────────────────────

def test_reconcile_builds_host_residency():
    res = R.reconcile(None, models=[_m("llama", "a", "loaded")], servers={"llama": "up"}, ts=T)
    assert res["aggregate"] == "active" and res["ts"] == T and res["source"] == "reconciled"
    assert res["unknown_ticks"] == 0 and res["servers"] == {"llama": "up"}
    assert res["models"][0]["model_id"] == "a"


def test_unknown_ticks_count_and_reset():
    res = R.reconcile(None, models=[], servers={"llama": "unknown"}, ts=T)
    assert res["aggregate"] == "unknown" and res["unknown_ticks"] == 1
    res = R.reconcile(res, models=[], servers={"llama": "unknown"}, ts=T + 5)
    assert res["unknown_ticks"] == 2
    res = R.reconcile(res, models=[], servers={"llama": "up"}, ts=T + 10)
    assert res["aggregate"] == "idle" and res["unknown_ticks"] == 0


def test_desired_profile_table():
    def res(agg, ticks=0):
        return {"aggregate": agg, "unknown_ticks": ticks}
    assert R.desired_profile(res("active")) == "performance"
    assert R.desired_profile(res("loading")) == "performance"
    assert R.desired_profile(res("sleeping")) == "powersave"
    assert R.desired_profile(res("idle")) == "powersave"
    assert R.desired_profile(res("off")) == "powersave"
    assert R.desired_profile(res("unknown", 5)) == "hold"
    assert R.desired_profile(res("unknown", 6)) == "powersave"
    assert R.desired_profile(None) == "hold"


def test_legacy_projection():
    assert R.legacy_state({"aggregate": "active"}) == "awake"
    assert R.legacy_state({"aggregate": "loading"}) == "awake"
    assert R.legacy_state({"aggregate": "idle"}) == "awake"
    assert R.legacy_state({"aggregate": "sleeping"}) == "sleeping"
    assert R.legacy_state({"aggregate": "off"}) == "unknown"
    assert R.legacy_state({"aggregate": "unknown"}) == "unknown"
    assert R.legacy_state(None) == "unknown"


# ── SSE delta ────────────────────────────────────────────────────────

def test_sse_delta_updates_one_model_and_reaggregates():
    res = R.reconcile(None, models=[_m("llama", "a", "loaded"), _m("llama", "b", "unloaded")],
                      servers={"llama": "up"}, ts=T)
    res = R.apply_sse_delta(res, "a", "sleeping", T + 1)
    assert res["aggregate"] == "sleeping" and res["source"] == "sse"
    assert [m["status"] for m in res["models"] if m["model_id"] == "a"] == ["sleeping"]
    res = R.apply_sse_delta(res, "new", "loading", T + 2)
    assert res["aggregate"] == "loading"
    assert any(m["model_id"] == "new" for m in res["models"])


def test_sse_delta_on_empty_state_creates_llama_entry():
    res = R.apply_sse_delta(None, "a", "loaded", T)
    assert res["aggregate"] == "active" and res["servers"]["llama"] == "up"


def test_sse_delta_ignores_unknown_status():
    res = R.reconcile(None, models=[_m("llama", "a", "loaded")], servers={"llama": "up"}, ts=T)
    same = R.apply_sse_delta(res, "a", None, T + 1)
    assert same["aggregate"] == "active"


def test_changed_models_lists_status_flips():
    prev = R.reconcile(None, models=[_m("llama", "a", "loaded")], servers={"llama": "up"}, ts=T)
    cur = R.reconcile(prev, models=[_m("llama", "a", "sleeping"), _m("lms", "b", "loaded")],
                      servers={"llama": "up", "lms": "up"}, ts=T + 5)
    assert sorted(R.changed_models(prev, cur)) == ["llama:a", "lms:b"]
    assert R.changed_models(None, cur) == sorted(["llama:a", "lms:b"])


def test_provider_subset_projects_one_provider():
    res = R.reconcile(None, models=[_m("llama", "a", "sleeping"), _m("lms", "b", "loaded")],
                      servers={"llama": "up", "lms": "up"}, ts=T)
    assert res["aggregate"] == "active"
    sub = R.provider_subset(res, "llama")
    assert sub["aggregate"] == "sleeping" and sub["servers"] == {"llama": "up"}
    assert [m["model_id"] for m in sub["models"]] == ["a"]
    assert R.legacy_state(sub) == "sleeping"
    down = R.reconcile(None, models=[_m("lms", "b", "loaded")], servers={"llama": "down", "lms": "up"}, ts=T)
    assert R.provider_subset(down, "llama")["aggregate"] == "off"
    assert R.provider_subset(None, "llama") is None
