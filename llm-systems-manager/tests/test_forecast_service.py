"""#1031 forecast service: merge/dedup, clearing, alert gates, run states, schedule, run-now and routes."""
from __future__ import annotations

import sqlite3
import threading
import types
from datetime import datetime, timezone

import pytest
from flask import Flask, session

import forecast
import forecast_checks
import forecast_effort
import forecast_tower
import jobs
import tower_watch

DAY = 86400.0
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def _cfg(**over):
    d = dict(enabled=True, every="daily", every_hours=8, run_day="mon", at="03:00", checks_disabled=[], window_days=14, alerts=False,
             alert_min_severity="warning", tower_effort="off", tower_history=False, digest_day="mon", digest_at="08:00")
    d.update(over)
    return types.SimpleNamespace(**d)


def finding(check="disk_fill", host="rig", subject="models", severity="warning", predicted_at=None, **over):
    """One finding row in the shape a check produces."""
    f = forecast_checks.Finding(check, host, subject, severity, "Disk is filling", detail="d", since=NOW - 5 * DAY,
                                predicted_at=predicted_at, rate=19.0, unit="GB/day", confidence="high",
                                suggested_action="free space", graph={"source": "system"})
    return {**f.to_row(), **over}


def obj(**over):
    """The same row as a Finding, for a fake check's detect()."""
    row = finding(**over)
    row.pop("fingerprint")
    return forecast_checks.Finding(**row)


def ck(cid="disk_fill", detect=None, title=None, min_days=3.0, tower=None):
    """A stand-in check; `tower` follows the real registry entry for that id unless it is given."""
    if tower is None:
        known = forecast_checks.BY_ID.get(cid)
        tower = True if known is None else known.tower
    return forecast_checks.Check(cid, title or forecast_checks.TITLES.get(cid, cid),
                                 detect or (lambda d: []), min_days, tower)


class RacingStore(forecast.Store):
    """A store whose open() runs a one-shot hook after the snapshot, to interleave a write with a clearing run."""
    def __init__(self, conn_factory):
        super().__init__(conn_factory)
        self.hook = None

    def open(self):
        rows = super().open()
        hook, self.hook = self.hook, None
        if hook is not None:
            hook(rows)
        return rows


class Fx:
    """A real job service and forecast store on in-memory sqlite, with fake alert and audit sinks."""
    def __init__(self, checks=None, clock=None, store_cls=forecast.Store, **over):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        jobs.init_table(self.conn)
        forecast.init_tables(self.conn)
        self.clock = clock or Clock()
        self.cfg = _cfg(**over)
        self.posts, self.closed, self.audits = [], [], []
        self.post_result = "auto"
        self.svc = jobs.Service(jobs.Store(lambda: self.conn),
                                cfg=lambda: types.SimpleNamespace(workers=4, history_days=30),
                                now=self.clock, inline=True)
        self.store = store_cls(lambda: self.conn)
        self.f = forecast.Forecast(self.svc, self.store, cfg=lambda: self.cfg,
                                   data_factory=lambda now, window_s: forecast_checks.CheckData(now=now, window_s=window_s),
                                   alert_post=self._post, alert_close=self._close, audit=self.audits.append,
                                   now=self.clock, tz_offset_s=lambda: 0.0,
                                   checks=list(checks) if checks is not None else list(forecast_checks.CHECKS))

    def now(self):
        return self.clock.t

    def _post(self, payload):
        self.posts.append(payload)
        return f"alert-{len(self.posts)}" if self.post_result == "auto" else self.post_result

    def _close(self, alert_id):
        self.closed.append(alert_id)
        return True


@pytest.fixture
def fx():
    return Fx()


def test_merge_new_then_silent_then_bucket_cross_then_severity_rise(fx):
    row = finding(predicted_at=fx.now() + 20 * DAY, severity="info")
    _, rep = fx.f.merge(row, "code")
    assert rep is True
    _, rep = fx.f.merge({**row, "rate": 19.0}, "code")
    assert rep is False
    _, rep = fx.f.merge({**row, "predicted_at": fx.now() + 6 * DAY, "severity": "info"}, "code")
    assert rep is True
    _, rep = fx.f.merge({**row, "predicted_at": fx.now() + 6 * DAY, "severity": "warning"}, "code")
    assert rep is True
    assert len(fx.store.open()) == 1 and fx.store.open()[0]["rate"] == 19.0
    assert fx.store.open()[0]["severity"] == "warning" and fx.store.open()[0]["verified"] == "code"


def test_clear_after_two_clean_runs_closes_alert():
    batches = [[obj(predicted_at=NOW + 10 * DAY)], [], []]
    fx = Fx(checks=[ck(detect=lambda d: batches.pop(0))], alerts=True)
    statuses = []
    for _ in range(3):
        fx.f.run_once()
        statuses.append(fx.store.find("disk_fill|rig|models")["status"])
    assert statuses == ["open", "open", "cleared"] and fx.closed == ["alert-1"]
    row = fx.store.find("disk_fill|rig|models")
    assert row["clean_runs"] == 2 and row["resolved"] == fx.now()
    assert [c["id"] for c in fx.f.view()["cleared"]] == [row["id"]] and fx.f.view()["findings"] == []


def test_collecting_or_failed_check_does_not_clear_its_findings():
    modes = ["find", "collecting", "boom"]

    def detect(d):
        mode = modes.pop(0)
        if mode == "find":
            return [obj(predicted_at=NOW + 10 * DAY)]
        if mode == "collecting":
            raise forecast_checks.Collecting(1.5, 3.0)
        raise RuntimeError("history unavailable")

    fx = Fx(checks=[ck(detect=detect)])
    out = [fx.f.run_once() for _ in range(3)]
    assert [o["checks"]["disk_fill"]["state"] for o in out] == ["ok", "collecting", "failed"]
    assert out[1]["checks"]["disk_fill"]["have_days"] == 1.5 and out[1]["checks"]["disk_fill"]["min_days"] == 3.0
    row = fx.store.find("disk_fill|rig|models")
    assert row["status"] == "open" and row["clean_runs"] == 0 and fx.closed == []


def test_dismissed_stays_dismissed_until_severity_rises(fx):
    row = finding(severity="info", predicted_at=NOW + 20 * DAY)
    stored, _ = fx.f.merge(row, "code")
    assert fx.f.dismiss(stored["id"], "alice") is True
    gone = fx.store.get(stored["id"])
    assert gone["status"] == "dismissed" and gone["dismissed_by"] == "alice" and gone["resolved"] == fx.now()
    again, rep = fx.f.merge({**row, "rate": 21.0}, "code")
    assert rep is False and again["status"] == "dismissed" and again["rate"] == 21.0
    up, rep = fx.f.merge({**row, "severity": "critical"}, "code")
    assert rep is True and up["status"] == "open"
    assert fx.f.dismiss("nope", "alice") is False and fx.f.dismiss(stored["id"], "alice") is True
    assert [a["action"] for a in fx.audits] == ["forecast.dismiss", "forecast.dismiss"]


def test_reopened_dismissed_finding_drops_its_closed_alert():
    batches = [[obj(predicted_at=NOW + 10 * DAY)]]
    fx = Fx(checks=[ck(detect=lambda d: batches.pop(0) if batches else [])], alerts=True)
    fx.f.run_once()
    fid = fx.store.find("disk_fill|rig|models")["id"]
    assert fx.store.get(fid)["alert_id"] == "alert-1" and len(fx.posts) == 1
    assert fx.f.dismiss(fid, "alice") is True and fx.closed == ["alert-1"]
    up, rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical", confidence="low"), "code")
    assert rep is True and up["status"] == "open" and up["alert_id"] is None and up["last_reported"] is None
    assert len(fx.posts) == 1
    fx.f.run_once()
    fx.f.run_once()
    assert fx.store.get(fid)["status"] == "cleared" and fx.closed == ["alert-1"]
    up, _ = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical"), "code")
    assert up["status"] == "open" and up["alert_id"] == "alert-2" and len(fx.posts) == 2


def test_dismiss_during_a_clearing_run_is_not_overwritten():
    batches = [[obj(predicted_at=NOW + 10 * DAY)]]
    fx = Fx(checks=[ck(detect=lambda d: batches.pop(0) if batches else [])], alerts=True, store_cls=RacingStore)
    fx.f.run_once()
    fx.f.run_once()
    fid = fx.store.find("disk_fill|rig|models")["id"]
    assert fx.store.get(fid)["clean_runs"] == 1
    fx.store.hook = lambda rows: fx.f.dismiss(rows[0]["id"], "alice")
    fx.f.run_once()
    row = fx.store.get(fid)
    assert row["status"] == "dismissed" and row["dismissed_by"] == "alice" and row["clean_runs"] == 1
    assert row["resolved"] == fx.now() and fx.closed == ["alert-1"]


def test_concurrent_run_now_submits_one_job():
    fx = Fx()
    fx.f.ensure_schedule()
    gate = threading.Barrier(2)
    queued, refused = [], []

    def go():
        gate.wait(5.0)
        try:
            queued.append(fx.f.run_now("alice", "operator"))
        except jobs.JobError as e:
            refused.append(e.status)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)
    assert len(queued) == 1 and refused == [409]
    assert [r["id"] for r in fx.svc.list(status="live", kind=forecast.KIND)
            if not r["spec"]["periodic"]] == [queued[0]["id"]]


def test_finding_view_hides_internal_columns(fx):
    fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    row = fx.f.view()["findings"][0]
    assert set(row) == {"id", "check", "title", "host", "subject", "severity", "summary", "detail", "since",
                        "predicted_at", "rate", "unit", "confidence", "suggested_action", "graph", "verified",
                        "status", "first_seen", "last_seen", "resolved", "thread_id", "dismissed_by"}
    assert row["check"] == "disk_fill" and row["title"] == "Disk fill" and row["graph"] == {"source": "system"}


def test_alert_gates():
    off = Fx(alerts=False)
    off.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert off.posts == []
    floor = Fx(alerts=True, alert_min_severity="critical")
    floor.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert floor.posts == []
    weak = Fx(alerts=True)
    weak.f.merge(finding(predicted_at=NOW + 10 * DAY, confidence="low"), "code")
    assert weak.posts == []
    model = Fx(alerts=True)
    model.f.merge(finding(predicted_at=NOW + 10 * DAY), "model")
    assert model.posts == []
    on = Fx(alerts=True)
    stored, rep = on.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert rep is True
    assert on.posts == [{"name": "Forecast: Disk fill", "source": "forecast", "metric": "disk_fill/models",
                         "host": "rig", "severity": "warning", "value": 19.0, "threshold": 0,
                         "message": "Disk is filling"}]
    assert stored["alert_id"] == "alert-1" and stored["last_reported"] == on.now()
    # the alarm engine prefixes the source, and the watcher still knows the alert as Forecast's own
    assert tower_watch._own_alert({"metric": "forecast/" + on.posts[0]["metric"]}) is True


def test_an_alert_closes_when_its_finding_stops_passing_the_gate():
    fx = Fx(alerts=True)
    stored, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert stored["alert_id"] == "alert-1" and stored["gate_streak"] == 1
    down, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="info"), "code")
    assert down["alert_id"] is None and fx.closed == ["alert-1"] and len(fx.posts) == 1
    # last_reported is kept, so a flapping gate cannot re-post every run
    assert down["last_reported"] == fx.now() and down["gate_streak"] == 0
    # a severity rise is not enough on its own: the dropped alert waits out REALERT_RUNS passing runs
    back, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert back["alert_id"] is None and len(fx.posts) == 1 and back["gate_streak"] == 1
    again, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert again["alert_id"] == "alert-2" and len(fx.posts) == 2


def test_a_finding_wobbling_across_the_gate_posts_once_then_waits_out_the_streak():
    """The reviewer's repro: a disk ETA around 14 days flipping warning/info every run."""
    fx = Fx(alerts=True)
    for i in range(8):
        severity = "warning" if i % 2 == 0 else "info"
        fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity=severity), "code")
    assert len(fx.posts) == 1 and fx.closed == ["alert-1"]
    for _ in range(2):
        fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert len(fx.posts) == 2 and fx.closed == ["alert-1"]


def test_an_escalation_closes_the_live_alert_and_raises_a_new_one():
    """The alarm engine never re-grades a live alert, so warning → critical → warning → critical posts twice."""
    fx = Fx(alerts=True)
    first, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert first["alert_id"] == "alert-1" and first["alert_severity"] == "warning"
    up, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical"), "code")
    assert up["alert_id"] == "alert-2" and up["alert_severity"] == "critical" and fx.closed == ["alert-1"]
    assert fx.posts[-1]["severity"] == "critical"
    down, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert down["alert_id"] == "alert-2" and down["alert_severity"] == "critical"
    assert len(fx.posts) == 2 and fx.closed == ["alert-1"]           # a downgrade leaves the alert alone
    again, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical"), "code")
    assert again["alert_id"] == "alert-2" and len(fx.posts) == 2 and fx.closed == ["alert-1"]


def test_a_close_that_fails_keeps_the_alert_id_for_the_next_run():
    fx = Fx(alerts=True)
    stored, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert stored["alert_id"] == "alert-1"
    fails = {"v": True}

    def close(alert_id):
        fx.closed.append(alert_id)
        return not fails["v"]

    fx.f._close = close
    assert fx.f.dismiss(stored["id"], "alice") is True
    kept = fx.store.get(stored["id"])
    assert kept["status"] == "dismissed" and kept["alert_id"] == "alert-1" and fx.closed == ["alert-1"]
    fails["v"] = False
    fx.f.run_once()
    done = fx.store.get(stored["id"])
    assert done["alert_id"] is None and done["alert_severity"] is None
    assert fx.closed == ["alert-1", "alert-1"]


def test_a_failed_close_at_dismiss_reopen_never_double_posts():
    """#1031 bug repro: a reopen must not wipe alert_id when the alarm engine is still down."""
    fx = Fx(alerts=True)
    stored, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert stored["alert_id"] == "alert-1"
    fx.f._close = lambda aid: fx.closed.append(aid) or False
    assert fx.f.dismiss(stored["id"], "alice") is True
    assert fx.store.get(stored["id"])["alert_id"] == "alert-1"
    up, rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical"), "code")
    assert rep is True and up["status"] == "open"
    assert up["alert_id"] == "alert-1"
    assert len(fx.posts) == 1


def test_a_close_that_succeeds_at_reopen_time_reports_normally():
    fx = Fx(alerts=True)
    stored, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert stored["alert_id"] == "alert-1"
    fx.f._close = lambda aid: fx.closed.append(aid) or False
    assert fx.f.dismiss(stored["id"], "alice") is True
    assert fx.store.get(stored["id"])["alert_id"] == "alert-1"
    fx.f._close = lambda aid: fx.closed.append(aid) or True
    up, rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical"), "code")
    assert rep is True and up["status"] == "open"
    assert up["alert_id"] == "alert-2" and len(fx.posts) == 2
    assert fx.closed == ["alert-1", "alert-1"]


def test_a_failed_close_when_a_cleared_finding_returns_never_double_posts():
    batches = [[obj(predicted_at=NOW + 10 * DAY)], [], []]
    fx = Fx(checks=[ck(detect=lambda d: batches.pop(0))], alerts=True)
    fx.f._close = lambda aid: fx.closed.append(aid) or False
    for _ in range(3):
        fx.f.run_once()
    row = fx.store.find("disk_fill|rig|models")
    assert row["status"] == "cleared" and row["alert_id"] == "alert-1"
    assert len(fx.posts) == 1
    up, rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical"), "code")
    assert rep is True and up["status"] == "open" and up["alert_id"] == "alert-1"
    assert len(fx.posts) == 1


def test_an_escalation_whose_close_fails_posts_once_the_next_run_closes():
    fx = Fx(alerts=True)
    stored, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="warning"), "code")
    assert stored["alert_id"] == "alert-1"
    fx.f._close = lambda aid: fx.closed.append(aid) or False
    up, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical"), "code")
    assert up["alert_id"] == "alert-1" and up["alert_severity"] == "warning"
    assert len(fx.posts) == 1
    fx.f._close = lambda aid: fx.closed.append(aid) or True
    again, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, severity="critical"), "code")
    assert again["alert_id"] == "alert-2" and len(fx.posts) == 2
    assert fx.closed == ["alert-1", "alert-1"]


def test_a_failed_close_leaves_a_dropped_alert_to_be_closed_again():
    fx = Fx(alerts=True)
    stored, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert stored["alert_id"] == "alert-1"
    fails = {"v": True}
    fx.f._close = lambda aid: fx.closed.append(aid) or (not fails["v"])
    down, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, confidence="low"), "code")
    assert down["alert_id"] == "alert-1" and down["gate_streak"] == 0
    fails["v"] = False
    again, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, confidence="low"), "code")
    assert again["alert_id"] is None and again["alert_severity"] is None
    assert fx.closed == ["alert-1", "alert-1"]


def test_a_flapping_gate_does_not_re_post_an_alert_every_run():
    fx = Fx(alerts=True)
    for i in range(6):
        confidence = "high" if i % 2 == 0 else "low"
        fx.f.merge(finding(predicted_at=NOW + 10 * DAY, confidence=confidence), "code")
    assert len(fx.posts) == 1 and fx.closed == ["alert-1"]
    assert fx.store.find("disk_fill|rig|models")["alert_id"] is None


def test_a_finding_is_re_alerted_after_two_runs_back_inside_the_gate():
    fx = Fx(alerts=True)
    stored, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY, confidence="low"), "code")
    assert fx.posts == [] and stored["alert_id"] is None
    first, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert fx.posts == [] and first["gate_streak"] == 1
    second, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert len(fx.posts) == 1 and second["alert_id"] == "alert-1" and second["gate_streak"] == 2
    fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert len(fx.posts) == 1                                        # the live alert is not re-posted


def test_switching_alerts_off_closes_the_alerts_already_raised_and_re_alerts_after_two_runs():
    fx = Fx(alerts=True)
    stored, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert stored["alert_id"] == "alert-1"
    fx.cfg.alerts = False
    again, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert again["alert_id"] is None and fx.closed == ["alert-1"] and len(fx.posts) == 1
    fx.cfg.alert_min_severity = "critical"
    fx.cfg.alerts = True
    fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert len(fx.posts) == 1
    fx.cfg.alert_min_severity = "warning"
    fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert len(fx.posts) == 1
    back, _rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert len(fx.posts) == 2 and back["alert_id"] == "alert-2"


def test_failed_alert_post_retries_next_run():
    fx = Fx(alerts=True)
    fx.post_result = None
    stored, _ = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert len(fx.posts) == 1 and stored["last_reported"] is None and stored["alert_id"] is None
    stored, rep = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert rep is False and len(fx.posts) == 2 and stored["last_reported"] is None
    fx.post_result = "auto"
    stored, _ = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert len(fx.posts) == 3 and stored["alert_id"] == "alert-3" and stored["last_reported"] == fx.now()
    stored, _ = fx.f.merge(finding(predicted_at=NOW + 10 * DAY), "code")
    assert len(fx.posts) == 3


def test_run_once_states_and_audit():
    def collecting(d):
        raise forecast_checks.Collecting(2.0, 7.0)

    def boom(d):
        raise RuntimeError("no history")

    checks = [ck("disk_fill", lambda d: [obj(predicted_at=NOW + 2 * DAY, severity="critical")]),
              ck("thermal_trend", collecting, min_days=7.0), ck("throughput", boom), ck("idle_waste", lambda d: [])]
    fx = Fx(checks=checks, checks_disabled=["idle_waste"])
    out = fx.f.run_once()
    assert out["mode"] == "code" and out["found"] == 1 and out["reported"] == 1
    assert {k: v["state"] for k, v in out["checks"].items()} == {"disk_fill": "ok", "thermal_trend": "collecting",
                                                                 "throughput": "failed", "idle_waste": "off"}
    assert out["checks"]["thermal_trend"]["have_days"] == 2.0 and out["checks"]["thermal_trend"]["min_days"] == 7.0
    assert out["checks"]["disk_fill"]["found"] == 1
    run = fx.store.last_run()
    assert run["mode"] == "code" and run["found"] == 1 and run["checks"]["disk_fill"]["state"] == "ok"
    ev = [a for a in fx.audits if a["action"] == "forecast.run"]
    assert len(ev) == 1 and ev[0]["actor"] == "forecast" and ev[0]["target"] == "code" and ev[0]["ok"] is True
    assert ev[0]["detail"]["found"] == 1 and ev[0]["detail"]["failed"] == 1 and "ms" in ev[0]["detail"]


def test_run_stops_between_checks_when_cancelled_or_disabled():
    seen = []
    checks = [ck("disk_fill", lambda d: seen.append("a") or []), ck("throughput", lambda d: seen.append("b") or [])]
    fx = Fx(checks=checks)
    out = fx.f.run_once(cancelled=lambda: len(seen) >= 1)
    assert seen == ["a"] and out["checks"]["throughput"]["state"] == "pending"
    seen.clear()
    fx2 = Fx(checks=checks)

    def first(d):
        seen.append("a")
        fx2.cfg.enabled = False
        return []
    fx2.f = forecast.Forecast(fx2.svc, fx2.store, cfg=lambda: fx2.cfg,
                              data_factory=lambda now, window_s: forecast_checks.CheckData(now=now, window_s=window_s),
                              alert_post=fx2._post, alert_close=fx2._close, audit=fx2.audits.append,
                              now=fx2.clock, tz_offset_s=lambda: 0.0,
                              checks=[ck("disk_fill", first), checks[1]])
    out = fx2.f.run_once()
    assert seen == ["a"] and out["checks"]["throughput"]["state"] == "pending"


def test_digest_waits_for_the_next_slot_after_the_first_run():
    """NOW is Friday 12:00; the digest day is Monday 08:00, so the first run must not write one."""
    clock = Clock(NOW)
    fx = Fx(checks=[ck("weekly_digest", lambda d: [], title="Weekly digest", min_days=0.0)], clock=clock,
            digest_day="mon", digest_at="08:00")
    slot = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc).timestamp()
    assert fx.f.run_once()["checks"]["weekly_digest"]["state"] == "off"
    assert fx.store.last_run()["digest_at"] == slot
    clock.t = NOW + 2 * DAY
    assert fx.f.run_once()["checks"]["weekly_digest"]["state"] == "off"
    assert fx.store.last_run()["digest_at"] == slot
    clock.t = NOW + 7 * DAY
    assert fx.f.run_once()["checks"]["weekly_digest"]["state"] == "ok"
    next_slot = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc).timestamp()
    assert fx.store.last_run()["digest_at"] == next_slot
    assert fx.f.run_once()["checks"]["weekly_digest"]["state"] == "off"
    assert fx.store.last_run()["digest_at"] == next_slot


def test_digest_slot_uses_the_local_weekday_and_hour():
    off = 2 * 3600.0
    monday = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc).timestamp()   # Monday 08:00 local (UTC+2)
    for at in (monday, monday + 5 * 3600, monday + 6 * DAY):
        assert forecast.digest_slot(at, "mon", "08:00", off) == monday
    assert forecast.digest_slot(monday - 60, "mon", "08:00", off) == monday - 7 * DAY
    assert forecast.digest_slot(monday + 7 * DAY, "mon", "08:00", off) == monday + 7 * DAY


def S(every, hours=8, day="mon", at="03:00"):
    return forecast.Schedule(every, hours, day, at)


def test_custom_periods_count_from_the_time_of_day_and_stay_put():
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc).timestamp()
    eight = forecast.next_slot(now, S("custom", hours=8, at="02:30"), 0)
    assert eight == datetime(2026, 9, 18, 18, 30, tzinfo=timezone.utc).timestamp()
    two_days = S("custom", hours=48)
    first = forecast.next_slot(now, two_days, 0)
    assert first > now and (first - 3 * 3600.0) % (48 * 3600.0) == 0
    assert forecast.next_slot(now + DAY, two_days, 0) in (first, first + 2 * DAY)
    assert forecast.next_slot(first - 1, two_days, 0) == first and forecast.next_slot(first, two_days, 0) == first + 2 * DAY
    assert two_days.period_s == 48 * 3600.0 and two_days.spec() == {"every": "custom", "at": "03:00", "hours": 48}


def test_schedule_reads_the_settings_and_tolerates_bad_values():
    class Cfg:
        every, every_hours, run_day, at = "weekly", 9999, "sun", "7:05"
    assert forecast.Schedule.of(Cfg) == forecast.Schedule("weekly", 720, "sun", "07:05")
    class Bad:
        every, every_hours, run_day, at = "5m", None, "someday", "25:99"
    assert forecast.Schedule.of(Bad) == forecast.Schedule("daily", 8, "mon", "03:00")


def test_next_slot_alignment():
    now = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(now, S("daily"), 0) == datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(now, S("weekly"), 0) == datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(now, S("6h"), 0) == datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(now, S("1h"), 0) == now + 3600.0
    assert forecast.next_slot(now, S("1h", at="03:20"), 0) == datetime(2026, 9, 18, 12, 20, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(now, S("weekly", day="sun", at="02:30"), 0) == datetime(2026, 9, 20, 2, 30, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(now, S("daily"), 7200.0) == datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc).timestamp()
    early = datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(early, S("weekly"), 0) == datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc).timestamp()
    on_slot = datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(on_slot, S("daily"), 0) == datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc).timestamp()
    monday = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc).timestamp()
    assert forecast.next_slot(monday, S("weekly"), 0) == datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc).timestamp()


def test_ensure_schedule_idempotent_period_change_and_disable(fx):
    fx.f.ensure_schedule()
    live = fx.svc.list(status="live", kind=forecast.KIND)
    assert len(live) == 1 and live[0]["period_s"] == 86400.0 and live[0]["source"] == "system"
    assert live[0]["spec"] == {"periodic": True, "every": "daily", "at": "03:00"}
    assert live[0]["not_before"] == forecast.next_slot(fx.now(), S("daily"), 0)
    fx.f.ensure_schedule()
    assert [r["id"] for r in fx.svc.list(status="live", kind=forecast.KIND)] == [live[0]["id"]]
    fx.cfg.every = "6h"
    fx.f.ensure_schedule()
    after = fx.svc.list(status="live", kind=forecast.KIND)
    assert len(after) == 1 and after[0]["period_s"] == 21600.0 and after[0]["id"] != live[0]["id"]
    assert fx.svc.get(live[0]["id"])["status"] == "cancelled"
    fx.cfg.enabled = False
    fx.f.ensure_schedule()
    assert fx.svc.list(status="live", kind=forecast.KIND) == []


def test_ensure_schedule_realigns_on_hour_change_and_spares_a_running_job(fx):
    fx.f.ensure_schedule()
    first = fx.svc.list(status="live", kind=forecast.KIND)[0]
    fx.cfg.at = "05:00"
    fx.f.ensure_schedule()
    live = fx.svc.list(status="live", kind=forecast.KIND)
    assert len(live) == 1 and live[0]["id"] != first["id"] and fx.svc.get(first["id"])["status"] == "cancelled"
    assert live[0]["spec"] == {"periodic": True, "every": "daily", "at": "05:00"}
    assert live[0]["not_before"] == datetime(2026, 9, 19, 5, 0, tzinfo=timezone.utc).timestamp()
    fx.cfg.every = "6h"
    fx.f.ensure_schedule()
    six = fx.svc.list(status="live", kind=forecast.KIND)[0]
    assert six["period_s"] == 21600.0 and six["not_before"] == forecast.next_slot(fx.now(), S("6h", at="05:00"), 0)
    fx.cfg.at = "09:00"
    fx.f.ensure_schedule()
    moved = fx.svc.list(status="live", kind=forecast.KIND)
    assert len(moved) == 1 and moved[0]["id"] != six["id"] and moved[0]["spec"]["at"] == "09:00"
    six = moved[0]
    fx.svc._store.update(six["id"], status="running", started=fx.now())
    fx.cfg.every = "daily"
    fx.f.ensure_schedule()
    assert [r["id"] for r in fx.svc.list(status="live", kind=forecast.KIND)] == [six["id"]]
    assert fx.svc.get(six["id"])["status"] == "running"
    fx.svc._store.update(six["id"], status="done", resolved=fx.now())
    fx.f.ensure_schedule()
    live = fx.svc.list(status="live", kind=forecast.KIND)
    assert len(live) == 1 and live[0]["spec"] == {"periodic": True, "every": "daily", "at": "09:00"}
    assert live[0]["not_before"] == datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc).timestamp()


def test_ensure_schedule_realigns_a_periodic_job_that_drifted_off_its_slot(fx):
    fx.f.ensure_schedule()
    job = fx.svc.list(status="live", kind=forecast.KIND)[0]
    slot = forecast.next_slot(fx.now(), S("daily"), 0)
    assert job["next_run"] == slot
    fx.svc._store.update(job["id"], next_run=slot + forecast.DRIFT_S + 60.0)
    fx.f.ensure_schedule()
    live = fx.svc.list(status="live", kind=forecast.KIND)
    assert len(live) == 1 and live[0]["id"] != job["id"] and live[0]["next_run"] == slot
    assert fx.svc.get(job["id"])["status"] == "cancelled"
    fx.f.ensure_schedule()
    assert [r["id"] for r in fx.svc.list(status="live", kind=forecast.KIND)] == [live[0]["id"]]


def test_ensure_schedule_leaves_a_small_drift_and_a_running_job_alone_and_realigns_a_fixed_period(fx):
    fx.f.ensure_schedule()
    job = fx.svc.list(status="live", kind=forecast.KIND)[0]
    slot = forecast.next_slot(fx.now(), S("daily"), 0)
    fx.svc._store.update(job["id"], next_run=slot + forecast.DRIFT_S - 1.0)
    fx.f.ensure_schedule()
    assert [r["id"] for r in fx.svc.list(status="live", kind=forecast.KIND)] == [job["id"]]
    fx.svc._store.update(job["id"], next_run=slot + 4 * 3600.0, status="running", started=fx.now())
    fx.f.ensure_schedule()
    assert [r["id"] for r in fx.svc.list(status="live", kind=forecast.KIND)] == [job["id"]]
    fx.svc._store.update(job["id"], status="done", resolved=fx.now())
    fx.cfg.every = "6h"
    fx.f.ensure_schedule()
    six = fx.svc.list(status="live", kind=forecast.KIND)[0]
    fx.svc._store.update(six["id"], next_run=fx.now() + 99999.0)
    fx.f.ensure_schedule()
    again = fx.svc.list(status="live", kind=forecast.KIND)
    assert len(again) == 1 and again[0]["id"] != six["id"]
    assert again[0]["not_before"] == forecast.next_slot(fx.now(), S("6h"), 0)


def test_disabling_cancels_even_a_running_periodic_job(fx):
    fx.f.ensure_schedule()
    per = fx.svc.list(status="live", kind=forecast.KIND)[0]
    fx.svc._store.update(per["id"], status="running", started=fx.now())
    fx.cfg.enabled = False
    fx.f.ensure_schedule()
    assert fx.svc.get(per["id"])["status"] == "cancelled"
    assert fx.svc.list(status="live", kind=forecast.KIND) == []


def test_run_now_conflicts(fx):
    fx.cfg.enabled = False
    with pytest.raises(jobs.JobError) as e:
        fx.f.run_now("alice", "operator")
    assert e.value.status == 409 and "off" in e.value.message
    fx.cfg.enabled = True
    fx.f.ensure_schedule()
    row = fx.f.run_now("alice", "operator")
    assert row["status"] == "queued" and row["source"] == "ui" and row["user"] == "alice"
    assert row["spec"] == {"periodic": False} and row["exclusive"] == ["forecast"]
    with pytest.raises(jobs.JobError) as e:
        fx.f.run_now("alice", "operator")
    assert e.value.status == 409 and "in progress" in e.value.message
    fx.svc.cancel(row["id"], actor="alice")
    per = [r for r in fx.svc.list(status="live", kind=forecast.KIND) if r["spec"].get("periodic")][0]
    fx.svc._store.update(per["id"], status="running", started=fx.now())
    with pytest.raises(jobs.JobError) as e:
        fx.f.run_now("alice", "operator")
    assert e.value.status == 409


@pytest.fixture
def client():
    fx = Fx(checks=[ck(detect=lambda d: [obj(predicted_at=NOW + 10 * DAY)])])
    app = Flask(__name__)
    app.secret_key = "t"
    app.config["TESTING"] = True
    forecast.register_routes(app, fx.f, role_of=lambda: session.get("role"), user_of=lambda: session.get("user") or "")
    c = app.test_client()
    c.fx = fx
    return c


def _as(c, role, user="alice"):
    with c.session_transaction() as s:
        s["role"], s["user"] = role, user


def test_routes_roles(client):
    assert client.get("/api/forecast").status_code == 401
    _as(client, "viewer")
    body = client.get("/api/forecast").get_json()
    assert body["ok"] is True and body["enabled"] is True and body["running"] is False
    assert body["last_run"] is None and body["findings"] == [] and body["window_days"] == 14
    assert [c["state"] for c in body["checks"]] == ["pending"] and body["checks"][0]["title"] == "Disk fill"
    assert client.post("/api/forecast/run").status_code == 403
    _as(client, "operator")
    r = client.post("/api/forecast/run")
    assert r.status_code == 202 and r.get_json()["job_id"]
    assert client.post("/api/forecast/run").status_code == 409
    client.fx.f.run_once()
    fid = client.fx.store.open()[0]["id"]
    body = client.get("/api/forecast").get_json()
    assert [f["id"] for f in body["findings"]] == [fid] and body["mode"] == "code" and body["last_run"] == NOW
    _as(client, "viewer")
    assert client.post(f"/api/forecast/{fid}/dismiss").status_code == 403
    _as(client, "operator")
    assert client.post(f"/api/forecast/{fid}/dismiss").status_code == 200
    assert client.post(f"/api/forecast/{fid}/dismiss").status_code == 404
    assert client.post("/api/forecast/nope/dismiss").status_code == 404


def test_tower_pass_sets_mode_and_verified():
    calls = []

    def tower(check, data, code_findings, thread_id):
        calls.append((check.id, len(code_findings), thread_id))
        return [({**code_findings[0], "summary": "Tower text"}, "tower+code")]

    tower.start_thread = lambda label: "thread-1"
    tower.end_thread = lambda tid: calls.append(("end", tid, None))
    fx = Fx(checks=[ck(detect=lambda d: [obj(predicted_at=NOW + 10 * DAY)])], tower_effort="full")
    fx.f = forecast.Forecast(fx.svc, fx.store, cfg=lambda: fx.cfg,
                             data_factory=lambda now, window_s: forecast_checks.CheckData(now=now, window_s=window_s),
                             alert_post=fx._post, alert_close=fx._close, audit=fx.audits.append, tower_pass=tower,
                             now=fx.clock, tz_offset_s=lambda: 0.0,
                             checks=[ck(detect=lambda d: [obj(predicted_at=NOW + 10 * DAY)])])
    out = fx.f.run_once()
    assert out["mode"] == "tower" and calls[0] == ("disk_fill", 1, "thread-1") and calls[-1][0] == "end"
    row = fx.store.find("disk_fill|rig|models")
    assert row["summary"] == "Tower text" and row["verified"] == "tower+code" and row["thread_id"] == "thread-1"
    view = fx.f.tower_view()
    assert view["enabled"] is True and view["checks"] == [{"title": "Disk fill", "state": "ok"}]
    assert view["findings"] == [{"host": "rig", "check": "Disk fill", "severity": "warning", "summary": "Tower text",
                                 "predicted": "2026-09-28", "verified": "tower+code"}]


def test_job_kind_runs_through_the_service(fx):
    assert fx.svc.kind(forecast.KIND) is not None and fx.svc.kind(forecast.KIND).title == "Forecast run"
    fx.cfg.checks_disabled = list(forecast_checks.IDS)
    row = fx.f.run_now("alice", "operator")
    assert fx.svc.tick() == 1
    done = fx.svc.get(row["id"])
    assert done["status"] == "done" and done["message"] == "0 found, 0 new"
    assert done["result"]["mode"] == "code" and done["result"]["checks"]["disk_fill"]["state"] == "off"


def test_init_tables_migrates_an_older_database_and_repeats_cleanly():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("""
        CREATE TABLE forecast_findings (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, check_id TEXT NOT NULL, host TEXT, subject TEXT,
            severity TEXT NOT NULL, summary TEXT, detail TEXT, since REAL, predicted_at REAL, rate REAL, unit TEXT,
            confidence TEXT, suggested_action TEXT, graph TEXT, verified TEXT, status TEXT NOT NULL,
            first_seen REAL, last_seen REAL, last_reported REAL, clean_runs INTEGER NOT NULL DEFAULT 0,
            thread_id TEXT, alert_id TEXT, dismissed_by TEXT, resolved REAL);
        CREATE TABLE forecast_runs (id TEXT PRIMARY KEY, started REAL, finished REAL, mode TEXT, checks TEXT,
            found INTEGER, reported INTEGER, message TEXT, digest_at REAL, digest TEXT, tier TEXT, tier_reason TEXT,
            model TEXT, tok_s REAL, timed_out INTEGER);
        INSERT INTO forecast_findings (id, fingerprint, check_id, severity, status)
            VALUES ('x', 'disk_fill|rig|models', 'disk_fill', 'warning', 'open');
        INSERT INTO forecast_runs (id, started, tier, model) VALUES ('r1', 1.0, 'full', 'qwen3-9b');
    """)
    forecast.init_tables(conn)
    forecast.init_tables(conn)                                       # a second boot must be a no-op
    store = forecast.Store(lambda: conn)
    assert store.open()[0]["gate_streak"] == 0 and store.open()[0]["clean_runs"] == 0
    assert store.open()[0]["alert_severity"] is None
    assert "alert_severity" in {r[1] for r in conn.execute("PRAGMA table_info(forecast_findings)")}
    assert store.recent_runs(5)[0]["tower_calls"] is None
    assert {r[1] for r in conn.execute("PRAGMA table_info(forecast_runs)")} >= set(forecast.Store.RUN_KEYS)
    assert "gate_streak" in {r[1] for r in conn.execute("PRAGMA table_info(forecast_findings)")}


def test_bucket_and_sort_put_undated_findings_last(fx):
    assert forecast.bucket(NOW + 0.5 * DAY, NOW) == 0 and forecast.bucket(NOW + 2 * DAY, NOW) == 1
    assert forecast.bucket(NOW + 8 * DAY, NOW) == 3 and forecast.bucket(NOW + 60 * DAY, NOW) == 4
    assert forecast.bucket(None, NOW) == len(forecast.BUCKETS)
    fx.f.merge(finding(subject="a", severity="info", predicted_at=NOW + 2 * DAY), "code")
    fx.f.merge(finding(subject="b", severity="critical", predicted_at=None), "code")
    fx.f.merge(finding(subject="c", severity="critical", predicted_at=NOW + 9 * DAY), "code")
    assert [f["subject"] for f in fx.f.view()["findings"]] == ["c", "b", "a"]




# ── Tower effort (#1031 rework): the tier drives the run ──
class FakeTower:
    """A Tower pass that records what the run asked of it at the pinned tier."""
    def __init__(self, digest_out=None, causes_out=None, eligible=True, model="qwen3-9b",
                 tok_s=None, timed_out=False):
        self.digest_out, self.causes_out, self._eligible = digest_out, causes_out, eligible
        self._model, self._tok, self._timed_out = model, tok_s, timed_out
        self.labels, self.ended, self.checks, self.digests, self.caused, self.profiles = [], [], [], [], [], []

    def eligible(self):
        return self._eligible

    def model_id(self):
        return self._model

    def begin_run(self, profile=None):
        self.profiles.append(profile)

    def model_calls(self):
        """Counted the way the real pass counts: the digest, each cause batch and each conversation turn."""
        return len(self.digests) + len(self.caused) + len(self.checks)

    def measured_tok_s(self):
        return self._tok

    def timed_out(self):
        return self._timed_out

    def start_thread(self, label):
        self.labels.append(label)
        return f"t{len(self.labels)}"

    def end_thread(self, tid):
        self.ended.append(tid)

    def __call__(self, check, data, code_findings, thread_id):
        self.checks.append((check.id, len(code_findings), thread_id))
        return [(dict(r), "tower+code") for r in code_findings]

    def digest(self, rows):
        self.digests.append([dict(r) for r in rows])
        return self.digest_out

    def causes(self, rows):
        self.caused.append([dict(r) for r in rows])
        return dict(self.causes_out or {})


def _with_tower(fx, tower, checks, signals=None):
    fx.f = forecast.Forecast(fx.svc, fx.store, cfg=lambda: fx.cfg,
                             data_factory=lambda now, window_s: forecast_checks.CheckData(now=now, window_s=window_s),
                             alert_post=fx._post, alert_close=fx._close, audit=fx.audits.append, tower_pass=tower,
                             model_signals=signals, now=fx.clock, tz_offset_s=lambda: 0.0, checks=checks)
    return fx.f


def _one_check():
    return [ck("disk_fill", lambda d: [obj(predicted_at=NOW + 10 * DAY)])]


def test_off_asks_the_model_for_nothing():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="off")
    tw = FakeTower(digest_out="never", causes_out={"x": {"detail": "never"}})
    _with_tower(fx, tw, checks)
    out = fx.f.run_once()
    assert out["mode"] == "code" and tw.checks == [] and tw.digests == [] and tw.caused == []
    assert tw.profiles == [forecast_effort.PROFILES["off"]]
    run = fx.store.last_run()
    assert run["tier"] == "off" and fx.f.view()["tower"] is None


def test_an_ineligible_tower_falls_back_to_code():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="full")
    tw = FakeTower(digest_out="never", eligible=False)
    _with_tower(fx, tw, checks)
    assert fx.f.run_once()["mode"] == "code"
    assert tw.digests == [] and tw.checks == [] and fx.store.last_run()["tier"] == "off"


def test_light_runs_the_digest_only():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="light")
    tw = FakeTower(digest_out="Disk is the one to watch.", causes_out={"x": {"detail": "never"}})
    _with_tower(fx, tw, checks)
    out = fx.f.run_once()
    assert out["mode"] == "tower" and tw.checks == [] and tw.caused == [] and tw.labels == []
    assert len(tw.digests) == 1 and all(r.get("title") for r in tw.digests[0])
    run = fx.store.last_run()
    assert run["digest"] == "Disk is the one to watch." and run["tier"] == "light"
    view = fx.f.view()
    assert view["digest"] == "Disk is the one to watch."
    assert view["tower"] == {"tier": "light", "reason": "Set to Light in Settings", "model": "qwen3-9b"}


def test_standard_runs_the_digest_and_a_cause_per_finding():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="standard")
    fx.f = _with_tower(fx, FakeTower(), checks)
    fx.f.run_once()
    fid = fx.store.find("disk_fill|rig|models")["id"]
    checks2 = _one_check()
    fx2 = Fx(checks=checks2, tower_effort="standard")
    tw = FakeTower(digest_out="Steady week.", causes_out={fid: {"detail": "d Likely cause: heavy nightly model pulls"}})
    _with_tower(fx2, tw, checks2)
    fx2.f.run_once()
    row = fx2.store.find("disk_fill|rig|models")
    assert len(tw.digests) == 1 and len(tw.caused) == 1 and tw.checks == []
    # the fixtures share no ids, so this run's own row keeps the code text
    assert row["verified"] == "code" and row["suggested_action"] == "free space"
    tw2 = FakeTower(digest_out=None, causes_out={row["id"]: {"detail": "d Likely cause: heavy nightly model pulls"}})
    _with_tower(fx2, tw2, checks2)
    out = fx2.f.run_once()
    row = fx2.store.find("disk_fill|rig|models")
    assert out["mode"] == "tower" and row["verified"] == "tower+code"
    assert row["detail"] == "d Likely cause: heavy nightly model pulls" and row["summary"] == "Disk is filling"
    assert row["suggested_action"] == "free space" and row["rate"] == 19.0


def test_full_investigates_each_flagged_check_and_still_writes_the_digest():
    checks = [ck("disk_fill", lambda d: [obj(predicted_at=NOW + 10 * DAY)]),
              ck("throughput", lambda d: []),
              ck("thermal_trend", lambda d: [obj(check="thermal_trend", subject="gpu", predicted_at=NOW + 5 * DAY)])]
    fx = Fx(checks=checks, tower_effort="full")
    tw = FakeTower(digest_out="Two hosts to watch.")
    _with_tower(fx, tw, checks)
    out = fx.f.run_once()
    assert out["mode"] == "tower"
    assert [c[0] for c in tw.checks] == ["disk_fill", "thermal_trend"]
    assert [c[2] for c in tw.checks] == ["t1", "t2"] and tw.ended == ["t1", "t2"]
    assert tw.labels == ["Forecast 2026-09-18 · Disk fill", "Forecast 2026-09-18 · Thermal trend"]
    assert fx.store.find("disk_fill|rig|models")["thread_id"] == "t1"
    assert len(tw.digests) == 1 and fx.store.last_run()["digest"] == "Two hosts to watch."
    # every row the conversation already explained is left out, so no cause call is made at all
    assert tw.caused == []


def test_full_never_investigates_the_weekly_digest():
    """The digest only restates findings Tower has already been given, so it gets no turn of its own."""
    def detect(d):
        return [obj(check="weekly_digest", host=None, subject="2026-W38", severity="info", summary="week so far")]

    checks = [ck("weekly_digest", detect, title="Weekly digest", min_days=0.0),
              ck("disk_fill", lambda d: [obj(predicted_at=NOW + 10 * DAY)])]
    fx = Fx(checks=checks, tower_effort="full", digest_day="fri", digest_at="08:00")
    tw = FakeTower(digest_out="Steady week.")
    _with_tower(fx, tw, checks)
    fx.f.run_once()                                  # baseline run: the digest slot has not passed
    fx.clock.t = NOW + 7 * DAY
    fx.f.run_once()
    assert [c[0] for c in tw.checks] == ["disk_fill", "disk_fill"]
    assert tw.labels == ["Forecast 2026-09-18 · Disk fill", "Forecast 2026-09-25 · Disk fill"]
    row = fx.store.find("weekly_digest|-|2026-W38")
    assert row["status"] == "open" and row["verified"] == "code" and row["summary"] == "week so far"


def test_a_cancelled_run_asks_the_model_for_no_digest_or_cause():
    checks = [ck("disk_fill", lambda d: [obj(predicted_at=NOW + 10 * DAY)]),
              ck("throughput", lambda d: [])]
    fx = Fx(checks=checks, tower_effort="standard")
    tw = FakeTower(digest_out="Steady week.")
    _with_tower(fx, tw, checks)
    out = fx.f.run_once(cancelled=lambda: True)
    assert out["mode"] == "code" and tw.digests == [] and tw.caused == []
    fx2 = Fx(checks=checks, tower_effort="standard")
    tw2 = FakeTower(digest_out="Steady week.")
    _with_tower(fx2, tw2, checks)

    def off(d):
        fx2.cfg.enabled = False
        return [obj(predicted_at=NOW + 10 * DAY)]

    fx2.f._checks = [ck("disk_fill", off)]
    assert fx2.f.run_once()["mode"] == "code" and tw2.digests == []


def test_full_stops_starting_new_turns_once_the_run_is_nearly_out_of_time():
    clock = Clock(NOW)

    def slow(cid):
        def detect(d):
            clock.t += forecast.TURN_DEADLINE * forecast.MAX_RUN_S / 2.4
            return [obj(check=cid, subject=cid, predicted_at=NOW + 10 * DAY)]
        return detect

    ids = ("disk_fill", "thermal_trend", "throughput")
    checks = [ck(cid, slow(cid), min_days=0.0) for cid in ids]
    fx = Fx(checks=checks, clock=clock, tower_effort="full")
    tw = FakeTower(digest_out=None)
    _with_tower(fx, tw, checks)
    out = fx.f.run_once()
    # the first two checks start inside the deadline; the third is past it and stays code-only
    assert [c[0] for c in tw.checks] == ["disk_fill", "thermal_trend"]
    assert out["checks"]["throughput"]["state"] == "ok"
    assert fx.store.find("throughput|rig|throughput")["verified"] == "code"
    assert fx.store.find("disk_fill|rig|disk_fill")["verified"] == "tower+code"


def test_the_codes_advice_is_byte_identical_after_every_tier():
    for tier in ("off", "light", "standard", "full"):
        checks = _one_check()
        fx = Fx(checks=checks, tower_effort=tier)
        tw = FakeTower(digest_out="Steady week.")
        _with_tower(fx, tw, checks)
        fx.f.run_once()
        row = fx.store.find("disk_fill|rig|models")
        assert row["suggested_action"] == "free space", tier
        assert row["summary"] == "Disk is filling" and row["rate"] == 19.0


def test_auto_picks_the_tier_from_the_models_signals():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    tw = FakeTower(digest_out="Steady week.")
    _with_tower(fx, tw, checks, signals=lambda m: {"score_pct": None, "size_b": 9.0,
                                                   "tool_grade": "native", "tok_s": None})
    fx.f.run_once()
    run = fx.store.last_run()
    assert run["tier"] == "light" and run["model"] == "qwen3-9b"
    assert run["tier_reason"] == "Light — 9B model, no evaluation yet, tool check passed"
    assert tw.checks == [] and tw.caused == []


def test_a_broken_signals_callable_still_runs_at_the_lowest_tier():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    tw = FakeTower(digest_out="Steady week.")

    def boom(model):
        raise RuntimeError("eval store down")

    _with_tower(fx, tw, checks, signals=boom)
    assert fx.f.run_once()["mode"] == "tower"
    assert fx.store.last_run()["tier"] == "light"


STRONG = {"score_pct": 93.0, "size_b": 32.0, "tool_grade": "native", "tok_s": 40.0}


def _run_at(fx, checks, *, timed_out=False, model="qwen3-9b", tok_s=None, signals=None, quiet=False):
    """One more run with a fresh pass, so each run's tier is picked from the stored history.
    A quiet run finds nothing, so no model call is made."""
    tw = FakeTower(digest_out="Steady week.", timed_out=timed_out, model=model, tok_s=tok_s)
    _with_tower(fx, tw, [ck("disk_fill", lambda d: [])] if quiet else checks, signals=signals or (lambda m: dict(STRONG)))
    fx.clock.t += 3600.0
    fx.f.run_once()
    return fx.store.last_run(), tw


def test_a_run_that_timed_out_lowers_the_next_auto_tier():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    first, tw = _run_at(fx, checks, timed_out=True, tok_s=31.5)
    assert first["tier"] == "full" and first["timed_out"] == 1 and first["tok_s"] == 31.5
    assert tw.profiles == [forecast_effort.PROFILES["full"]]
    after, tw2 = _run_at(fx, checks)
    assert after["tier"] == "standard" and after["tier_reason"].endswith("lowered after a timeout")
    assert after["timed_out"] == 0 and tw2.checks == []
    # a different model does not inherit the earlier timeout
    other, _tw = _run_at(fx, checks, model="other-32b")
    assert other["tier"] == "full"


def test_the_lowered_tier_is_held_for_three_good_runs_then_restored():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    assert _run_at(fx, checks, timed_out=True)[0]["tier"] == "full"
    assert [_run_at(fx, checks)[0]["tier"] for _ in range(3)] == ["standard", "standard", "standard"]
    assert _run_at(fx, checks)[0]["tier"] == "full"


def test_a_second_timeout_during_probation_restarts_the_count():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    _run_at(fx, checks, timed_out=True)                              # full timed out → standard
    assert _run_at(fx, checks)[0]["tier"] == "standard"
    assert _run_at(fx, checks, timed_out=True)[0]["tier"] == "standard"   # and this one timed out too
    assert [_run_at(fx, checks)[0]["tier"] for _ in range(3)] == ["light", "light", "light"]
    assert _run_at(fx, checks)[0]["tier"] == "full"


def test_a_quiet_run_that_called_the_model_at_all_never_lifts_the_cap():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    assert _run_at(fx, checks, timed_out=True)[0]["tier"] == "full"
    for _ in range(3):
        row, tw = _run_at(fx, checks, quiet=True)
        assert row["tier"] == "standard" and row["tower_calls"] == 0 and tw.digests == []
    assert _run_at(fx, checks)[0]["tier"] == "standard"              # still capped after three quiet runs
    assert [_run_at(fx, checks)[0]["tier"] for _ in range(2)] == ["standard", "standard"]
    assert _run_at(fx, checks)[0]["tier"] == "full"


def test_quiet_runs_between_good_ones_do_not_reset_the_count():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    _run_at(fx, checks, timed_out=True)
    for _ in range(2):                                               # two runs that called the model, each
        assert _run_at(fx, checks)[0]["tier"] == "standard"          # followed by one that did not
        assert _run_at(fx, checks, quiet=True)[0]["tier"] == "standard"
    assert _run_at(fx, checks)[0]["tier"] == "standard"              # the third good run lifts the cap
    assert _run_at(fx, checks, quiet=True)[0]["tier"] == "full"


def test_a_run_that_called_the_model_records_how_many_times():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="standard")
    _run_at(fx, checks)
    assert fx.store.last_run()["tower_calls"] == 2                   # the digest plus one cause batch
    fx.cfg.tower_effort = "light"
    _run_at(fx, checks)
    assert fx.store.last_run()["tower_calls"] == 1


def test_a_run_with_tower_off_neither_lifts_nor_clears_the_cap():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    _run_at(fx, checks, timed_out=True)
    fx.cfg.tower_effort = "off"
    for _ in range(3):
        assert _run_at(fx, checks)[0]["tier"] == "off"
    fx.cfg.tower_effort = "auto"
    assert _run_at(fx, checks)[0]["tier"] == "standard"


def test_the_runs_own_speed_beats_the_bench_table_next_run():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="auto")
    seen = []

    def signals(model):
        seen.append(model)
        return {"score_pct": 93.0, "size_b": 32.0, "tool_grade": "native", "tok_s": 40.0}

    tw = FakeTower(digest_out="Steady week.", tok_s=6.0)
    _with_tower(fx, tw, checks, signals=signals)
    fx.f.run_once()
    assert fx.store.last_run()["tier"] == "full" and seen == ["qwen3-9b"]
    tw2 = FakeTower(digest_out="Steady week.")
    _with_tower(fx, tw2, checks, signals=signals)
    fx.f.run_once()
    assert fx.store.last_run()["tier"] == "light"


def test_only_full_keeps_the_findings_only_tower_saw():
    for tier, status in (("light", "cleared"), ("standard", "cleared"), ("full", "open")):
        checks = [ck("disk_fill", lambda d: [])]
        fx = Fx(checks=checks, tower_effort=tier)
        tw = FakeTower(digest_out=None)
        _with_tower(fx, tw, checks)
        stored, _ = fx.f.merge(finding(subject="echo", predicted_at=NOW + 10 * DAY), "model")
        keep, _ = fx.f.merge(finding(subject="real", predicted_at=NOW + 10 * DAY), "code")
        fx.f.run_once()
        assert fx.store.get(stored["id"])["status"] == status, tier
        assert fx.store.get(keep["id"])["status"] == "open" and fx.closed == []


def test_a_new_weekly_digest_supersedes_the_older_open_ones():
    def detect(d):
        return [obj(check="weekly_digest", host=None, subject=forecast._iso_week(d.now, 0.0), severity="info")]

    checks = [ck("weekly_digest", detect, title="Weekly digest", min_days=0.0)]
    fx = Fx(checks=checks, digest_day="fri", digest_at="08:00")
    fx.f.run_once()                                  # baseline run: the next digest slot has not passed yet
    fx.clock.t = NOW + 7 * DAY
    week = forecast._iso_week(fx.clock.t, 0.0)
    old, _ = fx.f.merge(finding(check="weekly_digest", host="rig", subject=week, severity="info",
                                summary="an earlier roll-up"), "code")
    other, _ = fx.f.merge(finding(subject="real", predicted_at=NOW + 10 * DAY), "code")
    assert fx.f.run_once()["checks"]["weekly_digest"]["state"] == "ok"
    assert fx.store.get(old["id"])["status"] == "cleared"
    assert fx.store.get(other["id"])["status"] == "open"
    fresh = [r for r in fx.store.open() if r["check_id"] == "weekly_digest"]
    assert [(r["host"], r["subject"]) for r in fresh] == [(None, week)]


def test_a_weekly_digest_from_another_week_is_cleared_at_the_start_of_a_run():
    fx = Fx(checks=[ck("disk_fill", lambda d: [])])
    stale, _ = fx.f.merge(finding(check="weekly_digest", host=None, subject="2026-W37", severity="info"), "code")
    this, _ = fx.f.merge(finding(check="weekly_digest", host=None, subject="2026-W38", severity="info"), "code")
    fx.f.run_once()
    assert fx.store.get(stale["id"])["status"] == "cleared" and fx.store.get(stale["id"])["resolved"] == fx.now()
    assert fx.store.get(this["id"])["status"] == "open"


def test_a_stale_weekly_digest_alert_is_closed_with_its_row():
    fx = Fx(checks=[ck("disk_fill", lambda d: [])], alerts=True, alert_min_severity="info")
    stale, _ = fx.f.merge(finding(check="weekly_digest", host=None, subject="2026-W37", severity="warning"), "code")
    assert stale["alert_id"] == "alert-1"
    fx.f.run_once()
    assert fx.store.get(stale["id"])["status"] == "cleared" and fx.closed == ["alert-1"]


def test_the_runs_tier_is_read_once_and_pinned_for_the_pass():
    fx = Fx(tower_effort="light")

    def flip(d):
        fx.cfg.tower_effort = "full"
        return [obj(predicted_at=NOW + 10 * DAY)]

    checks = [ck("disk_fill", flip)]
    tw = FakeTower(digest_out="Steady week.")
    _with_tower(fx, tw, checks)
    out = fx.f.run_once()
    assert tw.profiles == [forecast_effort.PROFILES["light"]] and tw.checks == []
    assert out["mode"] == "tower" and fx.store.last_run()["tier"] == "light"


def test_a_tower_call_that_raises_never_fails_the_run():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="standard")
    tw = FakeTower()

    def boom(rows):
        raise RuntimeError("gateway gone")

    tw.digest = boom
    tw.causes = boom
    _with_tower(fx, tw, checks)
    out = fx.f.run_once()
    assert out["mode"] == "code" and fx.store.find("disk_fill|rig|models")["verified"] == "code"


def test_causes_never_double_the_cause_over_two_runs():
    checks = _one_check()
    fx = Fx(checks=checks, tower_effort="standard")
    tw = FakeTower()
    _with_tower(fx, tw, checks)

    def causes(rows):
        tw.caused.append([dict(r) for r in rows])
        return {rows[0]["id"]: forecast_tower.wording(rows[0], {"cause": "heavy nightly model pulls"})} if rows else {}

    tw.causes = causes
    fx.f.run_once()
    fx.f.run_once()
    row = fx.store.find("disk_fill|rig|models")
    assert len(tw.caused) == 2 and row["verified"] == "tower+code"
    assert row["detail"].count(forecast_tower.CAUSE) == 1 and row["detail"] == "d Likely cause: heavy nightly model pulls"
