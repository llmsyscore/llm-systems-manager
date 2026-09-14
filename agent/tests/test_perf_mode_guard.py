"""#966: jobs and the manual button go through the power arbiter; endpoints trigger reconcile."""
import sys
import types

import pytest

from tests.test_autotune_v2_routes import _load_llama


@pytest.fixture(scope="module")
def llama():
    return sys.modules.get("providers.llama") or _load_llama()


class _Arb:
    def __init__(self, mode="full", outcome=None):
        self.mode, self.calls, self.holds, self.timeouts = mode, [], {}, []
        self._outcome = outcome

    def request(self, profile, owner, timeout=45.0):
        self.calls.append(("request", profile, owner)); self.timeouts.append(timeout)
        self.holds[owner] = profile
        out = self._outcome or ("skipped" if self.mode != "full" else "verified")
        return {"outcome": out, "applied": profile if out == "verified" else None, "governor": "performance"
                if profile == "performance" and out == "verified" else "powersave", "error": None, "owner": owner,
                "desired": profile, "mode": self.mode}

    def release(self, owner):
        self.calls.append(("release", owner)); self.holds.pop(owner, None)

    def snapshot(self):
        return {"mode": self.mode, "desired": None, "applied": None, "owner": next(iter(self.holds), None),
                "outcome": None, "error": None, "governor": "powersave", "last_switch": None,
                "counters": {}, "enabled": self.mode != "disabled", "awake": "turbo", "sleep": "eco"}


class _Ctx:
    def __init__(self):
        import threading
        self.config = types.SimpleNamespace(
            PERF_CONTROLLER_ENABLED=True, PERF_TARGET_AWAKE="turbo", PERF_TARGET_SLEEP="eco",
            LLAMA_BIN="", LLAMA_ENABLED=True, AGENT_INSTALL_DIR="/tmp", LLAMA_API_URL="http://127.0.0.1:9931",
            LLAMA_SYSTEMD_UNIT="llama-server")
        self.state = {"token": "", "agent_id": ""}
        self.runtime_lock = threading.RLock()
        self.post_session = None

    def check_bearer(self, *a, **k):
        return None


def _wire(llama, monkeypatch, arb):
    ctx = _Ctx()
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama.power_arbiter, "get", lambda: arb)
    hits = []
    llama.set_reconcile_hook(lambda: hits.append(1))
    llama._reconcile_mark["last"] = 0.0
    return ctx, hits


def test_disabled_arbiter_is_a_no_op_with_a_reason(llama, monkeypatch):
    arb = _Arb(mode="disabled")
    _wire(llama, monkeypatch, arb)
    events = []
    llama._perf_job_set("awake", events.append)
    ev = events[0]
    assert ev["type"] == "perf_mode" and ev["enabled"] is False and ev["skipped"] is True
    assert ev["ok"] is False and "disabled" in ev["error"]


def test_job_acquires_then_releases_and_reports_unit_names(llama, monkeypatch):
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    events = []
    llama._perf_job_set("awake", events.append)
    llama._perf_job_set("sleep", events.append)
    assert arb.calls == [("request", "performance", "job"), ("release", "job")]
    assert [e["mode"] for e in events] == ["turbo", "eco"] and [e["phase"] for e in events] == ["awake", "sleep"]
    assert events[0]["ok"] and events[0]["owner"] == "job" and events[0]["outcome"] == "verified"


def test_deferred_switch_still_lets_the_job_run(llama, monkeypatch):
    arb = _Arb(outcome="deferred")
    _wire(llama, monkeypatch, arb)
    events = []
    llama._perf_job_set("awake", events.append)
    assert events[0]["ok"] is True and events[0]["outcome"] == "deferred"
    assert events[0]["skipped"] is False


def test_offline_benchmark_sets_and_resets(llama, monkeypatch):
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    events = []
    monkeypatch.setattr(llama, "_bench_put", events.append)
    monkeypatch.setattr(llama, "_bench_run_one", lambda *a, **k: None)
    llama._bench_run_all(["org/m:Q4"], "llama-bench", [])
    assert arb.calls == [("request", "performance", "job"), ("release", "job")]
    assert [e["phase"] for e in events if e["type"] == "perf_mode"] == ["awake", "sleep"]


def test_offline_benchmark_resets_when_the_job_raises(llama, monkeypatch):
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    events = []
    monkeypatch.setattr(llama, "_bench_put", events.append)

    def _boom(*a, **k):
        raise RuntimeError("bench blew up")
    monkeypatch.setattr(llama, "_bench_run_one", _boom)
    llama._bench_run_all(["org/m:Q4"], "llama-bench", [])
    assert arb.calls[-1] == ("release", "job")
    assert any(e["type"] == "done" and not e["ok"] for e in events)


def test_autotune_sets_and_resets_when_the_run_raises(llama, monkeypatch):
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    events = []
    monkeypatch.setattr(llama, "_autotune_put", events.append)
    monkeypatch.setattr(llama, "_bench_live_runtime", lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    llama._autotune_run_all({"model_ids": ["org/m:Q4"], "mode": "tune"})
    assert arb.calls == [("request", "performance", "job"), ("release", "job")]


# ── the manual Performance / Powersave / Auto button ────────────────

def test_manual_switch_goes_through_the_arbiter(llama, monkeypatch):
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    out = llama.llama_bench_perf_mode({"mode": "performance"})
    assert out["ok"] and out["unit"] == "turbo" and out["owner"] == "manual"
    assert arb.calls == [("request", "performance", "manual")]


def test_manual_switch_answers_inside_the_manager_proxy_budget(llama, monkeypatch):
    """The manager proxies /api/benchmark/perf-mode with timeout=35; the arbiter must answer first."""
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    llama.llama_bench_perf_mode({"mode": "powersave"})
    assert arb.timeouts == [25.0] and llama._MANUAL_SWITCH_TIMEOUT_S == 25.0


def test_disabled_reply_carries_the_power_snapshot(llama, monkeypatch):
    arb = _Arb(mode="disabled")
    _wire(llama, monkeypatch, arb)
    out = llama.llama_bench_perf_mode({"mode": "performance"})
    assert out["ok"] is False and out["mode"] == "performance" and out["unit"] is None
    assert out["outcome"] == "skipped" and out["governor"] == "powersave" and out["enabled"] is False


def test_job_hold_does_not_pass_a_manual_timeout(llama, monkeypatch):
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    llama._perf_job_set("awake", lambda ev: None)
    assert arb.timeouts == [45.0]


def test_manual_auto_releases_the_hold(llama, monkeypatch):
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    llama.llama_bench_perf_mode({"mode": "powersave"})
    out = llama.llama_bench_perf_mode({"mode": "auto"})
    assert out["ok"] and out["mode"] == "auto" and arb.calls[-1] == ("release", "manual")


def test_manual_switch_is_refused_when_the_arbiter_is_off(llama, monkeypatch):
    arb = _Arb(mode="disabled")
    _wire(llama, monkeypatch, arb)
    out = llama.llama_bench_perf_mode({"mode": "performance"})
    assert out["ok"] is False and "disabled" in out["error"]


def test_manual_switch_is_refused_in_observe_mode(llama, monkeypatch):
    arb = _Arb(mode="observe")
    _wire(llama, monkeypatch, arb)
    out = llama.llama_bench_perf_mode({"mode": "performance"})
    assert out["ok"] is False and "observe" in out["error"] and arb.calls == []


def test_manual_auto_still_releases_the_hold_in_observe_mode(llama, monkeypatch):
    arb = _Arb(mode="observe")
    _wire(llama, monkeypatch, arb)
    out = llama.llama_bench_perf_mode({"mode": "auto"})
    assert out["ok"] and out["mode"] == "auto" and arb.calls == [("release", "manual")]


def test_manual_switch_reports_a_failed_switch(llama, monkeypatch):
    arb = _Arb(outcome="failed")
    _wire(llama, monkeypatch, arb)
    out = llama.llama_bench_perf_mode({"mode": "performance"})
    assert out["ok"] is False and out["outcome"] == "failed" and out["error"]


def test_manual_switch_still_validates_the_mode(llama, monkeypatch):
    arb = _Arb()
    _wire(llama, monkeypatch, arb)
    assert llama.llama_bench_perf_mode({"mode": "turbo"})["ok"] is False and arb.calls == []


# ── lifecycle endpoints ask for an early reconcile ───────────────────

def test_server_actions_trigger_reconcile(llama, monkeypatch):
    arb = _Arb()
    ctx, hits = _wire(llama, monkeypatch, arb)

    class _P:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(llama.subprocess, "run", lambda *a, **k: _P())
    out = llama.llama_server_restart_endpoint(None)
    assert out["ok"] and hits == [1]


def test_unload_triggers_reconcile(llama, monkeypatch):
    arb = _Arb()
    ctx, hits = _wire(llama, monkeypatch, arb)

    class _R:
        ok, status_code, text = True, 200, ""

        def json(self):
            return {"ok": True}
    monkeypatch.setattr(llama.requests, "post", lambda *a, **k: _R(), raising=False)
    monkeypatch.setattr(llama, "_llama_wait_unloaded", lambda api, **k: True)
    out = llama.llama_unload_endpoint({"model": "org/m:Q4"}, None)
    assert out["ok"] and hits == [1]
