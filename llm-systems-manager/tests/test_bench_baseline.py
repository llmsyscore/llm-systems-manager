"""#882: pinned-baseline watcher — due math, build trigger, regression alert."""
from __future__ import annotations

import json
import sqlite3
from datetime import timezone

import bench_baseline as bb
import bench_live as bl

UTC = timezone.utc
AID = "a" * 32
MODEL = "org/m:Q4"
PIN = "run-pin"


def _doc(run_id, pred, ok=True):
    return {"run_id": run_id, "model_id": MODEL, "ok": ok,
            "config": {"bench": "throughput_1k", "osl": 1024, "concurrency": [1, 2]},
            "levels": [{"level": 1, "concurrency": 1, "wall_s": 5.0, "rows": [],
                        "all": {"pred_tps": pred, "prompt_tps": 400.0, "latency_s": 10.0, "accept_rate": None,
                                "agg_pred_tps": pred, "completion_tokens": 100}}]}


def _seed(db_path, run_id=PIN, pred=50.0, baseline=1, ts="2026-09-01T00:00:00+00:00"):
    conn = sqlite3.connect(db_path)
    bl.init_table(conn)
    doc = _doc(run_id, pred)
    conn.execute("INSERT OR IGNORE INTO bench_live_runs (run_id, model_id, agent_id, ts, ok, baseline, gen_tps, config_json, result_json)"
                 " VALUES (?,?,?,?,1,?,?,?,?)", (run_id, MODEL, AID, ts, baseline, pred, json.dumps(doc["config"]), json.dumps(doc)))
    conn.commit(); conn.close()


class Env:
    def __init__(self, tmp_path):
        self.db = str(tmp_path / "t.db")
        _seed(self.db)
        self.cfg = {"enabled": True, "nightly_at": "03:00", "on_build_change": True, "regression_pct": 15.0}
        self.hosts = [{"agent_id": AID, "hostname": "alpha", "online": True, "model": MODEL, "state": "awake"}]
        self.build = "b100-aaa"
        self.started, self.alerts, self.points = [], [], []
        self.refuse = False
        self.t = 1_800_000_000.0  # 2027-01-19 (UTC); slot maths use tz=UTC
        self.w = bb.Watcher(db_path=self.db, cfg=lambda: dict(self.cfg), fleet_hosts=lambda: list(self.hosts),
                            run_on_agent=self._run, llama_build_of=lambda aid: self.build,
                            alert=lambda p: self.alerts.append(p) or True, push_metrics=self.points.extend,
                            now=lambda: self.t, tz=UTC)

    def _run(self, aid, body):
        self.started.append((aid, body))
        if self.refuse:
            return False, "Another benchmark or autotune is in progress"
        return True, f"run-{len(self.started)}"

    def store(self, run_id, pred, ok=True):
        conn = sqlite3.connect(self.db)
        doc = _doc(run_id, pred, ok)
        conn.execute("INSERT INTO bench_live_runs (run_id, model_id, agent_id, ts, ok, baseline, gen_tps, config_json, result_json)"
                     " VALUES (?,?,?,?,?,0,?,?,?)", (run_id, MODEL, AID, "2027-01-19T03:10:00+00:00", 1 if ok else 0, pred,
                                                    json.dumps(doc["config"]), json.dumps(doc)))
        conn.commit(); conn.close()


def test_hhmm_and_slots():
    assert bb.parse_hhmm("03:00") == (3, 0)
    assert bb.parse_hhmm("3:5") == (3, 5)
    assert bb.parse_hhmm("") is None and bb.parse_hhmm("25:00") is None and bb.parse_hhmm("x") is None
    now = 1_800_000_000.0
    from datetime import datetime
    today = datetime.fromtimestamp(now, UTC)
    assert bb.slot_ts(now, "03:00", tz=UTC) == today.replace(hour=3, minute=0, second=0, microsecond=0).timestamp()
    assert bb.next_slot_ts(now, "03:00", tz=UTC) == bb.slot_ts(now, "03:00", tz=UTC) + 86400
    assert bb.next_slot_ts(now, "23:00", tz=UTC) == bb.slot_ts(now, "23:00", tz=UTC)
    assert bb.slot_ts(now, "", tz=UTC) is None


def test_slot_ts_default_tz_is_local():
    from datetime import datetime
    now = 1_800_000_000.0
    want = datetime.fromtimestamp(now).replace(hour=3, minute=0, second=0, microsecond=0).timestamp()
    assert bb.slot_ts(now, "03:00") == want


def test_nightly_due():
    now = 1_800_000_000.0
    slot = bb.slot_ts(now, "03:00", tz=UTC)
    assert bb.nightly_due(now, "03:00", None, tz=UTC)
    assert bb.nightly_due(now, "03:00", slot - 1, tz=UTC)
    assert not bb.nightly_due(now, "03:00", slot + 1, tz=UTC)
    assert not bb.nightly_due(slot - 10, "03:00", None, tz=UTC)
    assert not bb.nightly_due(now, "", None, tz=UTC)
    assert not bb.nightly_due(now, "nope", None, tz=UTC)


def test_metric_tag():
    assert bb._metric_tag("org/m:Q4") == "m:Q4"
    assert bb._metric_tag("weird name/ünïts!") == "_n_ts_"
    assert bb._metric_tag("a" * 100) == ("a" * 64)
    assert bb._metric_tag("") == ""


def test_delta_and_severity():
    assert bb.delta_pct(40.0, 50.0) == -20.0
    assert bb.delta_pct(None, 50.0) is None and bb.delta_pct(40.0, 0) is None
    assert bb.severity(-20.0, 15.0) == "warning"
    assert bb.severity(-30.0, 15.0) == "critical"
    assert bb.severity(-14.9, 15.0) is None and bb.severity(5.0, 15.0) is None and bb.severity(None, 15.0) is None


def test_nightly_check_regression_alerts(tmp_path):
    e = Env(tmp_path)
    e.w.tick()
    assert len(e.started) == 1
    aid, body = e.started[0]
    assert aid == AID and body["model_id"] == MODEL and body["baseline_run_id"] == PIN and body["bench"] == "throughput_1k"
    snap = e.w.snapshot()["baselines"][0]
    assert snap["running"] is True and snap["pending"] == "nightly"
    e.w.tick()
    assert len(e.started) == 1  # no double start while running
    e.store("run-1", 40.0)
    e.w.tick()
    row = e.w.snapshot()["baselines"][0]
    assert row["running"] is False and row["last_check"]["status"] == "regressed"
    assert row["last_check"]["delta_pct"] == -20.0 and row["last_check"]["severity"] == "warning"
    assert row["last_check"]["trigger"] == "nightly" and row["last_check"]["llama_build"] == "b100-aaa"
    assert len(e.alerts) == 1
    a = e.alerts[0]
    assert a["severity"] == "warning" and a["source"] == "benchmark" and a["metric"] == "decode_tps:m:Q4"
    assert a["host"] == "alpha" and a["value"] == 40.0 and a["threshold"] == 42.5
    assert MODEL in a["message"] and "-20 %" in a["message"]
    names = sorted(p["metric_name"] for p in e.points)
    assert names == ["baseline_delta_pct:m:Q4", "decode_tps:m:Q4"]
    assert all(p["source"] == "benchmark" and p["hostname"] == "alpha" for p in e.points)
    assert all(isinstance(p["timestamp"], str) for p in e.points)
    e.w.tick()
    assert len(e.started) == 1  # today's slot already checked


def test_ok_check_no_alert_and_history(tmp_path):
    e = Env(tmp_path)
    e.w.tick(); e.store("run-1", 49.0); e.w.tick()
    row = e.w.snapshot()["baselines"][0]
    assert row["last_check"]["status"] == "ok" and row["last_check"]["severity"] is None
    assert e.alerts == [] and len(e.points) == 2
    assert e.w.history(PIN)[0]["run_id"] == "run-1"


def test_failed_run_no_alert(tmp_path):
    e = Env(tmp_path)
    e.w.tick(); e.store("run-1", None, ok=False); e.w.tick()
    assert e.w.snapshot()["baselines"][0]["last_check"]["status"] == "failed"
    assert e.alerts == []


def test_build_change_triggers_when_nightly_done(tmp_path):
    e = Env(tmp_path)
    e.w.tick(); e.store("run-1", 50.0); e.w.tick()
    e.build = "b101-bbb"
    e.w.tick()
    assert len(e.started) == 2
    assert e.w.snapshot()["baselines"][0]["pending"] == "build"
    e.store("run-2", 20.0); e.w.tick()
    last = e.w.snapshot()["baselines"][0]["last_check"]
    assert last["trigger"] == "build" and last["status"] == "regressed" and last["severity"] == "critical"
    assert "b100-aaa" in e.alerts[0]["message"] and "b101-bbb" in e.alerts[0]["message"]
    assert e.w.snapshot()["baselines"][0]["build_changed"] is False


def test_build_changed_survives_a_skipped_check(tmp_path):
    e = Env(tmp_path)
    e.w.tick(); e.store("run-1", 20.0); e.w.tick()  # regressed check recorded on build b100-aaa
    assert e.w.snapshot()["baselines"][0]["last_check"]["status"] == "regressed"
    e.build = "b101-bbb"
    e.hosts[0]["model"] = "other"  # forces the next check to be skipped, not ok/regressed
    e.w.recheck(PIN); e.w.tick()
    row = e.w.snapshot()["baselines"][0]
    assert row["last_check"]["status"] == "skipped"
    assert row["build_changed"] is True


def test_build_change_ignored_when_disabled(tmp_path):
    e = Env(tmp_path)
    e.w.tick(); e.store("run-1", 50.0); e.w.tick()
    e.cfg["on_build_change"] = False
    e.build = "b101-bbb"
    e.w.tick()
    assert len(e.started) == 1
    assert e.w.snapshot()["baselines"][0]["build_changed"] is True


def test_scheduler_off_manual_recheck_still_runs(tmp_path):
    e = Env(tmp_path)
    e.cfg["enabled"] = False
    e.w.tick()
    assert e.started == []
    assert e.w.recheck(PIN) == {"ok": True, "queued": [PIN], "skipped": []}
    e.w.tick()
    assert len(e.started) == 1 and e.w.snapshot()["baselines"][0]["pending"] == "manual"
    assert e.w.recheck("nope")["skipped"] == [{"run_id": "nope", "reason": "not a pinned baseline"}]
    assert e.w.recheck(PIN)["skipped"] == [{"run_id": PIN, "reason": "check already running"}]


def test_not_loaded_records_skipped(tmp_path):
    e = Env(tmp_path)
    e.hosts[0]["model"] = "other"
    e.w.tick()
    assert e.started == []
    last = e.w.snapshot()["baselines"][0]["last_check"]
    assert last["status"] == "skipped" and last["error"] == "model not loaded on the host"
    e.w.tick()
    assert e.started == []  # counted as today's automatic attempt
    e.hosts[0]["online"] = False
    e.w.recheck(PIN); e.w.tick()
    assert e.w.snapshot()["baselines"][0]["last_check"]["error"] == "host offline"


def test_busy_refusal_never_consumes_an_attempt(tmp_path):
    e = Env(tmp_path)
    e.refuse = True
    e.w.tick()
    assert len(e.started) == 1
    e.w.tick()
    assert len(e.started) == 1  # waiting RETRY_S
    e.t += bb.RETRY_S + 1; e.w.tick()
    assert len(e.started) == 2
    e.t += bb.RETRY_S + 1; e.w.tick()
    assert len(e.started) == 3
    e.t += bb.RETRY_S + 1; e.w.tick()
    assert len(e.started) == 4  # three busy refusals so far, none recorded failed
    assert e.w.snapshot()["baselines"][0]["last_check"] is None
    assert e.alerts == []
    for _ in range(bb.MAX_BUSY):
        e.t += bb.RETRY_S + 1; e.w.tick()
    last = e.w.snapshot()["baselines"][0]["last_check"]
    assert last["status"] == "skipped" and last["error"] == "host busy all day"
    assert len(e.started) == bb.MAX_BUSY and e.w.snapshot()["baselines"][0]["pending"] is None


def test_other_failures_still_count_toward_max_attempts(tmp_path):
    e = Env(tmp_path)

    def boom(aid, body):
        e.started.append((aid, body))
        return False, "agent unreachable"

    e.w._run = boom
    e.w.tick()
    assert len(e.started) == 1
    e.t += bb.RETRY_S + 1; e.w.tick()
    assert len(e.started) == 2
    e.t += bb.RETRY_S + 1; e.w.tick()
    assert len(e.started) == 3
    last = e.w.snapshot()["baselines"][0]["last_check"]
    assert last["status"] == "failed" and last["error"] == "agent unreachable"
    assert e.alerts == []


def test_no_stored_config_records_skipped(tmp_path):
    e = Env(tmp_path)
    conn = sqlite3.connect(e.db)
    conn.execute("UPDATE bench_live_runs SET config_json = '{}'"); conn.commit(); conn.close()
    e.w.tick()
    assert e.started == []
    last = e.w.snapshot()["baselines"][0]["last_check"]
    assert last["status"] == "skipped" and last["error"] == "baseline has no stored config"


def test_timeout_marks_failed(tmp_path):
    e = Env(tmp_path)
    e.w.tick()
    e.t += bb.CHECK_MAX_WAIT_S + 1; e.w.tick()
    last = e.w.snapshot()["baselines"][0]["last_check"]
    assert last["status"] == "failed" and last["error"] == "timed out"


def test_snapshot_shape_and_schedule(tmp_path):
    e = Env(tmp_path)
    s = e.w.snapshot()
    b = s["baselines"][0]
    assert b["hostname"] == "alpha" and b["loaded"] is True and b["llama_build"] == "b100-aaa"
    assert b["last_check"] is None and b["pending"] is None and b["config"]["bench"] == "throughput_1k"
    assert b["config"]["concurrency"] == [1, 2] and b["active_run_id"] is None
    assert s["schedule"]["next_nightly_ts"] == bb.slot_ts(e.t, "03:00", tz=UTC) + 86400
    assert s["schedule"]["nightly_valid"] is True
    e.cfg["nightly_at"] = "bad"
    assert e.w.snapshot()["schedule"]["nightly_valid"] is False
    e.cfg["nightly_at"] = ""
    e.w.tick()
    assert e.started == []


def test_active_run_id_set_while_running(tmp_path):
    e = Env(tmp_path)
    e.w.tick()
    row = e.w.snapshot()["baselines"][0]
    assert row["running"] is True and row["active_run_id"] == "run-1"
    e.store("run-1", 49.0)
    e.w.tick()
    row = e.w.snapshot()["baselines"][0]
    assert row["running"] is False and row["active_run_id"] is None


def test_unpinned_baseline_drops_out(tmp_path):
    e = Env(tmp_path)
    conn = sqlite3.connect(e.db); conn.execute("UPDATE bench_live_runs SET baseline = 0"); conn.commit(); conn.close()
    assert e.w.snapshot()["baselines"] == []


def test_hosts_for_helper():
    rows = [{"agent_id": "x" * 32, "hostname": "zed", "online": True, "model": MODEL, "state": "awake"},
            {"agent_id": "y" * 32, "hostname": "amy", "online": True, "model": MODEL, "state": "sleeping"}]
    out = bl.hosts_for(rows, MODEL)
    assert [r["hostname"] for r in out] == ["zed", "amy"] and out[0]["loaded"] and not out[1]["loaded"]


def test_build_change_setdefault_preserves_manual_pending(tmp_path):
    e = Env(tmp_path)
    e.cfg["enabled"] = False
    e.w.tick()  # records b100-aaa as the agent's seen build; nothing queued (disabled)
    e.w.recheck(PIN)  # queue a manual check
    e.build = "b101-bbb"
    e.cfg["enabled"] = True  # would fire the build-change branch if not guarded
    e.w.tick()
    assert len(e.started) == 1
    aid, body = e.started[0]
    assert aid == AID and body["baseline_run_id"] == PIN
    e.store("run-1", 50.0)
    e.w.tick()
    last = e.w.snapshot()["baselines"][0]["last_check"]
    assert last["trigger"] == "manual"  # setdefault must not have overwritten it with "build"


def test_build_change_during_active_check_runs_after(tmp_path):
    e = Env(tmp_path)
    e.w.tick()  # starts the nightly check -> active
    assert len(e.started) == 1
    e.build = "b101-bbb"
    e.w.tick()  # build change detected while active: must not be dropped
    assert len(e.started) == 1  # still active, not double-started
    snap = e.w.snapshot()["baselines"][0]
    assert snap["running"] is True
    e.store("run-1", 50.0)
    e.w.tick()  # nightly check finishes, then the queued build check starts
    assert len(e.started) == 2
    aid, body = e.started[1]
    assert aid == AID and body["baseline_run_id"] == PIN
    assert e.w.snapshot()["baselines"][0]["pending"] == "build"


def test_tick_survives_fleet_hosts_error(tmp_path):
    e = Env(tmp_path)

    def boom():
        raise RuntimeError("fleet unavailable")

    w = bb.Watcher(db_path=e.db, cfg=lambda: dict(e.cfg), fleet_hosts=boom,
                   run_on_agent=e._run, llama_build_of=lambda aid: e.build,
                   alert=lambda p: e.alerts.append(p) or True, push_metrics=e.points.extend,
                   now=lambda: e.t, tz=UTC)
    w.tick()  # must not raise
    assert e.started == []


def test_recheck_skips_when_pending_is_starting(tmp_path):
    e = Env(tmp_path)
    entry = {"trigger": "nightly", "attempts": 0, "retry_at": 0.0, "build_from": "", "starting": True}
    e.w._pending[PIN] = entry
    result = e.w.recheck(PIN)
    assert result == {"ok": True, "queued": [], "skipped": [{"run_id": PIN, "reason": "check already running"}]}
    assert e.w._pending[PIN] is entry  # not overwritten by the manual recheck
