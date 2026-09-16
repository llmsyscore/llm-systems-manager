"""#1029 Tower timers: spec validation, limits, ticks and samples, the report turn, cancel, restart."""
from __future__ import annotations

import json
import sqlite3
import types

import jobs
import tower
import tower_timers as tm
import tower_tools as tt


def _cfg(**over):
    d = dict(enabled=True, model="", tool_mode="prompt", capabilities="read", off_topic="refuse",
             disabled_tools=[], max_tool_calls=3, max_tokens=256, temperature=0.2, history_days=30)
    d.update(over)
    return types.SimpleNamespace(**d)


class _Hosts:
    """host_detail dep whose ram_pct climbs one point per read."""
    def __init__(self):
        self.reads = 0

    def __call__(self, name, section="all"):
        if name != "box":
            return None
        self.reads += 1
        return {"hostname": "box", "online": True, "ram_pct": 40 + self.reads, "cpu_pct": 5, "gpu_temp_c": 60,
                "live": {"ram": {"used_pct": 40 + self.reads}}}


class _DeadHosts:
    """host_detail dep that answers for "box" until `dead` is flipped, then every read errors."""
    def __init__(self):
        self.dead = False

    def __call__(self, name, section="all"):
        if self.dead or name != "box":
            return None
        return {"hostname": "box", "online": True, "ram_pct": 41, "cpu_pct": 5, "gpu_temp_c": 60}


def _deps(hosts=None):
    return {"host": hosts or _Hosts(), "host_history": lambda h, m, w="24h", a=None: {"points": 0},
            "hosts": lambda *a, **k: [{"hostname": "box", "ram_pct": 41}], "models": lambda h=None, p=None: [],
            "alarms": lambda s="active", c=10, w=None, h=None, r=None: [{"id": "a1"}],
            "alert": lambda a: None, "alarm_search": lambda a: {"alerts": [], "total": 0, "offset": 0, "next_offset": None},
            "alarm_history": lambda w="30d", g="rule", t=10, h=None, r=None, a=None: {"total": 1}, "energy": lambda w="today", a=None: {},
            "flow": lambda: {}, "runs": lambda t=None, c=5, a=None: [], "speed": lambda m: [], "health": lambda: {},
            "log_tail": lambda h, p="llama", n=40, a=None: [], "config_get": lambda p: {}, "help": lambda t: "",
            "audit": lambda w="24h", a=None, ac=None, c=20, x=None: []}


class _Runs:
    def __init__(self, err=None, ticks_ok=True):
        self.err, self.ticks_ok, self.started, self.ticks, self.stopped = err, ticks_ok, [], [], []
        self.on_start = None

    def stop(self, rid, user):
        self.stopped.append((rid, user))
        return True

    def count_tick(self, user):
        self.ticks.append(user)
        return self.ticks_ok

    def start(self, **kw):
        self.started.append(kw)
        if self.on_start:
            self.on_start()
        return (None, self.err) if self.err else (f"r{len(self.started)}", None)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _timers(cfg=None, runs=None, clock=None, deps=None, audit=None, alerts=None):
    clock = clock or _Clock()
    st = tower.Store(":memory:")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    jobs.init_table(conn)
    svc = jobs.Service(jobs.Store(lambda: conn), cfg=lambda: types.SimpleNamespace(workers=4, history_days=30),
                       alert=(alerts.append if alerts is not None else None), audit=audit, now=clock, inline=True)
    reg = tt.build_registry(deps or _deps())
    t = tm.Timers(svc, store=st, registry_factory=lambda: reg, cfg=lambda: cfg or _cfg(), audit=audit, now=clock)
    t.runs = runs
    return t, st, svc


def test_timer_spec_metric_mode_labels_and_clamps():
    reg = tt.build_registry(_deps())
    spec, err = tm.timer_spec({"host": "box", "metric": "ram_pct", "every_s": 10, "for_s": 600}, reg, _cfg(), "operator")
    assert err is None
    assert spec["kind"] == "metric" and spec["host"] == "box" and spec["label"] == "ram_pct on box"
    assert spec["every_s"] == 30 and spec["times"] == 20 and "raised to 30" in spec["note"]
    spec, _ = tm.timer_spec({"label": "  RAM   watch ", "host": "box", "metric": "ram_pct", "every_s": 30, "times": 500}, reg, _cfg(), "operator")
    assert spec["label"] == "RAM watch" and spec["times"] == 120 and "capped at 120" in spec["note"]
    spec, _ = tm.timer_spec({"host": "box", "metric": "cpu_pct", "every_s": 600}, reg, _cfg(), "operator")
    assert spec["times"] == 1 and "note" not in spec

def test_timer_spec_rejects_bad_metric_hosts_and_unpollable_tools():
    reg = tt.build_registry(_deps())
    assert tm.timer_spec({"host": "box", "metric": "nope"}, reg, _cfg(), "operator")[1].startswith("metric must be one of")
    assert tm.timer_spec({"host": "all", "metric": "ram_pct"}, reg, _cfg(), "operator")[1] == "metric needs one host"
    assert tm.timer_spec({"metric": "ram_pct"}, reg, _cfg(), "operator")[1] == "metric needs one host"
    assert tm.timer_spec({}, reg, _cfg(), "operator")[1] == "give host and metric, or tool and args"
    for name in ("wait_until", "schedule", "help", "ask_operator", "load_model", "nothing"):
        assert tm.timer_spec({"tool": name}, reg, _cfg(), "operator")[1] == f"{name} cannot be polled"
    assert tm.timer_spec({"tool": "host_detail", "args": {}}, reg, _cfg(), "operator")[1] == "host_detail: host is required"
    spec, err = tm.timer_spec({"tool": "host_detail", "args": {"host": "box", "section": "ram"}, "pick": "ram.used_pct", "every_s": 60}, reg, _cfg(), "operator")
    assert err is None and spec["kind"] == "tool" and spec["args"] == {"host": "box", "section": "ram"}
    assert spec["pick"] == "ram.used_pct" and spec["label"] == "host detail on box"

def test_timer_spec_rejects_an_unknown_host_and_names_the_fleet():
    reg = tt.build_registry(_deps())
    err = tm.timer_spec({"host": "lmstudio", "metric": "ram_pct", "every_s": 30}, reg, _cfg(), "operator")[1]
    assert err == "unknown host: lmstudio; hosts are box"
    err = tm.timer_spec({"tool": "host_detail", "args": {"host": "lmstudio"}, "every_s": 60}, reg, _cfg(), "operator")[1]
    assert err == "unknown host: lmstudio; hosts are box"
    spec, err = tm.timer_spec({"tool": "host_detail", "args": {"host": "box"}, "every_s": 60}, reg, _cfg(), "operator")
    assert err is None and spec["kind"] == "tool"

def test_pick_path_walks_dicts_and_lists():
    obj = {"live": {"ram": {"used_pct": 41}}, "hosts": [{"hostname": "box"}, {"hostname": "mac"}]}
    assert tm.pick_path(obj, "live.ram.used_pct") == 41
    assert tm.pick_path(obj, "hosts.1.hostname") == "mac"
    assert tm.pick_path(obj, "hosts.5.hostname") is None
    assert tm.pick_path(obj, "live.gpu") is None
    assert tm.pick_path(obj, "") == obj

def test_schedule_queues_a_job_one_interval_out():
    audits = []
    clock = _Clock(1000.0)
    timers, st, svc = _timers(clock=clock, audit=audits.append)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 2})
    assert out["ok"] and out["every_s"] == 30 and out["times"] == 2 and out["label"] == "ram_pct on box"
    job = svc.get(out["timer_id"])
    assert job["kind"] == tm.KIND and job["status"] == "queued" and job["next_run"] == 1030.0 and job["thread_id"] == tid
    assert job["state"] == {"samples": [], "phase": "sampling", "ends": 1060.0} and job["source"] == "tower"
    assert [a["action"] for a in audits] == ["jobs.submit", "tower.timer.schedule"]
    assert timers.live_count("alice") == 1 and timers.live_count("bob") == 0
    assert st.get_thread("alice", tid)["updated"] == 1000.0
    view = timers.list("alice")[0]
    assert view["id"] == job["id"] and view["status"] == "queued" and view["count"] == 0 and view["next_in_s"] == 30 and view["left_s"] == 60


def test_ticks_sample_then_report_lands_in_the_thread():
    clock = _Clock(1000.0)
    runs = _Runs()
    timers, st, svc = _timers(clock=clock, runs=runs)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 2})
    clock.t = 1030.0
    svc.tick()
    job = svc.get(out["timer_id"])
    assert job["status"] == "queued" and job["next_run"] == 1060.0 and len(job["state"]["samples"]) == 1
    assert timers.list("alice")[0]["status"] == "running"
    clock.t = 1060.0
    svc.tick()                      # second sample -> phase reporting, re-queued 2 s out
    job = svc.get(out["timer_id"])
    assert job["state"]["phase"] == "reporting" and job["status"] == "queued" and job["next_run"] == 1062.0
    assert timers.list("alice")[0]["status"] == "reporting"
    clock.t = 1062.0
    svc.tick()                      # report turn starts -> done
    job = svc.get(out["timer_id"])
    assert job["status"] == "done" and job["result"]["run_id"] == "r1" and runs.started[0]["thread_id"] == tid
    assert runs.started[0]["text"].startswith(tm.REPORT_PREFIX) and runs.started[0]["prelude"]["result"]["ticks"] == 2
    assert timers.list("alice")[0]["run_id"] == "r1"


def test_report_retries_while_busy_then_fails_after_the_deadline():
    clock = _Clock(1000.0)
    runs = _Runs(err=(409, "busy"))
    timers, st, svc = _timers(clock=clock, runs=runs)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 1})
    clock.t = 1030.0
    svc.tick()
    assert svc.get(out["timer_id"])["state"]["phase"] == "reporting"
    clock.t = 1032.0
    svc.tick()
    assert svc.get(out["timer_id"])["status"] == "queued" and len(runs.started) == 1
    clock.t = 1030.0 + tm.REPORT_RETRY_S + 3
    svc.tick()
    job = svc.get(out["timer_id"])
    assert job["status"] == "failed" and job["message"].startswith("could not report")
    msgs = st.messages(tid)
    assert any(m.get("tool_name") == tm.TICK_TOOL for m in msgs)


def test_cancel_notes_the_thread_and_thread_delete_cancels_timers():
    audits = []
    clock = _Clock(1000.0)
    timers, st, svc = _timers(clock=clock, audit=audits.append)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 3})
    assert timers.cancel(out["timer_id"], "bob") == (None, (404, "unknown timer"))
    row, err = timers.cancel(out["timer_id"], "alice")
    assert err is None and row["status"] == "cancelled"
    note = [r for r in st.messages(tid) if r["role"] == "tool"][-1]
    assert note["tool_name"] == tm.TICK_TOOL and json.loads(note["content"])["status"] == "cancelled"
    assert timers.cancel(out["timer_id"], "alice") == (None, (409, "not live"))
    assert not [a for a in audits if a["action"].startswith("tower.timer.cancel")]
    out2 = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30})
    assert timers.cancel_thread(tid) == 1 and svc.get(out2["timer_id"])["message"] == "the conversation was deleted"


def test_tower_off_or_missing_tool_fails_the_timer():
    clock = _Clock(1000.0)
    cfg = _cfg()
    alerts = []
    timers, st, svc = _timers(clock=clock, cfg=cfg, alerts=alerts)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30})
    cfg.enabled = False
    clock.t = 1030.0
    svc.tick()
    assert svc.get(out["timer_id"])["status"] == "failed" and svc.get(out["timer_id"])["message"] == "Tower was turned off"
    assert alerts == []
    cfg.enabled = True
    disabled = timers.schedule(thread_id=tid, user="alice", role="operator", args={"tool": "alarms", "args": {}, "every_s": 30})
    cfg.disabled_tools = ["alarms"]
    clock.t = 1060.0
    svc.tick()
    assert svc.get(disabled["timer_id"])["message"] == "alarms is no longer available"
    assert [a["message"].endswith("alarms is no longer available") for a in alerts] == [True]


def test_limits_per_user_and_total_count_live_jobs():
    timers, st, _svc = _timers()
    tid = st.create_thread("alice", "t", {})
    assert timers.schedule(thread_id=tid, user="alice", role="operator", args={"metric": "nope", "host": "box"}) == {
        "ok": False, "message": "metric must be one of " + ", ".join(tm.METRICS)}
    for _ in range(tm.MAX_PER_USER):
        assert timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 60})["ok"]
    assert "cancel one first" in timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 60})["message"]
    for i in range(tm.MAX_TOTAL - tm.MAX_PER_USER):
        assert timers.schedule(thread_id=tid, user=f"u{i}", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 60})["ok"]
    assert "try again later" in timers.schedule(thread_id=tid, user="zed", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 60})["message"]


def test_rate_limited_tick_records_an_error_sample():
    clock = _Clock(1000.0)
    hosts = _Hosts()
    timers, st, svc = _timers(clock=clock, runs=_Runs(ticks_ok=False), deps=_deps(hosts))
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 2})
    reads_at_schedule = hosts.reads          # the schedule-time host probe already read once
    clock.t = 1030.0
    svc.tick()
    assert svc.get(out["timer_id"])["state"]["samples"] == [{"t": 1030.0, "error": "rate limited"}]
    assert hosts.reads == reads_at_schedule  # the rate-limited tick itself never reads the host


def test_tool_mode_keeps_the_picked_field_or_a_compact_copy():
    clock = _Clock(1000.0)
    hosts = _Hosts()
    timers, st, svc = _timers(clock=clock, runs=_Runs(), deps=_deps(hosts))
    tid = st.create_thread("alice", "t", {})
    picked = timers.schedule(thread_id=tid, user="alice", role="operator",
                             args={"tool": "host_detail", "args": {"host": "box"}, "pick": "live.ram.used_pct", "every_s": 30})["timer_id"]
    whole = timers.schedule(thread_id=tid, user="alice", role="operator",
                            args={"tool": "alarms", "args": {}, "every_s": 30})["timer_id"]
    missing = timers.schedule(thread_id=tid, user="alice", role="operator",
                              args={"tool": "host_detail", "args": {"host": "box"}, "pick": "live.gpu.temp", "every_s": 30})["timer_id"]
    reads_at_schedule = hosts.reads          # scheduling the two host_detail timers already probed the host
    clock.t = 1030.0
    svc.tick()
    assert svc.get(picked)["state"]["samples"][0]["value"] == 40 + reads_at_schedule + 1
    whole_sample = svc.get(whole)["state"]["samples"][0]["value"]
    assert isinstance(whole_sample, (dict, list)) and len(json.dumps(whole_sample)) <= tm.SAMPLE_CHARS + 40
    assert svc.get(missing)["state"]["samples"][0] == {"t": 1030.0, "error": "live.gpu.temp not in the result"}


def test_unknown_host_is_rejected_at_schedule_time_now_not_left_to_the_tick():
    clock = _Clock(1000.0)
    timers, st, svc = _timers(clock=clock, runs=_Runs())
    tid = st.create_thread("alice", "t", {})
    gone = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "nope", "metric": "ram_pct", "every_s": 30, "times": 2})
    assert gone == {"ok": False, "message": "unknown host: nope; hosts are box"}
    watts = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "watts", "every_s": 30, "times": 2})["timer_id"]
    clock.t = 1030.0
    svc.tick()
    assert svc.get(watts)["state"]["samples"] == [{"t": 1030.0, "error": "watts not reported"}]


def test_timer_fails_early_when_every_tick_errors():
    clock = _Clock(1000.0)
    hosts = _DeadHosts()
    alerts = []
    timers, st, svc = _timers(clock=clock, deps=_deps(hosts), alerts=alerts)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator",
                          args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 6})
    assert out["ok"]
    hosts.dead = True
    for _ in range(3):
        clock.t += 30.0
        svc.tick()
    job = svc.get(out["timer_id"])
    assert job["status"] == "failed" and job["message"].startswith("every tick failed:")
    assert alerts == []
    samples = job["state"]["samples"]
    assert len(samples) == 3 and all("error" in s for s in samples)
    assert any(m.get("tool_name") == tm.TICK_TOOL for m in st.messages(tid))


def test_timers_survive_a_restart_via_requeue():
    timers, st, svc = _timers()
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30})
    svc._store.update(out["timer_id"], status="running", started=1000.0)
    assert svc.recover() == 1 and svc.get(out["timer_id"])["status"] == "queued"


def test_samples_result_drops_the_oldest_rows_to_fit_the_report_budget():
    row = {"id": "t1", "thread_id": "x", "user": "alice", "label": "alarms", "every_s": 30, "times": 120, "status": "reporting",
           "spec": {"kind": "tool", "tool": "alarms", "args": {}, "pick": ""},
           "samples": [{"t": 1000 + i * 30, "value": {"alerts": ["a" * 200] * 2, "i": i}} for i in range(120)]}
    out = tm.samples_result(row)
    assert len(json.dumps(out)) <= tm.REPORT_CHARS and out["ticks"] == 120 and out["shown"].startswith("last ")
    assert out["samples"][-1]["value"]["i"] == 119 and "series" not in out
    text, prelude = tm.report_request(row)
    assert text.startswith("⏱ Timer finished: alarms · 120 ticks every 30 s over 60 min. Summarise")
    assert prelude["summary"] == "timer · alarms · 120 ticks"


def test_metric_mode_needs_host_detail_to_be_available():
    reg = tt.build_registry(_deps())
    assert tm.timer_spec({"host": "box", "metric": "ram_pct"}, reg, _cfg(disabled_tools=["host_detail"]), "operator")[1] == "host_detail cannot be polled"
    assert tm.timer_spec({"host": "box", "metric": "ram_pct", "every_s": 0}, reg, _cfg(), "operator")[0]["every_s"] == 30


def test_a_cancel_during_the_report_start_wins_and_stops_the_run():
    clock = _Clock(1000.0)
    runs = _Runs()
    timers, st, svc = _timers(clock=clock, runs=runs)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 1})
    runs.on_start = lambda: timers.cancel(out["timer_id"], "alice")
    clock.t = 1030.0
    svc.tick()                      # sample -> phase reporting
    clock.t = 1032.0
    svc.tick()                      # report turn starts, the operator cancels mid-flight
    job = svc.get(out["timer_id"])
    assert job["status"] == "cancelled" and job["result"] is None and runs.stopped == [("r1", "alice")]


def test_cancelling_a_timer_whose_report_run_already_started_stops_that_run():
    clock = _Clock(1000.0)
    runs = _Runs()
    timers, st, svc = _timers(clock=clock, runs=runs)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 1})
    svc._store.update(out["timer_id"], status="running", started=1000.0,
                      state={"samples": [{"t": 1030.0, "value": 41.0}], "phase": "reporting", "ends": 1030.0, "run_id": "r9"})
    row, err = timers.cancel(out["timer_id"], "alice")
    assert err is None and row["status"] == "cancelled" and row["run_id"] == "r9"
    assert runs.stopped == [("r9", "alice")]


def test_a_deleted_thread_gets_no_orphan_note_row():
    clock = _Clock(1000.0)
    runs = _Runs()
    alerts = []
    timers, st, svc = _timers(clock=clock, runs=runs, alerts=alerts)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 3})
    assert st.delete_thread("alice", tid)
    assert timers.cancel_thread(tid) == 1
    assert svc.get(out["timer_id"])["status"] == "cancelled" and st.messages(tid) == []
    out2 = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30})
    clock.t = 1030.0
    svc.tick()
    assert svc.get(out2["timer_id"])["message"] == "the conversation was deleted" and alerts == []
