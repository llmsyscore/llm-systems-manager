"""#966 end-to-end against a fake llama-server: collector -> residency -> profile."""
from __future__ import annotations

import importlib.util
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from tests._fake_llama import FakeLlama
from tests.test_autotune_v2_routes import _load_llama

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_PID = {"pid": 1}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load("residency_s", _AGENT_ROOT / "providers" / "residency.py")


@pytest.fixture(scope="module")
def llama():
    return sys.modules.get("providers.llama") or _load_llama()


@pytest.fixture(autouse=True)
def _fresh_collector(llama, monkeypatch):
    """Reset the collector's module state so every scenario starts as a fresh agent process."""
    for name, val in (("_llama_info_cache", {}), ("_llama_info_last_poll", 0.0),
                      ("_llama_info_last_active_ts", 0.0), ("_llama_info_last_tokens_total", None),
                      ("_llama_loaded", {"last": None}), ("_llama_info_idle_logged", False),
                      ("_llama_build_last", ""), ("_llama_main_pid", {"last": None}),
                      ("_residency_inputs", {"models": [], "server": "unknown", "ts": 0.0})):
        monkeypatch.setattr(llama, name, val)
    _PID["pid"] = 1
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: _PID["pid"])


class _Ctx:
    def __init__(self, url):
        self.config = types.SimpleNamespace(
            LLAMA_ENABLED=True, LLAMA_API_URL=url, POLL_INTERVAL_S=5, LLAMA_SYSTEMD_UNIT="llama-server",
            PERF_CONTROLLER_ENABLED=True, PERF_TARGET_AWAKE="performance", PERF_TARGET_SLEEP="powersave",
            LLAMA_BUILD_METHOD="")
        self.state = {"token": "", "agent_id": ""}
        self.runtime_lock = threading.RLock()
        self.now_iso = lambda: "now"

    def check_bearer(self, *a, **k):
        return None


def _tick(llama, ctx, pid=1, prev=None):
    """One collector tick: probe, reconcile, stamp legacy state — as _reconcile_residency does."""
    _PID["pid"] = pid
    llama._llama_info_last_poll = 0.0
    llama._llama_info_last_active_ts = time.time()
    sample = llama.collect_llama_for_metrics()
    models, server = llama.residency_inputs()
    res = R.reconcile(prev, models=models, servers={"llama": server}, ts=time.time())
    ctx.state["residency"] = res
    sample["residency"] = R.provider_subset(res, "llama")
    sample["state"] = R.legacy_state(sample["residency"])
    return sample, res


@pytest.fixture
def real_requests(llama, monkeypatch):
    import urllib.error
    import urllib.request

    class _Resp:
        def __init__(self, raw, code):
            self.text = raw.decode() if isinstance(raw, bytes) else raw
            self.status_code, self.ok = code, 200 <= code < 300

        def json(self):
            import json
            return json.loads(self.text or "null")

    def _get(url, timeout=2, params=None, headers=None, **kw):
        from urllib.parse import urlencode
        full = url + (("?" + urlencode(params)) if params else "")
        try:
            with urllib.request.urlopen(full, timeout=timeout) as r:
                return _Resp(r.read(), r.status)
        except urllib.error.HTTPError as e:
            return _Resp(e.read(), e.code)
    monkeypatch.setattr(llama.requests, "get", _get, raising=False)
    return _get


def test_unload_then_load_converges_each_tick(llama, monkeypatch, real_requests):
    fake = FakeLlama({"a": "loaded", "b": "unloaded"})
    ctx = _Ctx(fake.url)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    try:
        _, res = _tick(llama, ctx)
        assert res["aggregate"] == "active" and R.desired_profile(res) == "performance"
        fake.set_status("a", "unloaded")
        _, res = _tick(llama, ctx, prev=res)
        assert res["aggregate"] == "idle" and R.desired_profile(res) == "powersave"
        fake.set_status("b", "loading")
        _, res = _tick(llama, ctx, prev=res)
        assert res["aggregate"] == "loading" and R.legacy_state(res) == "awake"
        fake.set_status("b", "loaded")
        _, res = _tick(llama, ctx, prev=res)
        assert res["aggregate"] == "active"
    finally:
        fake.stop()


def test_awake_model_does_poll_metrics_and_slots(llama, monkeypatch, real_requests):
    """Control for the sleep scenario: the same fixture records /metrics + /slots when awake."""
    fake = FakeLlama({"a": "loaded"})
    ctx = _Ctx(fake.url)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    try:
        _tick(llama, ctx)
        assert fake.paths("/metrics") and fake.paths("/slots")
    finally:
        fake.stop()


def test_sleep_is_seen_from_props_and_never_polls_metrics(llama, monkeypatch, real_requests):
    fake = FakeLlama({"a": "loaded"})
    ctx = _Ctx(fake.url)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    try:
        fake.set_sleeping("a", True)
        sample, res = _tick(llama, ctx)
        assert res["aggregate"] == "sleeping" and sample["state"] == "sleeping"
        assert not any(p.startswith("/metrics") or p.startswith("/slots") for p in fake.requests)
        assert all("autoload=0" in p for p in fake.requests if p.startswith("/props"))
    finally:
        fake.stop()


def test_service_restart_changes_pid_and_state_follows(llama, monkeypatch, real_requests):
    fake = FakeLlama({"a": "loaded"})
    ctx = _Ctx(fake.url)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    try:
        _, res = _tick(llama, ctx, pid=100)
        fake.set_status("a", "unloaded")
        _, res = _tick(llama, ctx, pid=200, prev=res)     # new MainPID after restart
        assert res["aggregate"] == "idle"
    finally:
        fake.stop()


def test_crash_reports_off_after_unknown_ticks(llama, monkeypatch, real_requests):
    fake = FakeLlama({"a": "loaded"})
    ctx = _Ctx(fake.url)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    try:
        _, res = _tick(llama, ctx)
        fake.down()
        _, res = _tick(llama, ctx, prev=res)
        assert res["aggregate"] == "off" and R.desired_profile(res) == "powersave"
        assert R.legacy_state(res) == "unknown"
    finally:
        fake.stop()


def test_single_model_build_without_status_objects(llama, monkeypatch, real_requests):
    fake = FakeLlama({"solo": "loaded"}, router=False)
    ctx = _Ctx(fake.url)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    try:
        _, res = _tick(llama, ctx)
        assert res["aggregate"] == "active"
        fake.set_sleeping("solo", True)
        _, res = _tick(llama, ctx, prev=res)
        assert res["aggregate"] == "sleeping"
    finally:
        fake.stop()


def test_multi_model_mixed_statuses_aggregate_active(llama, monkeypatch, real_requests):
    fake = FakeLlama({"a": "sleeping", "b": "loaded", "c": "unloaded"})
    ctx = _Ctx(fake.url)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    try:
        _, res = _tick(llama, ctx)
        assert res["aggregate"] == "active"
        assert {m["model_id"]: m["status"] for m in res["models"]} == {"a": "sleeping", "b": "loaded", "c": "unloaded"}
    finally:
        fake.stop()


def test_agent_restart_first_tick_establishes_state_without_fabrication(llama, monkeypatch, real_requests):
    fake = FakeLlama({"a": "loaded"}, sleeping={"a"})
    ctx = _Ctx(fake.url)                       # fresh ctx == fresh agent process
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    try:
        assert llama.llama_get_state() == "unknown"     # nothing invented before the first tick
        _, res = _tick(llama, ctx)
        assert res["aggregate"] == "sleeping" and llama.llama_get_state() == "sleeping"
    finally:
        fake.stop()


# ── the arbiter is the only systemctl reload-or-restart caller ───────

def test_only_the_arbiter_runs_reload_or_restart():
    offenders = []
    scanned = 0
    for sub in ("providers", "collectors", "."):
        for p in sorted((_AGENT_ROOT / sub).glob("*.py")):
            if p.name == "power_arbiter.py":
                continue
            scanned += 1
            if re.search(r"reload-or-restart", p.read_text()):
                offenders.append(str(p.relative_to(_AGENT_ROOT)))
    assert offenders == [], offenders
    assert scanned > 10 and (_AGENT_ROOT / "llm-systems-agent.py").exists()
