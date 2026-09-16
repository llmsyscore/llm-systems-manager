"""#915 job service: ledger, outcomes, periodic runs, exclusive keys, cancel, timeout, recover, sweep, alerts."""
from __future__ import annotations

import sqlite3
import threading
import time
import types

import pytest
from pydantic import ValidationError

import jobs


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _cfg(**over):
    d = dict(workers=4, history_days=30)
    d.update(over)
    return types.SimpleNamespace(**d)


def _service(clock=None, alerts=None, audits=None, cfg=None):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    jobs.init_table(conn)
    store = jobs.Store(lambda: conn)
    svc = jobs.Service(store, cfg=lambda: cfg or _cfg(), alert=(alerts.append if alerts is not None else None),
                       audit=(audits.append if audits is not None else None), now=clock or Clock(), inline=True)
    return svc, store


def _threaded(cfg=None, clock=None):
    """A Service that really launches worker threads, so `_workers` matters."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    jobs.init_table(conn)
    store = jobs.Store(lambda: conn)
    return jobs.Service(store, cfg=lambda: cfg or _cfg(), now=clock or Clock(), inline=False), store


def _echo_kind(calls, **over):
    def run(job):
        calls.append(job.row())
        return jobs.ok(result={"n": job.run_count + 1})
    kw = dict(name="echo", title="Echo", run=run)
    kw.update(over)
    return jobs.Kind(**kw)


def test_submit_validates_labels_and_lists_live_rows():
    audits = []
    svc, _ = _service(audits=audits)
    svc.register(jobs.Kind("echo", "Echo", run=lambda j: jobs.ok(),
                           validate=lambda spec, who: ((spec, None) if spec.get("x") else (None, "x is required")),
                           label=lambda spec: f"echo {spec['x']}", exclusive=lambda spec: [f"k:{spec['x']}"]))
    with pytest.raises(jobs.JobError) as e:
        svc.submit("echo", {}, user="alice", role="operator", source="ui")
    assert str(e.value) == "x is required" and e.value.status == 400
    with pytest.raises(jobs.JobError) as e:
        svc.submit("nope", {"x": 1})
    assert e.value.status == 404
    row = svc.submit("echo", {"x": 7}, user="alice", role="operator", source="ui")
    assert row["status"] == "queued" and row["label"] == "echo 7" and row["exclusive"] == ["k:7"]
    assert row["next_run"] == 1000.0 and row["not_before"] == 1000.0 and row["state"] == {}
    assert [r["id"] for r in svc.list()] == [row["id"]]
    assert svc.list(status="done") == [] and svc.list(kind="other") == [] and svc.list(user="bob") == []
    assert audits[-1]["action"] == "jobs.submit" and audits[-1]["detail"]["kind"] == "echo"
    assert svc.set_state(row["id"], {"a": 1}) and svc.get(row["id"])["state"] == {"a": 1}


def test_tick_runs_a_due_job_once_and_marks_it_done():
    clock = Clock(1000.0)
    calls = []
    svc, _ = _service(clock=clock)
    svc.register(_echo_kind(calls))
    row = svc.submit("echo", {}, not_before=1030.0)
    assert svc.tick() == 0 and calls == []
    clock.t = 1030.0
    assert svc.tick() == 1
    got = svc.get(row["id"])
    assert got["status"] == "done" and got["result"] == {"n": 1} and got["run_count"] == 1
    assert got["last_run"] == 1030.0 and got["resolved"] == 1030.0 and got["message"] == "done"
    assert calls[0]["status"] == "running" and svc.tick() == 0


def test_periodic_job_requeues_until_runs_left_is_spent():
    clock = Clock(1000.0)
    calls = []
    svc, _ = _service(clock=clock)
    svc.register(_echo_kind(calls))
    row = svc.submit("echo", {}, period_s=60.0, runs_left=2)
    svc.tick()
    got = svc.get(row["id"])
    assert got["status"] == "queued" and got["next_run"] == 1060.0 and got["runs_left"] == 1 and got["run_count"] == 1
    clock.t = 1060.0
    svc.tick()
    assert svc.get(row["id"])["status"] == "done" and len(calls) == 2


def test_again_and_finish_drive_phases():
    clock = Clock(1000.0)
    svc, _ = _service(clock=clock)

    def run(job):
        if job.state.get("phase") != "b":
            return jobs.again(5.0, state={"phase": "b"}, message="phase b next")
        return jobs.finish(result={"ok": True}, message="all done")
    svc.register(jobs.Kind("ph", "Phases", run=run))
    row = svc.submit("ph", {})
    svc.tick()
    got = svc.get(row["id"])
    assert got["status"] == "queued" and got["next_run"] == 1005.0 and got["state"] == {"phase": "b"} and got["message"] == "phase b next"
    clock.t = 1005.0
    svc.tick()
    got = svc.get(row["id"])
    assert got["status"] == "done" and got["result"] == {"ok": True} and got["message"] == "all done"


def test_fail_with_retry_requeues_then_final_failure_alerts_and_audits():
    clock = Clock(1000.0)
    alerts, audits, finished = [], [], []
    svc, _ = _service(clock=clock, alerts=alerts, audits=audits)
    svc.register(jobs.Kind("boom", "Boom", run=lambda j: jobs.fail("nope", retry_in=10.0 if j.attempts == 0 else None),
                           on_finish=lambda j: finished.append(j.status)))
    row = svc.submit("boom", {}, user="alice", label="the boom")
    svc.tick()
    got = svc.get(row["id"])
    assert got["status"] == "queued" and got["attempts"] == 1 and got["next_run"] == 1010.0 and alerts == []
    clock.t = 1010.0
    svc.tick()
    got = svc.get(row["id"])
    assert got["status"] == "failed" and got["attempts"] == 2 and got["message"] == "nope" and finished == ["failed"]
    assert alerts == [{"name": "Job failed: the boom", "source": "jobs", "metric": f"jobs/boom/{row['id']}", "severity": "warning",
                       "value": 2, "threshold": 0, "message": "Boom · the boom · nope"}]
    assert audits[-1]["action"] == "jobs.failed" and audits[-1]["ok"] is False


def test_handler_exception_is_a_failure_not_a_crash():
    svc, _ = _service()

    def run(job):
        raise RuntimeError("secret path /x")
    svc.register(jobs.Kind("bad", "Bad", run=run))
    row = svc.submit("bad", {})
    svc.tick()
    got = svc.get(row["id"])
    assert got["status"] == "failed" and got["message"] == "RuntimeError"


def test_exclusive_keys_serialise_jobs_and_workers_cap_concurrency():
    svc, store = _service(cfg=_cfg(workers=1))
    started = []
    svc.register(jobs.Kind("hold", "Hold", run=lambda j: started.append(j.id) or jobs.ok(), exclusive=lambda s: [f"perf:{s['h']}"]))
    a = svc.submit("hold", {"h": "x"})
    b = svc.submit("hold", {"h": "x"})
    store.update(a["id"], status="running", started=1000.0, lease=1030.0)   # a holds perf:x on a live worker
    assert svc.tick() == 0
    assert svc.get(b["id"])["message"] == "waiting for perf:x"
    c = svc.submit("hold", {"h": "y"})
    assert svc.tick() == 0          # workers=1 and a is running
    store.update(a["id"], status="done", resolved=1000.0)
    assert svc.tick() == 2 and started == [b["id"], c["id"]]


def test_cancel_queued_and_running_jobs():
    clock = Clock(1000.0)
    audits, cancelled, finished = [], [], []
    svc, store = _service(clock=clock, audits=audits)
    svc.register(jobs.Kind("k", "K", run=lambda j: jobs.ok(), on_cancel=lambda j: cancelled.append(j.id),
                           on_finish=lambda j: finished.append((j.id, j.status))))
    q = svc.submit("k", {}, user="alice")
    r = svc.submit("k", {}, user="alice")
    store.update(r["id"], status="running", started=1000.0)
    out = svc.cancel(q["id"], actor="alice")
    assert out["status"] == "cancelled" and out["message"] == jobs.CANCELLED and out["resolved"] == 1000.0 and cancelled == []
    out = svc.cancel(r["id"], actor="alice", message="stop it")
    assert out["status"] == "cancelled" and out["message"] == "stop it" and cancelled == [r["id"]]
    assert svc.cancel(q["id"]) is None and svc.cancel("nope") is None
    assert finished == [(q["id"], "cancelled"), (r["id"], "cancelled")]
    assert [a["action"] for a in audits[-2:]] == ["jobs.cancel", "jobs.cancel"]


def test_cancel_where_matches_kind_thread_and_spec():
    svc, _ = _service()
    svc.register(jobs.Kind("k", "K", run=lambda j: jobs.ok()))
    a = svc.submit("k", {"batch_id": "b1"}, thread_id="t1")
    b = svc.submit("k", {"batch_id": "b2"}, thread_id="t1")
    c = svc.submit("k", {"batch_id": "b3"}, thread_id="t2")
    assert svc.cancel_where("k", spec_match={"batch_id": "b1"}, message="gone") == 1
    assert svc.get(a["id"])["message"] == "gone"
    assert svc.cancel_where("k", thread_id="t1") == 1 and svc.get(b["id"])["status"] == "cancelled"
    assert svc.get(c["id"])["status"] == "queued"


def test_outcome_of_a_cancelled_run_is_discarded():
    svc, store = _service()
    svc.register(jobs.Kind("k", "K", run=lambda j: jobs.ok(result={"late": True})))
    r = svc.submit("k", {})
    store.update(r["id"], status="running", started=1000.0)
    svc.cancel(r["id"])
    svc._apply(r["id"], jobs.ok(result={"late": True}))
    got = svc.get(r["id"])
    assert got["status"] == "cancelled" and got["result"] is None


def test_overrun_times_out_fires_on_cancel_and_alerts():
    clock = Clock(1000.0)
    alerts, cancelled = [], []
    svc, store = _service(clock=clock, alerts=alerts)
    svc.register(jobs.Kind("slow", "Slow", run=lambda j: jobs.ok(), on_cancel=lambda j: cancelled.append(j.id),
                           max_run_s=lambda spec: float(spec["limit"])))
    r = svc.submit("slow", {"limit": 30})
    store.update(r["id"], status="running", started=1000.0, last_run=1000.0)
    clock.t = 1029.0
    svc.tick()
    assert svc.get(r["id"])["status"] == "running" and svc.get(r["id"])["lease"] == 1029.0 + jobs.LEASE_S
    clock.t = 1031.0
    svc.tick()
    got = svc.get(r["id"])
    assert got["status"] == "failed" and got["message"] == jobs.TIMED_OUT and cancelled == [r["id"]]
    assert alerts and alerts[-1]["message"].endswith(jobs.TIMED_OUT)


def test_recover_requeues_or_fails_running_rows_by_kind_policy():
    clock = Clock(2000.0)
    alerts = []
    svc, store = _service(clock=clock, alerts=alerts)
    svc.register(jobs.Kind("re", "Re", run=lambda j: jobs.ok(), resume="requeue"))
    svc.register(jobs.Kind("fa", "Fa", run=lambda j: jobs.ok(), resume="fail"))
    a = svc.submit("re", {})
    b = svc.submit("fa", {})
    store.update(a["id"], status="running", started=1500.0, lease=1530.0)
    store.update(b["id"], status="running", started=1500.0, lease=1530.0)
    store.insert({**svc.get(a["id"]), "id": "orphan0000000000", "kind": "gone", "status": "running"})
    assert svc.recover() == 3
    got_a = svc.get(a["id"])
    assert got_a["status"] == "queued" and got_a["next_run"] == 2000.0 and got_a["message"] == jobs.RESTARTED
    assert svc.get(b["id"])["status"] == "failed" and svc.get(b["id"])["message"] == jobs.RESTARTED
    assert svc.get("orphan0000000000")["status"] == "failed" and svc.get("orphan0000000000")["message"] == "kind not registered"
    assert len(alerts) == 2


def test_summary_recent_and_sweep():
    clock = Clock(1000.0)
    svc, store = _service(clock=clock, cfg=_cfg(history_days=2))
    svc.register(jobs.Kind("k", "K", run=lambda j: jobs.ok()))
    q = svc.submit("k", {}, not_before=1500.0, label="later")
    r = svc.submit("k", {}, label="now")
    store.update(r["id"], status="running", started=1000.0)
    f = svc.submit("k", {}, label="old fail")
    store.update(f["id"], status="failed", resolved=1000.0 - 3600.0)
    d = svc.submit("k", {}, label="ancient")
    store.update(d["id"], status="done", resolved=1000.0 - 3 * 86400.0)
    s = svc.summary()
    assert s == {"queued": 1, "running": 1, "failed_24h": 1, "next_due": 1500.0} and svc.get(q["id"])["next_run"] == 1500.0
    assert [x["label"] for x in svc.recent(3)] == ["now", "later", "old fail"]
    clock.t = 1000.0 + jobs.SWEEP_EVERY_S
    svc.tick()
    assert svc.get(d["id"]) is None and svc.get(f["id"]) is not None


def test_view_shape_and_can_cancel():
    clock = Clock(1000.0)
    svc, _ = _service(clock=clock)
    svc.register(jobs.Kind("k", "K title", run=lambda j: jobs.ok()))
    r = svc.submit("k", {"a": 1}, user="alice", role="operator", source="ui", label="L", not_before=1060.0)
    v = svc.view(r, role="operator", user="alice")
    assert v["id"] == r["id"] and v["kind"] == "k" and v["kind_title"] == "K title" and v["label"] == "L"
    assert v["status"] == "queued" and v["user"] == "alice" and v["source"] == "ui" and v["can_cancel"] is True
    assert v["next_run"] == 1060.0 and v["next_run_local"] and v["created_local"] and "spec" not in v
    assert svc.view(r, role="operator", user="bob")["can_cancel"] is False
    assert svc.view(r, role="admin", user="bob")["can_cancel"] is True
    assert svc.view(r, role="viewer", user="alice")["can_cancel"] is False
    full = svc.view(r, role="admin", user="x", detail=True)
    assert full["spec"] == {"a": 1} and full["state"] == {} and full["result"] is None


def test_start_thread_is_off_under_pytest():
    svc, _ = _service()
    assert jobs.start_thread(svc, lambda: True) is None


def test_manager_jobs_settings_and_catalog():
    import settings_catalog as sc
    from config.unified_config import settings
    assert settings.manager.jobs.workers == 4 and settings.manager.jobs.history_days == 30
    w, h = sc._BY_PATH["manager.jobs.workers"], sc._BY_PATH["manager.jobs.history_days"]
    assert w["group"] == "jobs" and w["hot"] is True and w["min"] == 1 and w["max"] == 16
    assert h["group"] == "jobs" and h["hot"] is True and h["min"] == 1 and h["max"] == 365
    assert ("jobs", "Jobs") in sc.GROUPS
    from config.unified_config import ManagerJobs
    for bad in ({"workers": 500}, {"workers": 0}, {"history_days": 0}, {"history_days": 400}):
        with pytest.raises(ValidationError):
            ManagerJobs(**bad)


def test_a_live_cancelled_worker_keeps_its_slot_and_exclusive_key():
    gate = threading.Event()
    svc, _ = _threaded(cfg=_cfg(workers=1))
    started = []

    def run(job):
        started.append(job.id)
        gate.wait(5.0)
        return jobs.ok()
    svc.register(jobs.Kind("hold", "Hold", run=run, exclusive=lambda s: [f"perf:{s['h']}"], max_run_s=10_000.0))
    a = svc.submit("hold", {"h": "x"})
    assert svc.tick() == 1
    for _ in range(200):
        if started:
            break
        time.sleep(0.01)
    assert started == [a["id"]]
    svc.cancel(a["id"])
    assert svc.get(a["id"])["status"] == "cancelled"
    b = svc.submit("hold", {"h": "x"})
    c = svc.submit("hold", {"h": "y"})
    assert svc.tick() == 0
    assert svc.get(b["id"])["message"] == "waiting for perf:x"
    assert svc.get(c["id"])["status"] == "queued" and svc.get(c["id"])["message"] == "queued"
    gate.set()
    svc._workers[a["id"]]["thread"].join(1.0)
    assert svc.tick() == 1
    assert svc.get(b["id"])["status"] in ("running", "done")
    assert svc.get(c["id"])["status"] == "queued"
    svc.cancel(b["id"])


def test_lease_is_refreshed_only_near_expiry():
    clock = Clock(1000.0)
    svc, store = _service(clock=clock)
    svc.register(jobs.Kind("k", "K", run=lambda j: jobs.ok(), max_run_s=10_000.0))
    r = svc.submit("k", {})
    store.update(r["id"], status="running", started=1000.0, lease=1000.0 + jobs.LEASE_S)
    clock.t = 1005.0
    svc.tick()
    assert svc.get(r["id"])["lease"] == 1000.0 + jobs.LEASE_S
    clock.t = 1015.0
    svc.tick()
    assert svc.get(r["id"])["lease"] == 1015.0 + jobs.LEASE_S


def test_fail_with_alert_off_audits_without_paging():
    alerts, audits = [], []
    svc, _ = _service(alerts=alerts, audits=audits)
    svc.register(jobs.Kind("quiet", "Quiet", run=lambda j: jobs.fail("the operator turned it off", alert=False)))
    row = svc.submit("quiet", {}, label="quiet one")
    svc.tick()
    got = svc.get(row["id"])
    assert got["status"] == "failed" and got["message"] == "the operator turned it off"
    assert alerts == []
    assert audits[-1]["action"] == "jobs.failed" and audits[-1]["detail"]["message"] == "the operator turned it off"


def test_list_orders_failed_rows_by_resolution_on_request():
    clock = Clock(1000.0)
    svc, store = _service(clock=clock)
    svc.register(jobs.Kind("k", "K", run=lambda j: jobs.ok()))
    old = svc.submit("k", {}, label="older created")
    clock.t = 1010.0
    new = svc.submit("k", {}, label="newer created")
    store.update(old["id"], status="failed", resolved=1200.0)
    store.update(new["id"], status="failed", resolved=1100.0)
    assert [r["label"] for r in svc.list("failed")] == ["newer created", "older created"]
    assert [r["label"] for r in svc.list("failed", order="resolved DESC")] == ["older created", "newer created"]
