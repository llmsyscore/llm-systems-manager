"""#966: llama collector feeds residency; state/heartbeat read one derivation; no state file."""
from __future__ import annotations

import importlib.util
import re
import sys
import threading
import types
from pathlib import Path

import pytest

from tests.test_autotune_v2_routes import _load_llama

_AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"
_SRC = _AGENT_PY.read_text()


def _exec_fn(name, ns):
    """Exec one top-level function from llm-systems-agent.py (unimportable: hyphenated name)."""
    m = re.search(rf"^def {name}\(.*?(?=^\S)", _SRC, re.MULTILINE | re.DOTALL)
    assert m, name
    exec(compile(m.group(0), str(_AGENT_PY), "exec"), ns)
    return ns


@pytest.fixture(scope="module")
def llama():
    mod = sys.modules.get("providers.llama") or _load_llama()
    # The shared loader stubs providers.llama_sse out; these tests need the real leaf.
    mod.llama_sse = _load_real_llama_sse()
    return mod


def _load_real_llama_sse():
    path = _AGENT_PY.parent / "providers" / "llama_sse.py"
    spec = importlib.util.spec_from_file_location("providers.llama_sse", path)
    real = importlib.util.module_from_spec(spec)
    sys.modules["providers.llama_sse"] = real
    spec.loader.exec_module(real)
    return real


class _Resp:
    def __init__(self, body, ok=True, text=""):
        self._b, self.ok, self.text, self.status_code = body, ok, text, 200 if ok else 500

    def json(self):
        return self._b


class _Ctx:
    def __init__(self):
        self.config = types.SimpleNamespace(
            LLAMA_ENABLED=True, LLAMA_API_URL="http://127.0.0.1:9931", POLL_INTERVAL_S=5,
            LLAMA_SYSTEMD_UNIT="llama-server", PERF_CONTROLLER_ENABLED=False,
            PERF_TARGET_AWAKE="performance", PERF_TARGET_SLEEP="powersave", LLAMA_BUILD_METHOD="")
        self.state = {"token": "", "agent_id": ""}
        import threading
        self.runtime_lock = threading.RLock()
        self.now_iso = lambda: "now"

    def check_bearer(self, *a, **k):
        return None


def _wire(llama, monkeypatch, models_body, props_by_model):
    ctx = _Ctx()
    calls = []

    def _get(url, **kw):
        calls.append((url, kw.get("params")))
        if url.endswith("/v1/models"):
            return _Resp(models_body)
        if url.endswith("/props"):
            mid = (kw.get("params") or {}).get("model")
            return _Resp(props_by_model.get(mid, {}))
        return _Resp({}, ok=False)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama.requests, "get", _get, raising=False)
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: 1)
    llama._llama_info_last_poll = 0.0
    llama._llama_info_cache = {}
    return ctx, calls


def test_collector_emits_residency_inputs_and_uses_autoload_zero(llama, monkeypatch):
    body = {"data": [{"id": "a", "status": {"value": "loaded"}}, {"id": "b", "status": {"value": "unloaded"}}]}
    ctx, calls = _wire(llama, monkeypatch, body, {"a": {"is_sleeping": False}})
    sample = llama.collect_llama_for_metrics()
    models, server = llama.residency_inputs()
    assert server == "up"
    assert {m["model_id"]: m["status"] for m in models} == {"a": "loaded", "b": "unloaded"}
    props_calls = [p for (u, p) in calls if u.endswith("/props")]
    assert props_calls == [{"model": "a", "autoload": "0"}]
    assert sample["model"] == "a"


def test_sleeping_model_skips_metrics_and_slots_on_the_same_tick(llama, monkeypatch):
    body = {"data": [{"id": "a", "status": {"value": "loaded"}}]}
    ctx, calls = _wire(llama, monkeypatch, body, {"a": {"is_sleeping": True}})
    sample = llama.collect_llama_for_metrics()
    assert sample["sleeping"] is True and sample["tokens_per_second"] == 0
    assert not any(u.endswith("/metrics") or u.endswith("/slots") for (u, _) in calls)
    models, _ = llama.residency_inputs()
    assert models[0]["status"] == "sleeping"


def test_unreachable_server_reports_down_not_stale_awake(llama, monkeypatch):
    ctx = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: None)

    def _boom(url, **kw):
        raise ConnectionError("refused")
    monkeypatch.setattr(llama.requests, "get", _boom, raising=False)
    llama._llama_info_last_poll = 0.0
    sample = llama.collect_llama_for_metrics()
    models, server = llama.residency_inputs()
    assert server == "down" and models == [] and sample["state"] == "unknown"


def test_llama_get_state_projects_ctx_residency(llama, monkeypatch):
    ctx = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    assert llama.llama_get_state() == "unknown"
    ctx.state["residency"] = {"aggregate": "sleeping", "servers": {"llama": "up"},
                              "models": [{"provider": "llama", "model_id": "a",
                                          "status": "sleeping", "ts": 1}]}
    assert llama.llama_get_state() == "sleeping"
    ctx.state["residency"] = {"aggregate": "idle", "servers": {"llama": "up"}, "models": []}
    assert llama.llama_get_state() == "awake"


def test_state_endpoint_carries_residency_and_power(llama, monkeypatch):
    ctx = _Ctx()
    host = {"aggregate": "active", "ts": 5.0, "source": "reconciled",
            "unknown_ticks": 0, "unknown_ticks_max": 6,
            "models": [{"provider": "llama", "model_id": "a", "status": "loaded", "ts": 5.0},
                       {"provider": "vllm", "model_id": "v", "status": "loaded", "ts": 5.0}],
            "servers": {"llama": "up", "vllm": "up"}}
    ctx.state["residency"] = host
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    out = llama.llama_state_endpoint(None)
    assert out["state"] == "awake" and out["residency"]["aggregate"] == "active"
    assert "power" in out and "mode" in out["power"]
    assert "perf_last_transition" in out


def test_state_endpoint_residency_is_the_llama_subset_and_host_residency_is_whole(llama, monkeypatch):
    ctx = _Ctx()
    host = {"aggregate": "active", "ts": 5.0, "source": "reconciled",
            "unknown_ticks": 0, "unknown_ticks_max": 6,
            "models": [{"provider": "llama", "model_id": "a", "status": "sleeping", "ts": 5.0},
                       {"provider": "vllm", "model_id": "v", "status": "loaded", "ts": 5.0}],
            "servers": {"llama": "up", "vllm": "up"}}
    ctx.state["residency"] = host
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    out = llama.llama_state_endpoint(None)
    sub = out["residency"]
    assert [m["model_id"] for m in sub["models"]] == ["a"]
    assert sub["servers"] == {"llama": "up"} and sub["aggregate"] == "sleeping"
    assert out["host_residency"] is host and host["aggregate"] == "active"


def test_state_endpoint_without_residency_yet(llama, monkeypatch):
    ctx = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    out = llama.llama_state_endpoint(None)
    assert out["residency"] is None and out["host_residency"] is None


def test_reconcile_now_is_coalesced_and_invalidates_the_probe_cache(llama, monkeypatch):
    hits = []
    llama.set_reconcile_hook(lambda: hits.append(1))
    llama._llama_info_last_poll = 12345.0
    llama._reconcile_mark["last"] = 0.0
    llama.reconcile_now()
    llama.reconcile_now()
    assert hits == [1] and llama._llama_info_last_poll == 0.0


def test_perf_controller_symbols_are_gone(llama):
    for name in ("perf_controller_loop", "_perf_process_line", "_perf_switch", "llama_write_state_file",
                 "llama_read_state_file", "_llama_port_open", "_push_llama_state_to_manager",
                 "_llama_sse_authoritative", "_perf_run_unit", "_perf_mode_set", "_perf_mode_state"):
        assert not hasattr(llama, name), name


def test_awake_model_probes_metrics_and_slots_with_autoload_zero(llama, monkeypatch):
    body = {"data": [{"id": "a", "status": {"value": "loaded"}}]}
    ctx, calls = _wire(llama, monkeypatch, body, {"a": {"is_sleeping": False}})
    llama._llama_info_last_active_ts = llama.time.time()
    llama.collect_llama_for_metrics()
    for suffix in ("/metrics", "/slots"):
        params = [p for (u, p) in calls if u.endswith(suffix)]
        assert params == [{"model": "a", "autoload": "0"}], suffix


def test_props_without_is_sleeping_is_a_pre_sleep_build_and_is_polled(llama, monkeypatch):
    """No is_sleeping key at all means the build predates sleep, so it cannot be woken."""
    body = {"data": [{"id": "a", "status": {"value": "loaded"}}]}
    ctx, calls = _wire(llama, monkeypatch, body, {"a": {"build_info": "b1"}})
    llama._llama_info_last_active_ts = llama.time.time()
    llama.collect_llama_for_metrics()
    assert [p for (u, p) in calls if u.endswith("/metrics")] == [{"model": "a", "autoload": "0"}]
    models, _ = llama.residency_inputs()
    assert models[0]["status"] == "loaded"


def test_malformed_is_sleeping_still_skips_metrics_and_slots(llama, monkeypatch):
    """The key is present but unreadable — treat it as 'might be asleep', not as a pre-sleep build."""
    body = {"data": [{"id": "a", "status": {"value": "loaded"}}]}
    ctx, calls = _wire(llama, monkeypatch, body, {"a": {"is_sleeping": "no"}})
    llama._llama_info_last_active_ts = llama.time.time()
    llama.collect_llama_for_metrics()
    assert not any(u.endswith("/metrics") or u.endswith("/slots") for (u, _) in calls)


def test_failed_props_skips_metrics_and_slots(llama, monkeypatch):
    ctx = _Ctx()
    calls = []

    def _get(url, **kw):
        calls.append((url, kw.get("params")))
        if url.endswith("/v1/models"):
            return _Resp({"data": [{"id": "a", "status": {"value": "loaded"}}]})
        return _Resp({}, ok=False)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama.requests, "get", _get, raising=False)
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: 1)
    llama._llama_info_last_poll = 0.0
    llama._llama_info_cache = {}
    llama._llama_info_last_active_ts = llama.time.time()
    llama.collect_llama_for_metrics()
    assert any(u.endswith("/props") for (u, _) in calls)
    assert not any(u.endswith("/metrics") or u.endswith("/slots") for (u, _) in calls)


def test_props_bind_to_the_reported_model_not_the_first_probed(llama, monkeypatch):
    body = {"data": [{"id": "a", "status": {"value": "loaded"}},
                     {"id": "b", "status": {"value": "loaded"}}]}
    _wire(llama, monkeypatch, body,
          {"a": {"is_sleeping": True, "total_slots": 1},
           "b": {"is_sleeping": False, "total_slots": 8}})
    sample = llama.collect_llama_for_metrics()
    assert sample["model"] == "b"
    assert sample["is_sleeping"] is False and sample["sleeping"] is False
    assert sample["total_slots"] == 8
    models, _ = llama.residency_inputs()
    assert {m["model_id"]: m["status"] for m in models} == {"a": "sleeping", "b": "loaded"}


def test_collector_returns_a_copy_so_the_cache_stays_clean(llama, monkeypatch):
    body = {"data": [{"id": "a", "status": {"value": "loaded"}}]}
    _wire(llama, monkeypatch, body, {"a": {"is_sleeping": False}})
    sample = llama.collect_llama_for_metrics()
    sample["residency"] = {"injected": True}
    assert "residency" not in llama._llama_info_cache


# ── #966: the reconcile runs on every tick, not only when publishing ──

def _tick_ns(collection_enabled, approved, state=None):
    ns = {
        "CONFIG": types.SimpleNamespace(
            COLLECTION_ENABLED=collection_enabled, LLAMA_ENABLED=True, VLLM_ENABLED=False,
            LMS_ENABLED=False, OPENCLAW_ENABLED=False, POLL_INTERVAL_S=5,
            POWER_UNKNOWN_TICKS=6, LLAMA_API_URL="http://127.0.0.1:9931",
            AGENT_DESCRIPTION="", AGENT_HOSTNAME="h"),
        "_collector_wake": threading.Event(),
        "_is_approved": lambda: approved,
        "_runtime_lock": threading.RLock(),
        "_state": state if state is not None else {},
        "_metric_client": None,
        "_log_hb": {"last": 0.0},
        "logger": __import__("logging").getLogger("test"),
        "time": __import__("time"),
        "types": types,
        "Any": object,
        "_build_metric_sample": lambda: {"built": True},
        "_push_dashboard_payload": lambda s: None,
        "_push_host_payload": lambda s: None,
        "_push_vllm_payload": lambda s: None,
        "llama_get_state": lambda: "awake",
        "best_effort": None,
    }
    return ns


def test_tick_reconciles_even_when_collection_is_disabled():
    ns = _tick_ns(collection_enabled=False, approved=True)
    probed = []
    ns["_probe_and_reconcile"] = lambda: probed.append(1) or ns["_state"].update(
        {"residency": {"aggregate": "sleeping"}})
    _exec_fn("_collector_tick", ns)
    ns["_collector_wake"].set()
    ns["_collector_tick"]()
    assert probed == [1]
    assert ns["_state"]["residency"] == {"aggregate": "sleeping"}
    assert ns["_state"].get("last_metric_sample") is None
    assert not ns["_collector_wake"].is_set()


def test_tick_reconciles_even_when_the_agent_is_unapproved():
    ns = _tick_ns(collection_enabled=True, approved=False)
    probed = []
    ns["_probe_and_reconcile"] = lambda: probed.append(1) or ns["_state"].update(
        {"residency": {"aggregate": "idle"}})
    _exec_fn("_collector_tick", ns)
    ns["_collector_tick"]()
    assert probed == [1] and ns["_state"]["residency"] == {"aggregate": "idle"}


def test_tick_clears_the_wake_event_before_probing_not_after():
    ns = _tick_ns(collection_enabled=False, approved=True)
    seen = []
    ns["_probe_and_reconcile"] = lambda: seen.append(ns["_collector_wake"].is_set())
    _exec_fn("_collector_tick", ns)
    ns["_collector_wake"].set()
    ns["_collector_tick"]()
    assert seen == [False]


def test_collector_loop_waits_on_the_event_never_sleeps():
    m = re.search(r"^def collector_loop\(.*?(?=^\S)", _SRC, re.MULTILINE | re.DOTALL)
    assert m
    assert "_collector_wake.wait(CONFIG.POLL_INTERVAL_S)" in m.group(0)
    assert "time.sleep" not in m.group(0)


def test_sse_status_applies_delta_and_triggers_reconcile(llama, monkeypatch):
    ctx = _Ctx()
    ctx.state["residency"] = {"models": [{"provider": "llama", "model_id": "a", "status": "loaded", "ts": 1}],
                              "servers": {"llama": "up"}, "aggregate": "active", "unknown_ticks": 0,
                              "unknown_ticks_max": 6}
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    hits = []
    llama.set_reconcile_hook(lambda: hits.append(1))
    llama._reconcile_mark["last"] = 0.0
    llama._llama_sse_apply_status({"model": "a", "status": "sleeping"})
    assert ctx.state["residency"]["aggregate"] == "sleeping"
    assert ctx.state["residency"]["source"] == "sse" and hits == [1]
    assert ctx.state["llama_sse"]["status"] == "sleeping"


def test_sse_reload_events_force_rebootstrap(llama, monkeypatch):
    ctx = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    hits = []
    llama.set_reconcile_hook(lambda: hits.append(1))
    llama._reconcile_mark["last"] = 0.0
    llama._llama_sse_on_event("models_reload", {"model": "*"})
    assert hits == [1]


def test_router_mode_prefers_live_models_and_falls_back_to_unit_file(llama, monkeypatch):
    ctx = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama.requests, "get",
                        lambda url, **kw: _Resp({"data": [{"id": "a", "status": {"value": "loaded"}}]}),
                        raising=False)
    assert llama._llama_router_mode() is True

    def _boom(url, **kw):
        raise ConnectionError("refused")
    monkeypatch.setattr(llama.requests, "get", _boom, raising=False)
    monkeypatch.setattr(llama, "_llama_router_mode_from_unit_file", lambda: False)
    assert llama._llama_router_mode() is False


def test_router_mode_empty_model_list_is_not_evidence_and_defers_to_the_unit_file(llama, monkeypatch):
    ctx = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama.requests, "get", lambda url, **kw: _Resp({"data": []}), raising=False)
    monkeypatch.setattr(llama, "_llama_router_mode_from_unit_file", lambda: True)
    assert llama._llama_router_mode() is True
