"""#891: overnight fleet batch — validation, state machine, summary, store."""
from __future__ import annotations

import sqlite3

import pytest

import autotune_batch as ab

A1, A2 = "a" * 32, "b" * 32
NAMES = {A1: "alpha", A2: "bravo"}
NOW = 1_800_000_000.0


def _body(**kw):
    b = {"items": [{"agent_id": A1, "model_id": "org/m:Q4"}], "objective": "balanced",
         "dims": {"context": {"on": True}}, "budget_min": 480, "restart": True}
    b.update(kw)
    return b


# ── validation ──

def test_validate_accepts_a_minimal_body():
    req = ab.validate_body(_body(), {A1, A2}, NOW)
    assert req["items"] == [{"agent_id": A1, "model_id": "org/m:Q4"}]
    assert req["objective"] == "balanced" and req["budget_min"] == 480
    assert req["start_at"] == NOW and req["restart"] is True and req["power_cap_w"] is None


@pytest.mark.parametrize("patch,msg", [
    ({"items": []}, "items"),
    ({"items": [{"agent_id": "zz", "model_id": "m"}]}, "approved llama host"),
    ({"items": [{"agent_id": A1, "model_id": ""}]}, "model_id"),
    ({"items": [{"agent_id": A1, "model_id": "m"}] * 33}, "32"),
    ({"items": [{"agent_id": A1, "model_id": "m"}, {"agent_id": A1, "model_id": "m"}]}, "duplicate"),
    ({"objective": "loud"}, "objective"),
    ({"budget_min": 4}, "budget_min"),
    ({"budget_min": 1441}, "budget_min"),
    ({"start_at": NOW + 86401}, "start_at"),
    ({"start_at": NOW - 61}, "start_at"),
    ({"dims": "nope"}, "dims"),
    ({"objective": "quiet"}, "power_cap_w"),
    ({"objective": "quiet", "power_cap_w": 19}, "power_cap_w"),
])
def test_validate_rejects(patch, msg):
    with pytest.raises(ValueError) as e:
        ab.validate_body(_body(**patch), {A1, A2}, NOW)
    assert msg in str(e.value)


def test_validate_quiet_carries_the_cap_and_start_at_rounds():
    req = ab.validate_body(_body(objective="quiet", power_cap_w=250.4, start_at=NOW + 3600.6), {A1}, NOW)
    assert req["power_cap_w"] == 250.4 and req["start_at"] == NOW + 3600.6


# ── pure helpers ──

def _doc(ok=True, verify_ok=True, gain=12.0, selected=True, ctx=32768):
    return {"type": "model_done", "model_id": "org/m:Q4", "run_id": "r1", "ok": ok,
            "verify": {"ok": verify_ok}, "stop_reason": None if ok else "budget",
            "before": {"decode_tps": 40.0}, "after": {"decode_tps": 40.0 * (1 + gain / 100), "ctx": ctx, "wh_per_ktok": 0.42},
            "changes": [{"key": "threads", "current": "8", "recommended": "12", "selected": selected}]}


def test_should_apply_rules():
    assert ab.should_apply(_doc()) == (True, "")
    assert ab.should_apply(_doc(verify_ok=False))[0] is False and "verify" in ab.should_apply(_doc(verify_ok=False))[1]
    assert ab.should_apply(_doc(gain=-3))[0] is False and "slower" in ab.should_apply(_doc(gain=-3))[1]
    assert ab.should_apply(_doc(selected=False))[0] is False and "no change" in ab.should_apply(_doc(selected=False))[1]
    d = _doc(); d["before"] = None
    assert ab.should_apply(d) == (True, "")


def test_item_from_done_copies_the_headline_fields():
    item = {"status": "running"}
    ab.item_from_done(item, _doc(gain=10.0))
    assert item["gain_pct"] == pytest.approx(10.0) and item["ctx"] == 32768
    assert item["wh_per_ktok"] == 0.42 and item["run_id"] == "r1" and item["verify_ok"] is True


def test_summary_title_and_body_lines():
    b = ab.new_batch(ab.validate_body(_body(items=[
        {"agent_id": A1, "model_id": "org/m:Q4"}, {"agent_id": A1, "model_id": "org/n:Q8"},
        {"agent_id": A2, "model_id": "org/m:Q4"}]), {A1, A2}, NOW), NAMES, NOW)
    b["items"][0].update({"status": "done", "gain_pct": 12.4, "ctx": 32768, "applied": True, "note": "applied 1 change"})
    b["items"][1].update({"status": "done", "gain_pct": -2.0, "ctx": 8192, "applied": False, "note": "slower than the live config"})
    b["items"][2].update({"status": "skipped", "note": "host offline"})
    b["restarted"] = ["alpha"]
    s = ab.summary_of(b)
    assert s["title"] == "Overnight autotune: 2 tuned, 1 applied, 1 skipped"
    lines = s["body"].split("\n")
    assert lines[0] == "alpha · org/m:Q4 · +12 % · ctx 32,768 · applied"
    assert lines[1] == "alpha · org/n:Q8 · −2 % · ctx 8,192 · not applied · slower than the live config"
    assert lines[2] == "bravo · org/m:Q4 · skipped · host offline"
    assert lines[3] == "restarted: alpha"
    p = ab.alert_payload(b)
    assert p["severity"] == "info" and p["metric"] == f"autotune/batch/{b['id']}"
    assert p["name"] == s["title"] and p["message"] == s["body"]


def test_summary_trims_notes_and_caps_the_alert_body():
    items = [{"hostname": f"h{i}", "model_id": f"org/m{i}:Q4", "status": "failed", "note": "x" * 400,
              "applied": False, "gain_pct": None, "ctx": None} for i in range(40)]
    b = {"id": "cafebabe1234", "items": items, "restarted": [], "restart_errors": {}}
    s = ab.summary_of(b)
    assert all(len(line) <= 60 + ab.SUMMARY_NOTE_MAX for line in s["body"].split("\n"))
    assert len(ab.alert_payload(b)["message"]) == ab.SUMMARY_MAX_CHARS


# ── store ──

@pytest.fixture
def store(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db", check_same_thread=False)
    ab.init_table(conn)
    return ab.Store(lambda: conn)


def test_store_round_trip_and_active(store):
    b = ab.new_batch(ab.validate_body(_body(), {A1}, NOW), NAMES, NOW)
    store.save(b)
    assert store.get(b["id"])["items"][0]["hostname"] == "alpha"
    assert store.active()["id"] == b["id"]
    b["status"] = "done"; store.save(b)
    assert store.active() is None
    assert [x["id"] for x in store.recent(5)] == [b["id"]]


def test_store_recover_fails_running_and_returns_queued(store):
    running = ab.new_batch(ab.validate_body(_body(), {A1}, NOW), NAMES, NOW)
    running["status"] = "running"; running["items"][0]["status"] = "running"
    queued = ab.new_batch(ab.validate_body(_body(start_at=NOW + 600), {A1}, NOW), NAMES, NOW + 1)
    store.save(running); store.save(queued)
    rearm, failed = store.recover(NOW + 2)
    assert [x["id"] for x in rearm] == [queued["id"]]
    assert [x["id"] for x in failed] == [running["id"]]
    r = store.get(running["id"])
    assert r["status"] == "failed" and r["error"] == "manager restarted mid-batch"
    assert r["items"][0]["status"] == "failed" and r["items"][0]["note"] == "manager restarted"


# ── runner ──

class Fake:
    """Injected callables with a scripted world; records every call."""

    def __init__(self):
        self.t = NOW
        self.calls = []
        self.online = {A1: True, A2: False}
        self.busy = set()
        self.pre = {A1: {"ok": True, "busy": False, "unit_active": True, "sizes": {"org/m:Q4": 1, "org/n:Q8": 1}}}
        self.docs = {"org/m:Q4": _doc(), "org/n:Q8": _doc(gain=-3)}
        self.run_ok = True
        self.cfg = {"__DEFAULTS__": {}, "org/m:Q4": {"threads": "8"}, "org/n:Q8": {"threads": "8"}}
        self.writes, self.profiles, self.alerts = [], [], []
        self.stream_events = None
        self.read_error = None
        self.ended = []
        self.pre_busy = []      # scripted busy flags consumed one per preflight call

    def deps(self):
        return ab.Deps(
            hosts=lambda: [{"agent_id": a, "hostname": NAMES[a], "online": self.online[a]} for a in (A1, A2)],
            busy_agents=lambda: set(self.busy),
            preflight=self._preflight,
            run_on_agent=self._run, stream_on_agent=self._stream,
            run_ended=lambda aid: self.ended.append(aid),
            cancel_on_agent=lambda aid: self.calls.append(("cancel", aid)) or True,
            stop_server=lambda aid: self.calls.append(("stop", aid)) or (True, None),
            restart_server=lambda aid: self.calls.append(("restart", aid)) or (True, None),
            read_config=self._read_config,
            write_config=lambda aid, cfg: self.writes.append((aid, cfg)) or (True, None),
            active_profile=lambda aid, mid: "default",
            save_profile=lambda aid, mid, name, values, make_active: self.profiles.append((mid, name, dict(values), make_active)),
            alert=lambda payload: self.alerts.append(payload) or True,
            now=lambda: self.t, sleep=self._sleep, shutting_down=lambda: False)

    def _sleep(self, s):
        self.t += s

    def _preflight(self, aid):
        p = self.pre.get(aid)
        if p is not None and self.pre_busy:
            p = dict(p, busy=self.pre_busy.pop(0))
        return p

    def _read_config(self, aid):
        if self.read_error:
            raise RuntimeError(self.read_error)
        return {k: dict(v) for k, v in self.cfg.items()}

    def _run(self, aid, body):
        self.calls.append(("run", aid, body))
        return (True, "run-" + body["model_ids"][0]) if self.run_ok else (False, "Another benchmark or auto-tune is in progress")

    def _stream(self, aid, last_id=None):
        if self.stream_events is not None:
            yield from self.stream_events
            return
        mid = self.calls[-1][2]["model_ids"][0]
        yield {"type": "line", "text": "x"}
        self.t += 600
        yield dict(self.docs[mid], model_id=mid)
        yield {"type": "done", "ok": True}


def _run_batch(fake, body):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    ab.init_table(conn)
    store = ab.Store(lambda: conn)
    b = ab.new_batch(ab.validate_body(body, {A1, A2}, NOW), NAMES, NOW)
    store.save(b)
    runner = ab.Runner(store, fake.deps())
    runner.run(b["id"])
    return store.get(b["id"]), runner


def test_runner_tunes_applies_restarts_and_alerts():
    f = Fake()
    b, _ = _run_batch(f, _body(items=[{"agent_id": A1, "model_id": "org/m:Q4"}, {"agent_id": A1, "model_id": "org/n:Q8"}]))
    assert b["status"] == "done"
    m, n = b["items"]
    assert m["status"] == "done" and m["applied"] is True and m["note"] == "applied 1 change"
    assert n["status"] == "done" and n["applied"] is False and "slower" in n["note"]
    # The unit reads active before each item, so each one stops it; one restart, at the end.
    assert [c for c in f.calls if c[0] in ("stop", "restart")] == [("stop", A1), ("stop", A1), ("restart", A1)]
    assert f.writes[0][1]["org/m:Q4"] == {"threads": "12"} and "__DEFAULTS__" not in f.writes[0][1]
    assert f.profiles[0][1].startswith("before batch ") and f.profiles[1] == ("org/m:Q4", "default", {"threads": "12"}, True)
    assert b["summary"]["title"] == "Overnight autotune: 2 tuned, 1 applied, 0 skipped"
    assert b["restarted"] == ["alpha"]
    assert f.alerts and f.alerts[0]["name"] == b["summary"]["title"]


def test_runner_splits_the_budget_across_remaining_items():
    f = Fake()
    _run_batch(f, _body(budget_min=100, items=[{"agent_id": A1, "model_id": "org/m:Q4"}, {"agent_id": A1, "model_id": "org/n:Q8"}]))
    runs = [c for c in f.calls if c[0] == "run"]
    assert runs[0][2]["budget_min"] == 50
    # 10 minutes elapsed on the first item: 90 left for one item.
    assert runs[1][2]["budget_min"] == 90
    assert runs[0][2]["objective"] == "balanced" and runs[0][2]["dims"] == {"context": {"on": True}}


def test_runner_skips_offline_busy_and_unconfigured():
    f = Fake()
    f.busy.add(A1)
    b, _ = _run_batch(f, _body(items=[{"agent_id": A2, "model_id": "org/m:Q4"}, {"agent_id": A1, "model_id": "org/m:Q4"}]))
    assert [i["status"] for i in b["items"]] == ["skipped", "skipped"]
    assert b["items"][0]["note"] == "host offline" and b["items"][1]["note"] == "host busy"
    f = Fake()
    b, _ = _run_batch(f, _body(items=[{"agent_id": A1, "model_id": "org/zz:Q4"}]))
    assert b["items"][0]["note"] == "model not configured"
    assert b["summary"]["title"] == "Overnight autotune: 0 tuned, 0 applied, 1 skipped"
    assert not [c for c in f.calls if c[0] == "run"]


def test_runner_unreachable_preflight_and_refused_start():
    f = Fake(); f.pre = {}
    b, _ = _run_batch(f, _body())
    assert b["items"][0]["status"] == "skipped" and b["items"][0]["note"] == "host unreachable"
    f = Fake(); f.run_ok = False
    b, _ = _run_batch(f, _body())
    assert b["items"][0]["status"] == "failed" and "in progress" in b["items"][0]["note"]
    # A host the batch stopped is started again even when nothing was tuned.
    assert ("restart", A1) in f.calls


def test_runner_verify_failure_is_not_applied_and_no_restart_without_toggle():
    f = Fake(); f.pre[A1]["unit_active"] = False
    f.docs["org/m:Q4"] = _doc(verify_ok=False)
    b, _ = _run_batch(f, _body(restart=False))
    i = b["items"][0]
    assert i["status"] == "done" and i["applied"] is False and "verify" in i["note"]
    assert not f.writes and not [c for c in f.calls if c[0] in ("stop", "restart")]


def test_runner_applied_item_restarts_when_toggle_on_and_unit_was_down():
    f = Fake(); f.pre[A1]["unit_active"] = False
    b, _ = _run_batch(f, _body(restart=True))
    assert b["items"][0]["applied"] is True and ("restart", A1) in f.calls and ("stop", A1) not in f.calls


def test_runner_failed_run_and_stream_error():
    f = Fake(); f.docs["org/m:Q4"] = _doc(ok=False)
    b, _ = _run_batch(f, _body())
    assert b["items"][0]["status"] == "failed" and b["items"][0]["note"] == "budget"
    f = Fake(); f.stream_events = [{"type": "done", "ok": False, "error": "no active job"}]
    b, _ = _run_batch(f, _body())
    assert b["items"][0]["status"] == "failed" and b["items"][0]["note"] == "no active job"


def test_runner_item_timeout_cancels_on_the_agent():
    f = Fake()

    def slow(aid, last_id=None):
        yield {"type": "line", "text": "a"}
        f.t += ab.ITEM_CAP_S + 1
        yield {"type": "line", "text": "b"}
        yield {"type": "done", "ok": True}
    f._stream = slow
    b, _ = _run_batch(f, _body())
    assert b["items"][0]["status"] == "failed" and b["items"][0]["note"] == "timed out after 2 h"
    assert ("cancel", A1) in f.calls


def test_runner_cancel_stops_after_the_current_item():
    f = Fake()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    ab.init_table(conn)
    store = ab.Store(lambda: conn)
    b = ab.new_batch(ab.validate_body(_body(items=[{"agent_id": A1, "model_id": "org/m:Q4"}, {"agent_id": A1, "model_id": "org/n:Q8"}]), {A1}, NOW), NAMES, NOW)
    store.save(b)
    orig = f._stream
    holder = {}

    def stream(aid, last_id=None):
        for ev in orig(aid, last_id):
            if ev.get("type") == "line":
                assert holder["runner"].cancel(b["id"]) == "running"
            yield ev
    # Deps bind the stream at construction, so the wrapper must be in place first.
    f._stream = stream
    runner = holder["runner"] = ab.Runner(store, f.deps())
    runner.run(b["id"])
    out = store.get(b["id"])
    assert out["status"] == "cancelled"
    assert out["items"][0]["status"] == "cancelled" and out["items"][1]["status"] == "cancelled"
    assert ("cancel", A1) in f.calls and ("restart", A1) in f.calls
    assert f.alerts[0]["name"].startswith("Overnight autotune")


def test_runner_waits_for_start_at_and_cancel_while_queued():
    f = Fake()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    ab.init_table(conn)
    store = ab.Store(lambda: conn)
    b = ab.new_batch(ab.validate_body(_body(start_at=NOW + 120), {A1}, NOW), NAMES, NOW)
    store.save(b)
    runner = ab.Runner(store, f.deps())
    runner.run(b["id"])
    assert f.t >= NOW + 120 and store.get(b["id"])["status"] == "done"
    b2 = ab.new_batch(ab.validate_body(_body(start_at=NOW + 120), {A1}, NOW), NAMES, NOW)
    store.save(b2)
    f2 = Fake()
    runner2 = ab.Runner(store, f2.deps())
    assert runner2.cancel(b2["id"]) == "cancelled"
    runner2.run(b2["id"])
    assert store.get(b2["id"])["status"] == "cancelled" and not [c for c in f2.calls if c[0] == "run"]


def test_runner_restarts_stopped_hosts_when_an_item_raises():
    f = Fake(); f.read_error = "agent went away"
    b, _ = _run_batch(f, _body())
    assert b["status"] == "failed" and "agent went away" in b["error"]
    assert ("stop", A1) in f.calls and ("restart", A1) in f.calls
    assert b["restarted"] == ["alpha"]


def test_recover_restarts_hosts_a_crashed_batch_had_stopped(store):
    running = ab.new_batch(ab.validate_body(_body(items=[
        {"agent_id": A1, "model_id": "org/m:Q4"}, {"agent_id": A2, "model_id": "org/m:Q4"}]), {A1, A2}, NOW), NAMES, NOW)
    running.update({"status": "running", "stopped": [A1]})
    running["items"][0]["status"] = "running"
    running["items"][1].update({"status": "done", "applied": True})
    queued = ab.new_batch(ab.validate_body(_body(start_at=NOW + 600), {A1}, NOW), NAMES, NOW + 1)
    store.save(running); store.save(queued)
    f = Fake()
    rearm = ab.Runner(store, f.deps()).recover(NOW + 5)
    assert [x["id"] for x in rearm] == [queued["id"]]
    row = store.get(running["id"])
    assert row["status"] == "failed" and row["restarted"] == ["alpha"]
    # Only the host the batch stopped comes back; the applied host was never stopped.
    assert [c for c in f.calls if c[0] == "restart"] == [("restart", A1)]
    # The still-running tune is cancelled first, so the restart is not fighting it.
    assert f.calls.index(("cancel", A1)) < f.calls.index(("restart", A1))


def test_runner_start_returns_none_under_pytest():
    f = Fake()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    ab.init_table(conn)
    store = ab.Store(lambda: conn)
    b = ab.new_batch(ab.validate_body(_body(), {A1}, NOW), NAMES, NOW)
    store.save(b)
    assert ab.Runner(store, f.deps()).start(b) is None


def test_runner_waits_out_a_settling_host_between_items():
    f = Fake()
    # The first item's preflight is clear; the host then reads busy twice before it settles.
    f.pre_busy = [False, True, True, False]
    b, _ = _run_batch(f, _body(items=[{"agent_id": A1, "model_id": "org/m:Q4"}, {"agent_id": A1, "model_id": "org/n:Q8"}]))
    assert [i["status"] for i in b["items"]] == ["done", "done"]
    assert [c[1] for c in f.calls if c[0] == "run"] == [A1, A1]
    assert f.ended == [A1, A1]
    assert not f.pre_busy


def test_runner_gives_up_on_a_host_that_never_settles():
    f = Fake()
    f.pre_busy = [False] + [True] * 200
    b, _ = _run_batch(f, _body(items=[{"agent_id": A1, "model_id": "org/m:Q4"}, {"agent_id": A1, "model_id": "org/n:Q8"}]))
    assert [i["status"] for i in b["items"]] == ["done", "skipped"]
    assert b["items"][1]["note"] == "host busy"


def test_runner_caps_the_item_budget_below_the_follower_limit():
    f = Fake()
    _run_batch(f, _body(budget_min=900))
    assert [c for c in f.calls if c[0] == "run"][0][2]["budget_min"] == ab.ITEM_BUDGET_MAX == 115


def test_runner_reconnects_a_dropped_stream_with_the_last_event_id():
    f = Fake()
    seen = []

    def flaky(aid, last_id=None):
        seen.append(last_id)
        yield {"type": "line", "text": "a", "_id": "7"}
        if len(seen) == 1:
            raise RuntimeError("connection reset")
        yield dict(f.docs["org/m:Q4"], model_id="org/m:Q4")
        yield {"type": "done", "ok": True}
    f._stream = flaky
    b, _ = _run_batch(f, _body())
    assert b["items"][0]["status"] == "done" and b["items"][0]["applied"] is True
    assert seen == [None, "7"]
    assert ("cancel", A1) not in f.calls


def test_runner_cancels_the_agent_when_the_stream_never_comes_back():
    f = Fake()

    def broken(aid, last_id=None):
        raise RuntimeError("no reachable agent stream")
        yield  # pragma: no cover
    f._stream = broken
    b, _ = _run_batch(f, _body())
    assert b["items"][0]["status"] == "failed"
    assert b["items"][0]["note"].startswith("stream failed")
    assert ("cancel", A1) in f.calls
    assert f.ended == [A1]
