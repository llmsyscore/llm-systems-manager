"""#966: the one writer of the perf units — owners, dwell, verify, retry, record, mode."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_PY = _AGENT_ROOT / "providers" / "power_arbiter.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


PA = _load("power_arbiter", _PY)
UNITS = {"performance": "turbo", "powersave": "eco"}


class FakeSys:
    """Fake systemctl + governor: records argv, returns scripted rc, tracks governor."""
    def __init__(self, rc=0, governor="powersave", cpufreq=True, show_result="success"):
        self.runs, self.rc, self.governor, self.cpufreq, self.show_result = [], rc, governor, cpufreq, show_result
        self.t = 0.0

    def run(self, argv, **kw):
        self.runs.append(list(argv))
        if argv[:3] == ["sudo", "-n", "/usr/bin/systemctl"]:
            if self.rc == 0 and self.cpufreq:
                self.governor = "performance" if argv[-1] == "turbo" else "powersave"
            return types.SimpleNamespace(returncode=self.rc, stdout="", stderr="denied" if self.rc else "")
        if len(argv) > 1 and argv[0].endswith("systemctl") and argv[1] == "show":
            return types.SimpleNamespace(returncode=0, stdout=f"Result={self.show_result}\nExecMainStatus=0\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def read_governor(self, fresh=False):
        return self.governor if self.cpufreq else None

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def switches(self):
        return [r[-1] for r in self.runs if r[:3] == ["sudo", "-n", "/usr/bin/systemctl"]]


def _arb(fs, tmp_path=None, **kw):
    a = PA.PowerArbiter(UNITS, mode=kw.pop("mode", "full"), run=fs.run, governor_reader=fs.read_governor,
                        record_path=str(tmp_path / "power.json") if tmp_path else None,
                        clock=fs.clock, sleep=fs.sleep, **kw)
    a.start()
    return a


# ── mode selection ───────────────────────────────────────────────────

def test_select_mode_table():
    ok = "    (root) NOPASSWD: /usr/bin/systemctl reload-or-restart eco, /usr/bin/systemctl reload-or-restart turbo"
    assert PA.select_mode(enabled=False, is_linux=True, sudo_list=ok, unit_files_present=True,
                          units=UNITS)[0] == "disabled"
    assert PA.select_mode(enabled=True, is_linux=False, sudo_list=ok, unit_files_present=True,
                          units=UNITS)[0] == "observe"
    assert PA.select_mode(enabled=True, is_linux=True, sudo_list="nothing", unit_files_present=True,
                          units=UNITS)[0] == "observe"
    assert PA.select_mode(enabled=True, is_linux=True, sudo_list=ok, unit_files_present=False,
                          units=UNITS)[0] == "observe"
    assert PA.select_mode(enabled=True, is_linux=True, sudo_list=ok, unit_files_present=True,
                          units=UNITS) == ("full", "")


# ── manual / job requests ────────────────────────────────────────────

def test_manual_request_switches_and_verifies():
    fs = FakeSys()
    a = _arb(fs)
    out = a.request("performance", "manual")
    assert out["outcome"] == "verified" and out["applied"] == "performance"
    assert fs.switches() == ["turbo"]
    assert a.snapshot()["owner"] == "manual" and a.snapshot()["governor"] == "performance"
    a.stop()


def test_job_outranks_manual_and_release_restores_lower_owner():
    fs = FakeSys()
    a = _arb(fs)
    a.request("powersave", "manual")
    a.request("performance", "job")
    assert fs.switches() == ["eco", "turbo"]
    a.release("job")
    a.wait_idle(5.0)
    assert fs.switches() == ["eco", "turbo", "eco"] and a.snapshot()["owner"] == "manual"
    a.stop()


def test_release_with_no_holds_falls_back_to_policy():
    fs = FakeSys()
    a = _arb(fs, dwell_ticks=1)
    a.set_policy("powersave")
    a.wait_idle(5.0)
    a.request("performance", "job")
    a.release("job")
    a.wait_idle(5.0)
    assert fs.switches() == ["eco", "turbo", "eco"] and a.snapshot()["owner"] == "policy"
    a.stop()


def test_manual_is_released_when_aggregate_changes():
    fs = FakeSys()
    a = _arb(fs, dwell_ticks=1)
    a.on_aggregate("sleeping")
    a.set_policy("powersave"); a.wait_idle(5.0)
    a.request("performance", "manual")
    a.on_aggregate("sleeping")           # same aggregate: keeps manual
    assert a.snapshot()["owner"] == "manual"
    a.on_aggregate("active")             # change: releases manual
    a.set_policy("performance"); a.wait_idle(5.0)
    assert a.snapshot()["owner"] == "policy"
    a.stop()


# ── policy dwell / hold / converged ──────────────────────────────────

def test_policy_needs_dwell_ticks_then_applies_once():
    fs = FakeSys()
    a = _arb(fs, dwell_ticks=2)
    a.set_policy("performance"); a.wait_idle(1.0)
    assert fs.switches() == []
    a.set_policy("performance"); a.wait_idle(5.0)
    assert fs.switches() == ["turbo"]
    for _ in range(5):
        a.set_policy("performance"); a.wait_idle(1.0)
    assert fs.switches() == ["turbo"]
    a.stop()


def test_hold_does_not_switch_and_does_not_reset_dwell_target():
    fs = FakeSys()
    a = _arb(fs, dwell_ticks=1)
    a.set_policy("hold"); a.wait_idle(1.0)
    assert fs.switches() == []
    a.set_policy("powersave"); a.wait_idle(5.0)
    assert fs.switches() == ["eco"]
    a.stop()


def test_flapping_candidate_never_reaches_dwell():
    fs = FakeSys()
    a = _arb(fs, dwell_ticks=2)
    for p in ("performance", "powersave", "performance", "powersave"):
        a.set_policy(p); a.wait_idle(1.0)
    assert fs.switches() == []
    a.stop()


# ── verification outcomes and retry ──────────────────────────────────

def test_failed_switch_keeps_previous_applied_and_retries_with_backoff():
    fs = FakeSys(rc=1)
    a = _arb(fs, dwell_ticks=1)
    out = a.request("performance", "manual", timeout=5.0)
    assert out["outcome"] == "failed" and out["applied"] is None and "denied" in out["error"]
    snap = a.snapshot()
    assert snap["counters"]["failures"] == 1 and snap["desired"] == "performance"
    fs.rc = 0
    a.wait_idle(120.0)                    # backoff 5s elapses on the fake clock
    assert a.snapshot()["applied"] == "performance" and a.snapshot()["outcome"] == "verified"
    a.stop()


def test_no_cpufreq_is_applied_unverifiable():
    fs = FakeSys(cpufreq=False)
    a = _arb(fs)
    out = a.request("powersave", "manual")
    assert out["outcome"] == "applied_unverifiable" and out["applied"] == "powersave"
    assert out["governor"] is None and a.snapshot()["counters"]["unverifiable"] == 1
    a.stop()


def test_opposite_profile_governor_is_failed():
    fs = FakeSys()
    fs.read_governor = lambda fresh=False: "powersave"
    a = _arb(fs)
    out = a.request("performance", "manual", timeout=5.0)
    assert out["outcome"] == "failed"
    assert "powersave" in out["error"] and "performance" in out["error"]
    a.stop()


class RecLogger:
    """Records warnings; everything else is swallowed."""
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, *a, **k):
        pass

    debug = error = info


def test_third_party_governor_is_unverifiable_and_warns_once():
    fs = FakeSys()
    fs.read_governor = lambda fresh=False: "schedutil"
    lg = RecLogger()
    a = _arb(fs, logger=lg)
    out = a.request("performance", "manual", timeout=5.0)
    assert out["outcome"] == "applied_unverifiable" and out["applied"] == "performance"
    assert out["governor"] == "schedutil"
    assert a.snapshot()["counters"]["unverifiable"] == 1 and a.snapshot()["counters"]["failures"] == 0
    a.request("powersave", "manual", timeout=5.0)
    assert a.snapshot()["counters"]["unverifiable"] == 2
    assert len([w for w in lg.warnings if "schedutil" in w]) == 1
    a.stop()


def test_hybrid_governor_list_is_unverifiable():
    fs = FakeSys()
    fs.read_governor = lambda fresh=False: "performance,powersave"
    a = _arb(fs)
    out = a.request("performance", "manual", timeout=5.0)
    assert out["outcome"] == "applied_unverifiable" and out["governor"] == "performance,powersave"
    a.stop()


def test_retry_is_cleared_once_desired_matches_applied():
    fs = FakeSys(rc=1)
    a = _arb(fs, dwell_ticks=1)
    out = a.request("performance", "manual", timeout=5.0)
    assert out["outcome"] == "failed" and a._retry_at is not None
    fs.rc = 0
    a.wait_idle(120.0)                      # backoff elapses, the retry verifies
    assert a.snapshot()["outcome"] == "verified" and a._retry_at is None
    fs.rc = 1
    a.release("manual")
    a.request("powersave", "manual", timeout=5.0)
    assert a._retry_at is not None
    a.release("manual")                     # nothing desired: the arbiter converges
    a.wait_idle(5.0)
    time.sleep(0.2)
    runs = len(fs.runs)
    fs.t += 2.0
    time.sleep(0.3)
    assert a._retry_at is None and len(fs.runs) == runs
    a.stop()


def test_failed_attempt_at_the_other_profile_does_not_linger_after_convergence():
    fs = FakeSys()
    a = _arb(fs, dwell_ticks=1)
    assert a.request("powersave", "manual", timeout=5.0)["outcome"] == "verified"
    a.release("manual")
    fs.rc = 1
    assert a.request("performance", "manual", timeout=5.0)["outcome"] == "failed"
    assert a.snapshot()["outcome"] == "failed" and a.snapshot()["applied"] == "powersave"
    a.release("manual")                     # back to nothing desired: powersave stays applied
    a.wait_idle(5.0)
    time.sleep(0.2)
    snap = a.snapshot()
    assert snap["applied"] == "powersave" and snap["outcome"] == "verified" and snap["error"] is None
    assert snap["readback"] and snap["readback"][0]["actual"] == "powersave"
    a.stop()
    a.stop()


def test_record_is_written_with_owner_only_permissions(tmp_path):
    fs = FakeSys()
    a = _arb(fs, tmp_path)
    a.request("performance", "manual")
    assert oct((tmp_path / "power.json").stat().st_mode & 0o777) == "0o600"
    a.stop()


def test_rate_cap_defers_the_seventh_switch():
    fs = FakeSys()
    a = _arb(fs, max_switches_per_min=6)
    for i in range(7):
        a.request("performance" if i % 2 == 0 else "powersave", "manual", timeout=2.0)
    assert len(fs.switches()) == 6 and a.snapshot()["counters"]["deferred"] >= 1
    a.stop()


# ── observe / disabled ───────────────────────────────────────────────

def test_observe_mode_never_runs_systemctl_but_tracks_desired():
    fs = FakeSys()
    a = _arb(fs, mode="observe", dwell_ticks=1)
    out = a.request("performance", "manual")
    a.set_policy("powersave"); a.wait_idle(1.0)
    assert fs.runs == [] and out["outcome"] == "skipped"
    assert a.snapshot()["mode"] == "observe" and a.snapshot()["desired"] == "performance"
    a.stop()


# ── durable record ───────────────────────────────────────────────────

def test_record_written_after_switch_and_job_hold_released_on_recover(tmp_path):
    fs = FakeSys()
    a = _arb(fs, tmp_path)
    a.request("performance", "job")
    rec = json.loads((tmp_path / "power.json").read_text())
    assert rec["applied_profile"] == "performance" and rec["owner"] == "job" and rec["outcome"] == "verified"
    a.stop()
    b = _arb(fs, tmp_path)
    b.recover()
    snap = b.snapshot()
    assert snap["applied"] == "performance" and snap["owner"] != "job"
    b.stop()


def test_record_write_failure_does_not_block_switch(tmp_path):
    fs = FakeSys()
    a = _arb(fs, tmp_path / "missing-dir")
    out = a.request("performance", "manual")
    assert out["outcome"] == "verified"
    a.stop()


def test_snapshot_shape():
    fs = FakeSys()
    a = _arb(fs)
    s = a.snapshot()
    for k in ("mode", "desired", "applied", "owner", "outcome", "governor", "last_switch",
              "counters", "enabled", "awake", "sleep"):
        assert k in s
    assert s["awake"] == "turbo" and s["sleep"] == "eco" and s["enabled"] is True
    a.stop()


# ── requests that need no switch must not block ──────────────────────

def test_request_for_the_already_applied_profile_returns_at_once():
    fs = FakeSys()
    a = _arb(fs)
    a.request("performance", "job")
    t0 = time.monotonic()
    out = a.request("performance", "job", timeout=30.0)
    assert out["outcome"] == "verified" and out["applied"] == "performance"
    assert time.monotonic() - t0 < 1.0
    assert fs.switches() == ["turbo"]
    a.stop()


def test_outranked_request_returns_at_once_and_keeps_the_hold():
    fs = FakeSys()
    a = _arb(fs)
    a.request("performance", "job")
    t0 = time.monotonic()
    out = a.request("powersave", "manual", timeout=30.0)
    assert out["outcome"] == "skipped" and "job" in out["error"]
    assert time.monotonic() - t0 < 1.0
    assert fs.switches() == ["turbo"] and a.snapshot()["desired"] == "performance"
    a.release("job")
    a.wait_idle(5.0)
    assert fs.switches() == ["turbo", "eco"] and a.snapshot()["owner"] == "manual"
    a.stop()


def test_cap_deferred_request_returns_deferred_at_once():
    fs = FakeSys()
    a = _arb(fs, max_switches_per_min=2)
    a.request("performance", "manual")
    a.request("powersave", "manual")
    t0 = time.monotonic()
    out = a.request("performance", "manual", timeout=30.0)
    assert out["outcome"] == PA.OUTCOME_DEFERRED and "cap" in out["error"]
    assert time.monotonic() - t0 < 1.0
    assert len(fs.switches()) == 2 and a.snapshot()["counters"]["deferred"] == 1
    a.stop()


# ── worker lifecycle ─────────────────────────────────────────────────

class StuckThread:
    """Stands in for a worker that outlives stop()'s join."""
    def __init__(self):
        self.joins = []

    def join(self, timeout=None):
        self.joins.append(timeout)

    def is_alive(self):
        return True


def test_stop_keeps_the_worker_slot_when_the_thread_will_not_exit():
    fs = FakeSys()
    a = _arb(fs)
    a.stop()
    stuck = StuckThread()
    a._thread = stuck
    a.stop()
    assert a._thread is stuck and stuck.joins == [5.0]
    a.start()
    assert a._thread is stuck


# ── recover ──────────────────────────────────────────────────────────

def test_recover_drops_a_recorded_manual_hold(tmp_path):
    (tmp_path / "power.json").write_text(json.dumps(
        {"applied_profile": "performance", "owner": "manual", "outcome": "verified"}))
    fs = FakeSys(governor="performance")
    a = _arb(fs, tmp_path, dwell_ticks=1)
    a.recover()
    snap = a.snapshot()
    assert snap["applied"] == "performance" and snap["owner"] is None and snap["desired"] is None
    a.set_policy("powersave"); a.wait_idle(5.0)
    assert fs.switches() == ["eco"] and a.snapshot()["owner"] == "policy"
    a.stop()


def test_recover_ignores_a_record_the_governor_disagrees_with(tmp_path):
    (tmp_path / "power.json").write_text(json.dumps(
        {"applied_profile": "performance", "owner": "job", "outcome": "verified"}))
    fs = FakeSys(governor="powersave")
    a = _arb(fs, tmp_path)
    a.recover()
    snap = a.snapshot()
    assert snap["applied"] is None and snap["governor"] == "powersave"
    assert fs.switches() == []
    a.stop()


def test_recover_adopts_the_record_without_cpufreq(tmp_path):
    (tmp_path / "power.json").write_text(json.dumps(
        {"applied_profile": "powersave", "owner": "job", "outcome": "applied_unverifiable"}))
    fs = FakeSys(cpufreq=False)
    a = _arb(fs, tmp_path)
    a.recover()
    snap = a.snapshot()
    assert snap["applied"] == "powersave" and snap["governor"] is None
    a.stop()


def test_job_hold_stays_until_the_last_holder_releases():
    fs = FakeSys()
    a = _arb(fs, dwell_ticks=1)
    a.set_policy("powersave")
    a.wait_idle(5.0)
    a.request("performance", "job", holder="autotune")
    a.request("performance", "job", holder="bench")
    a.release("job", holder="bench")
    a.wait_idle(5.0)
    assert a.snapshot()["owner"] == "job" and a.snapshot()["desired"] == "performance"
    assert fs.switches() == ["eco", "turbo"]
    a.release("job", holder="autotune")
    a.wait_idle(5.0)
    assert a.snapshot()["owner"] == "policy" and fs.switches() == ["eco", "turbo", "eco"]
    a.stop()


def test_release_without_a_holder_drops_every_holder():
    fs = FakeSys()
    a = _arb(fs)
    a.request("performance", "job", holder="autotune")
    a.request("performance", "job", holder="bench")
    a.release("job")
    a.wait_idle(5.0)
    assert a.snapshot()["owner"] is None
    a.stop()
