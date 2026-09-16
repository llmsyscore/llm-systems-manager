"""#1029 Tower timers: spec validation, limits, ticks and samples, the report turn, cancel, restart."""
from __future__ import annotations

import json
import types

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


def _timers(store=None, cfg=None, runs=None, clock=None, deps=None, audit=None):
    st = store or tower.Store(":memory:")
    reg = tt.build_registry(deps or _deps())
    t = tm.Timers(st, registry_factory=lambda: reg, cfg=lambda: cfg or _cfg(), audit=audit, now=clock or _Clock())
    t.runs = runs
    return t, st, reg


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


def test_pick_path_walks_dicts_and_lists():
    obj = {"live": {"ram": {"used_pct": 41}}, "hosts": [{"hostname": "box"}, {"hostname": "mac"}]}
    assert tm.pick_path(obj, "live.ram.used_pct") == 41
    assert tm.pick_path(obj, "hosts.1.hostname") == "mac"
    assert tm.pick_path(obj, "hosts.5.hostname") is None
    assert tm.pick_path(obj, "live.gpu") is None
    assert tm.pick_path(obj, "") == obj


def test_schedule_queues_a_timer_with_one_interval_before_the_first_tick():
    audits = []
    clock = _Clock(1000.0)
    timers, st, _ = _timers(clock=clock, audit=audits.append)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 2})
    assert out["ok"] and out["every_s"] == 30 and out["times"] == 2 and out["label"] == "ram_pct on box"
    row = st.get_timer(out["timer_id"])
    assert row["status"] == "queued" and row["next_tick"] == 1030.0 and row["ends"] == 1060.0 and row["samples"] == []
    assert audits[-1]["action"] == "tower.timer.schedule" and audits[-1]["actor"] == "timer via alice"
    assert timers.live_count("alice") == 1 and timers.live_count("bob") == 0
    assert st.get_thread("alice", tid)["updated"] == 1000.0
    view = timers.list("alice")[0]
    assert view["count"] == 0 and view["next_in_s"] == 30 and view["left_s"] == 60 and view["status"] == "queued"


def test_schedule_refuses_a_bad_spec_and_enforces_the_limits():
    timers, st, _ = _timers()
    tid = st.create_thread("alice", "t", {})
    assert timers.schedule(thread_id=tid, user="alice", role="operator", args={"metric": "nope", "host": "box"}) == {"ok": False, "message": "metric must be one of " + ", ".join(tm.METRICS)}
    for _ in range(tm.MAX_PER_USER):
        assert timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 60})["ok"]
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 60})
    assert not out["ok"] and "cancel one first" in out["message"]
    for i in range(tm.MAX_TOTAL - tm.MAX_PER_USER):
        assert timers.schedule(thread_id=tid, user=f"u{i}", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 60})["ok"]
    out = timers.schedule(thread_id=tid, user="zed", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 60})
    assert not out["ok"] and "try again later" in out["message"]


def test_ticks_sample_the_metric_then_start_the_report_turn_with_the_samples():
    audits = []
    clock = _Clock(1000.0)
    runs = _Runs()
    timers, st, _ = _timers(clock=clock, runs=runs, audit=audits.append)
    tid = st.create_thread("alice", "t", {})
    out = timers.schedule(thread_id=tid, user="alice", role="operator", args={"label": "RAM on box", "host": "box", "metric": "ram_pct", "every_s": 30, "times": 2})
    timer_id = out["timer_id"]
    timers.tick()
    assert st.get_timer(timer_id)["samples"] == [] and runs.ticks == []
    clock.t = 1030.0
    timers.tick()
    row = st.get_timer(timer_id)
    assert row["status"] == "running" and row["samples"] == [{"t": 1030.0, "value": 41.0}] and row["next_tick"] == 1060.0
    assert runs.ticks == ["alice"] and runs.started == []
    clock.t = 1061.0
    timers.tick()
    row = st.get_timer(timer_id)
    assert row["status"] == "done" and row["run_id"] == "r1" and row["resolved"] == 1061.0
    assert [s["value"] for s in row["samples"]] == [41.0, 42.0]
    kw = runs.started[0]
    assert kw["user"] == "alice" and kw["role"] == "operator" and kw["thread_id"] == tid and kw["actor"] == "timer via alice"
    assert kw["text"].startswith("⏱ Timer finished: RAM on box · 2 ticks every 30 s.")
    res = kw["prelude"]["result"]
    assert kw["prelude"]["name"] == "timer" and kw["prelude"]["args"]["timer_id"] == timer_id
    assert res["ok"] and res["ticks"] == 2 and res["planned"] == 2 and res["errors"] == 0
    assert res["min"] == 41.0 and res["max"] == 42.0 and res["avg"] == 41.5 and res["unit"] == "%"
    assert res["series"] == [[1030, 41.0], [1061, 42.0]] and [s["value"] for s in res["samples"]] == [41.0, 42.0]
    assert [a["action"] for a in audits] == ["tower.timer.schedule", "tower.timer.complete"]
    assert timers.live_count("alice") == 0 and timers.list("alice")[0]["status"] == "done"


def test_a_missed_tick_reschedules_from_now_not_from_the_past():
    clock = _Clock(1000.0)
    timers, st, _ = _timers(clock=clock, runs=_Runs())
    tid = st.create_thread("alice", "t", {})
    timer_id = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 5})["timer_id"]
    clock.t = 1100.0
    timers.tick()
    assert st.get_timer(timer_id)["next_tick"] == 1130.0


def test_tool_mode_keeps_the_picked_field_or_a_compact_copy():
    clock = _Clock(1000.0)
    timers, st, _ = _timers(clock=clock, runs=_Runs())
    tid = st.create_thread("alice", "t", {})
    picked = timers.schedule(thread_id=tid, user="alice", role="operator",
                             args={"tool": "host_detail", "args": {"host": "box"}, "pick": "live.ram.used_pct", "every_s": 30})["timer_id"]
    whole = timers.schedule(thread_id=tid, user="alice", role="operator",
                            args={"tool": "alarms", "args": {}, "every_s": 30})["timer_id"]
    missing = timers.schedule(thread_id=tid, user="alice", role="operator",
                              args={"tool": "host_detail", "args": {"host": "box"}, "pick": "live.gpu.temp", "every_s": 30})["timer_id"]
    clock.t = 1030.0
    timers.tick()
    assert st.get_timer(picked)["samples"][0]["value"] == 41
    whole_sample = st.get_timer(whole)["samples"][0]["value"]
    assert isinstance(whole_sample, (dict, list)) and len(json.dumps(whole_sample)) <= tm.SAMPLE_CHARS + 40
    assert st.get_timer(missing)["samples"][0] == {"t": 1030.0, "error": "live.gpu.temp not in the result"}


def test_report_retries_while_the_user_is_busy_then_gives_up():
    clock = _Clock(1000.0)
    runs = _Runs(err=(409, "run_active"))
    timers, st, _ = _timers(clock=clock, runs=runs)
    tid = st.create_thread("alice", "t", {})
    timer_id = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 1})["timer_id"]
    clock.t = 1030.0
    timers.tick()
    assert st.get_timer(timer_id)["status"] == "reporting" and len(runs.started) == 1
    clock.t = 1090.0
    timers.tick()
    assert st.get_timer(timer_id)["status"] == "reporting" and len(runs.started) == 2
    runs.err = None
    clock.t = 1092.0
    timers.tick()
    row = st.get_timer(timer_id)
    assert row["status"] == "done" and row["run_id"] == "r3"


def test_report_that_never_starts_fails_and_leaves_the_samples_in_the_thread():
    clock = _Clock(1000.0)
    runs = _Runs(err=(503, "no_model"))
    timers, st, _ = _timers(clock=clock, runs=runs)
    tid = st.create_thread("alice", "t", {})
    timer_id = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 1})["timer_id"]
    clock.t = 1030.0
    timers.tick()
    clock.t = 1030.0 + tm.REPORT_RETRY_S + 1
    timers.tick()
    row = st.get_timer(timer_id)
    assert row["status"] == "failed" and row["message"] == "could not report: no_model"
    rows = [r for r in st.messages(tid) if r["role"] == "tool"]
    assert len(rows) == 1 and rows[0]["tool_name"] == "timer" and rows[0]["tool_ok"] == 0
    body = json.loads(rows[0]["content"])
    assert body["status"] == "failed" and body["message"] == "could not report: no_model" and body["ticks"] == 1


def test_a_rate_limited_tick_is_recorded_as_an_error_sample():
    clock = _Clock(1000.0)
    hosts = _Hosts()
    timers, st, _ = _timers(clock=clock, runs=_Runs(ticks_ok=False), deps=_deps(hosts))
    tid = st.create_thread("alice", "t", {})
    timer_id = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 3})["timer_id"]
    clock.t = 1030.0
    timers.tick()
    assert st.get_timer(timer_id)["samples"] == [{"t": 1030.0, "error": "rate limited"}] and hosts.reads == 0


def test_unknown_host_and_missing_metric_become_error_samples():
    clock = _Clock(1000.0)
    timers, st, _ = _timers(clock=clock, runs=_Runs())
    tid = st.create_thread("alice", "t", {})
    gone = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "nope", "metric": "ram_pct", "every_s": 30, "times": 2})["timer_id"]
    watts = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "watts", "every_s": 30, "times": 2})["timer_id"]
    clock.t = 1030.0
    timers.tick()
    assert st.get_timer(gone)["samples"] == [{"t": 1030.0, "error": "unknown host"}]
    assert st.get_timer(watts)["samples"] == [{"t": 1030.0, "error": "watts not reported"}]


def test_a_timer_fails_when_tower_is_off_the_tool_is_disabled_or_the_thread_is_gone():
    clock = _Clock(1000.0)
    cfg = _cfg()
    timers, st, _ = _timers(clock=clock, runs=_Runs(), cfg=cfg)
    tid = st.create_thread("alice", "t", {})
    off = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30})["timer_id"]
    cfg.enabled = False
    clock.t = 1030.0
    timers.tick()
    assert st.get_timer(off)["status"] == "failed" and st.get_timer(off)["message"] == "Tower was turned off"
    cfg.enabled = True
    disabled = timers.schedule(thread_id=tid, user="alice", role="operator", args={"tool": "alarms", "args": {}, "every_s": 30})["timer_id"]
    cfg.disabled_tools = ["alarms"]
    clock.t = 1060.0
    timers.tick()
    assert st.get_timer(disabled)["message"] == "alarms is no longer available"
    cfg.disabled_tools = []
    orphan = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30})["timer_id"]
    st.delete_thread("alice", tid)
    assert st.get_timer(orphan)["status"] == "cancelled" and st.get_timer(orphan)["message"] == "the conversation was deleted"


def test_cancel_stops_a_live_timer_and_notes_it_in_the_thread():
    audits = []
    clock = _Clock(1000.0)
    timers, st, _ = _timers(clock=clock, runs=_Runs(), audit=audits.append)
    tid = st.create_thread("alice", "t", {})
    timer_id = timers.schedule(thread_id=tid, user="alice", role="operator", args={"label": "RAM", "host": "box", "metric": "ram_pct", "every_s": 30, "times": 4})["timer_id"]
    clock.t = 1030.0
    timers.tick()
    assert timers.cancel(timer_id, "bob") == (None, (404, "unknown timer"))
    assert timers.cancel("nope", "alice") == (None, (404, "unknown timer"))
    row, err = timers.cancel(timer_id, "alice")
    assert err is None and row["status"] == "cancelled" and row["message"] == "cancelled by the operator" and row["resolved"] == 1030.0
    assert timers.cancel(timer_id, "alice") == (None, (409, "not live"))
    note = [r for r in st.messages(tid) if r["role"] == "tool"][-1]
    body = json.loads(note["content"])
    assert note["tool_name"] == "timer" and body["status"] == "cancelled" and body["ticks"] == 1 and body["label"] == "RAM"
    assert [a["action"] for a in audits] == ["tower.timer.schedule"]
    clock.t = 1060.0
    timers.tick()
    assert len(st.get_timer(timer_id)["samples"]) == 1
    assert timers.list("alice")[0]["status"] == "cancelled"
    clock.t = 1060.0 + tm.RECENT_S + 1
    assert timers.list("alice") == []


def test_a_manager_restart_fails_every_live_timer():
    clock = _Clock(1000.0)
    timers, st, _ = _timers(clock=clock, runs=_Runs())
    tid = st.create_thread("alice", "t", {})
    a = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30})["timer_id"]
    st.init_tables()
    row = st.get_timer(a)
    assert row["status"] == "failed" and row["message"] == "manager restarted" and row["next_tick"] is None


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


def test_start_thread_is_inert_under_pytest():
    assert tm.start_thread(None, lambda: True) is None


def test_a_cancel_during_the_report_start_wins_and_stops_the_run():
    clock = _Clock(1000.0)
    runs = _Runs()
    timers, st, _ = _timers(clock=clock, runs=runs)
    tid = st.create_thread("alice", "t", {})
    timer_id = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 1})["timer_id"]
    runs.on_start = lambda: timers.cancel(timer_id, "alice")
    clock.t = 1030.0
    timers.tick()
    row = st.get_timer(timer_id)
    assert row["status"] == "cancelled" and row["run_id"] is None and runs.stopped == [("r1", "alice")]


def test_a_cancelled_timer_is_not_retried_or_flipped_to_failed_by_a_later_tick():
    clock = _Clock(1000.0)
    runs = _Runs(err=(409, "run_active"))
    timers, st, _ = _timers(clock=clock, runs=runs)
    tid = st.create_thread("alice", "t", {})
    timer_id = timers.schedule(thread_id=tid, user="alice", role="operator", args={"host": "box", "metric": "ram_pct", "every_s": 30, "times": 1})["timer_id"]
    clock.t = 1030.0
    timers.tick()
    assert st.get_timer(timer_id)["status"] == "reporting"
    timers.cancel(timer_id, "alice")
    clock.t = 1030.0 + tm.REPORT_RETRY_S + 5
    timers.tick()
    row = st.get_timer(timer_id)
    assert row["status"] == "cancelled" and len(runs.started) == 1


def test_metric_mode_needs_host_detail_to_be_available():
    reg = tt.build_registry(_deps())
    assert tm.timer_spec({"host": "box", "metric": "ram_pct"}, reg, _cfg(disabled_tools=["host_detail"]), "operator")[1] == "host_detail cannot be polled"
    assert tm.timer_spec({"host": "box", "metric": "ram_pct", "every_s": 0}, reg, _cfg(), "operator")[0]["every_s"] == 30
