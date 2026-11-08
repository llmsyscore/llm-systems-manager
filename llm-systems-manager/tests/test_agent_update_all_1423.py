"""#1423 Update all agents as a manager-side job: order, retry, verify stop, cancel, output, audit."""
from __future__ import annotations

import sqlite3
import types

import agent_update_all as ua
import jobs

A1, A2, A3 = "a" * 32, "b" * 32, "c" * 32


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Attempts(list):
    """One stream per call, in order."""


class Fleet:
    """Registry stand-in: agents come back on `latest` after a stream unless listed in `stuck`."""
    def __init__(self, agents, streams, stuck=()):
        self.agents = {a["agent_id"]: dict(a) for a in agents}
        self.streams, self.stuck, self.calls, self.records = streams, set(stuck), [], []

    def load(self):
        return {"agents": {k: dict(v) for k, v in self.agents.items()}}

    def open(self, agent):
        aid = agent["agent_id"]
        self.calls.append(aid)
        s = self.streams[aid]
        frames = s.pop(0) if isinstance(s, Attempts) else s
        if isinstance(frames, Exception):
            raise frames
        for f in frames:
            if f.get("stage") == "done" and f.get("ok") and aid not in self.stuck:
                self.agents[aid]["version"] = "v2"
            yield f

    def record(self, user, item, ok):
        self.records.append((user, item["agent_id"], ok))


def _agent(aid, host, version="v1", status="approved"):
    return {"agent_id": aid, "hostname": host, "version": version, "status": status, "token": "t"}


OK = [{"line": "version_before: v1"}, {"stage": "install", "msg": "running"}, {"line": "pip ok"},
      {"stage": "done", "ok": True, "rc": 0, "restart_eta_s": 3}]
DL_FAIL = [{"stage": "done", "ok": False, "rc": 1, "msg": "tarball download failed"}]
FAIL = [{"stage": "done", "ok": False, "rc": 2, "msg": "pip failed", "retryable": False}]


def _setup(fleet, clock=None):
    clock = clock or Clock()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    jobs.init_table(conn)
    svc = jobs.Service(jobs.Store(lambda: conn), cfg=lambda: types.SimpleNamespace(workers=2, history_days=30),
                       now=clock, inline=True)
    up = ua.UpdateAll(svc, load_agents=fleet.load, latest_version=lambda: "v2",
                      update_available=lambda cur, latest: bool(cur and latest and cur != latest),
                      open_stream=fleet.open, record_update=fleet.record, now=clock, sleep=clock.sleep)
    return svc, up


def _run(svc, up, user="alice", role="admin"):
    row, err, plan = up.start(user=user, role=role)
    assert err is None, err
    svc.tick()
    return svc.get(row["id"]), [line["text"] for line in up.lines(row["id"])[0]], plan


def test_updates_one_at_a_time_and_skips_disabled():
    fleet = Fleet([_agent(A1, "h1"), _agent(A2, "h2"), _agent(A3, "h3", status="disabled")],
                  {A1: OK, A2: OK})
    svc, up = _setup(fleet)
    row, text, plan = _run(svc, up)
    assert [a["agent_id"] for a in plan["todo"]] == [A1, A2]
    assert [a["agent_id"] for a in plan["skipped"]] == [A3]
    assert fleet.calls == [A1, A2]
    assert row["status"] == "done"
    assert row["result"]["results"] == [{"name": "h3", "state": "skipped"}, {"name": "h1", "state": "updated"},
                                        {"name": "h2", "state": "updated"}]
    assert "– h3: disabled, skipped" in text
    assert "── [1/2] h1: v1 → v2" in text and "── [2/2] h2: v1 → v2" in text
    assert "✓ h1 is back on v2" in text
    assert "── install: running" in text and "pip ok" in text
    assert not any(t.startswith("version_before") for t in text)
    assert text[-1] == "── Update all finished: 2 updated, skipped: h3"
    assert fleet.records == [("alice", A1, True), ("alice", A2, True)]


def test_download_failure_is_retried_once():
    fleet = Fleet([_agent(A1, "h1")], {A1: Attempts([DL_FAIL, OK])})
    svc, up = _setup(fleet)
    row, text, _ = _run(svc, up)
    assert fleet.calls == [A1, A1]
    assert "retrying h1 once — the download failed" in text
    assert row["result"]["results"] == [{"name": "h1", "state": "updated"}]


def test_failure_carries_on_but_missed_version_stops_the_run():
    fleet = Fleet([_agent(A1, "h1"), _agent(A2, "h2"), _agent(A3, "h3")], {A1: FAIL, A2: OK, A3: OK}, stuck={A2})
    svc, up = _setup(fleet)
    row, text, _ = _run(svc, up)
    assert fleet.calls == [A1, A2]
    assert "✗ h2 did not report v2 within 2 minutes" in text
    assert "stopping: h2 did not come back on v2, so the rest are left as they are" in text
    assert row["result"]["results"] == [{"name": "h1", "state": "failed"}, {"name": "h2", "state": "failed"},
                                        {"name": "h3", "state": "skipped"}]
    assert text[-1] == "── Update all finished: 0 updated, failed: h1, h2, skipped: h3"


def test_unreachable_agent_fails_that_agent_only():
    fleet = Fleet([_agent(A1, "h1"), _agent(A2, "h2")], {A1: RuntimeError("could not reach the agent"), A2: OK})
    svc, up = _setup(fleet)
    row, text, _ = _run(svc, up)
    assert "✗ request failed: could not reach the agent" in text
    assert row["result"]["results"] == [{"name": "h1", "state": "failed"}, {"name": "h2", "state": "updated"}]


def test_already_current_agent_needs_no_restart():
    done = [{"stage": "done", "ok": True, "rc": 0, "msg": "already up to date; no restart needed"}]
    fleet = Fleet([_agent(A1, "h1")], {A1: done}, stuck={A1})
    svc, up = _setup(fleet)
    row, text, _ = _run(svc, up)
    assert "✓ h1 already up to date; no restart needed" in text
    assert row["result"]["results"] == [{"name": "h1", "state": "updated"}]


def test_second_start_refused_while_one_is_live_and_nothing_to_do_refused():
    fleet = Fleet([_agent(A1, "h1")], {A1: OK})
    svc, up = _setup(fleet)
    row, err, _ = up.start(user="alice", role="admin")
    assert row and err is None
    assert up.start(user="alice", role="admin")[1] == "an Update all run is already going"
    svc.tick()
    assert up.start(user="alice", role="admin")[1] == "all agents are already up to date"
    fleet.agents[A1].update(version="v1", status="disabled")
    assert up.start(user="alice", role="admin")[1] == "only disabled agents need an update"


def test_non_admin_job_is_refused():
    fleet = Fleet([_agent(A1, "h1")], {A1: OK})
    svc, up = _setup(fleet)
    row, text, _ = _run(svc, up, role="operator")
    assert row["status"] == "failed" and row["message"] == ua.NOT_ADMIN
    assert fleet.calls == []


def test_cancel_between_agents_skips_the_rest():
    fleet = Fleet([_agent(A1, "h1"), _agent(A2, "h2")], {A1: OK, A2: OK})
    svc, up = _setup(fleet)
    row, _, _ = up.start(user="alice", role="admin")
    real_record = fleet.record

    def record_then_cancel(user, item, ok):
        real_record(user, item, ok)
        svc.cancel(row["id"], actor="alice")
    fleet.record = record_then_cancel
    up._record = record_then_cancel
    svc.tick()
    text = [line["text"] for line in up.lines(row["id"])[0]]
    assert fleet.calls == [A1]
    assert "stopped by the operator; the rest are left as they are" in text
    assert svc.get(row["id"])["status"] == "cancelled"


def test_lines_after_cursor_and_log_cap():
    fleet = Fleet([_agent(A1, "h1")], {A1: OK})
    svc, up = _setup(fleet)
    row, _, _ = _run(svc, up)
    allx, n = up.lines(row["id"])
    tail, n2 = up.lines(row["id"], n - 1)
    assert n == n2 == len(allx) and tail == allx[-1:]
    assert up.lines(row["id"], 10 ** 6) == ([], n)
    assert up.lines("nope") == ([], 0)


def test_frame_line_formats():
    assert ua.frame_line({"line": "version_to: v2"}) is None
    assert ua.frame_line({"blank": True}) == ("", "blank")
    assert ua.frame_line({"stage": "done", "ok": False, "rc": 3, "msg": "x"}) == ("✗ done (rc=3) — x", "err")
    assert ua.frame_line({"stage": "done", "ok": True}) == ("✓ done (rc=?)", "ok")
    assert ua.frame_line({"stage": "fetch", "msg": "go"}) == ("── fetch: go", "stage")
    assert ua.frame_line({"keepalive": 1}) is None
