"""#897: tool_run job kind — start on the agent, wait on tools/state, cancel, restart re-entry, start-or-queue."""
from __future__ import annotations

import sqlite3
import types

import pytest

import jobs
import tool_queue as tq

A1, A2 = "a" * 32, "b" * 32
AGENTS = {A1: {"agent_id": A1, "token": "t1", "hostname": "gpu-01"},
          A2: {"agent_id": A2, "token": "t2", "hostname": "gpu-02"}}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeAgent:
    """Scripted agent: `starts` answers POST start paths, `states` is a list of tools/state bodies
    consumed one per GET (the last one repeats), None entries mean unreachable."""
    def __init__(self, starts=None, states=None, clock=None):
        self.starts = starts if starts is not None else {"ok": True, "run_id": "r1"}
        self.states = list(states or [{"bench_active": False, "autotune_active": False, "quality_active": False}])
        self.calls = []
        self.clock = clock

    def __call__(self, method, agent, path, **kw):
        self.calls.append((method, agent["agent_id"], path, kw.get("json")))
        if method == "POST":
            if self.starts is None:
                return None
            body = self.starts
            return _Resp(body, body.get("_status", 200))
        if self.clock is not None:
            self.clock.t += 3.0
        body = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if body is None:
            return None
        if body.get("_status", 200) != 200:
            return _Resp({}, body["_status"])
        return _Resp(body)


def _env(agent, clock=None, held=None, holder=None, max_queued=5):
    clock = clock or Clock()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    jobs.init_table(conn)
    svc = jobs.Service(jobs.Store(lambda: conn), cfg=lambda: types.SimpleNamespace(workers=4, history_days=30),
                       now=clock, inline=True)
    deps = tq.Deps(agent_for=lambda aid: AGENTS.get(aid), agent_call=agent,
                   held_agents=lambda: set(held or ()), holder_tool=lambda aid: (holder or {}).get(aid),
                   hostname=lambda aid: AGENTS.get(aid, {}).get("hostname", ""),
                   note_start=lambda aid, p, t: agent.calls.append(("note", aid, p, t)),
                   max_queued=lambda: max_queued, sleep=lambda s: None, now=clock)
    q = tq.Queue(svc, deps)
    return q, svc, clock


def _spec(tool="benchmark", agent_id=A1, provider="llama", body=None):
    return {"provider": provider, "agent_id": agent_id, "tool": tool,
            "path": f"/{provider}/bench/run", "cancel": f"/{provider}/bench/cancel",
            "body": body or {"model_ids": ["org/m"]}, "model_id": "org/m"}


BUSY = {"bench_active": True, "autotune_active": False, "quality_active": False}
IDLE = {"bench_active": False, "autotune_active": False, "quality_active": False}


def test_run_starts_waits_for_flag_and_finishes_when_it_drops():
    agent = FakeAgent(states=[IDLE, BUSY, BUSY, IDLE])
    q, svc, _ = _env(agent)
    row = svc.submit(tq.KIND, _spec(), user="alice", role="operator", source="ui")
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "done" and done["result"] == {"run_id": "r1"}
    assert agent.calls[0] == ("POST", A1, "/llama/bench/run", {"model_ids": ["org/m"]})
    assert ("note", A1, "llama", "benchmark") in agent.calls
    assert done["state"]["run_id"] == "r1" and done["state"]["seen"] is True


def test_run_without_run_id_waits_on_flag():
    agent = FakeAgent(starts={"ok": True}, states=[BUSY, IDLE])
    q, svc, _ = _env(agent)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "done" and done["result"] == {"run_id": "started"}


def test_refusal_at_start_keeps_the_row_queued():
    agent = FakeAgent(starts={"ok": False, "error": "a benchmark is already in progress"})
    q, svc, _ = _env(agent)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    svc.tick()
    after = svc.get(row["id"])
    assert after["status"] == "queued" and after["message"] == "waiting for the host"
    assert after["next_run"] == pytest.approx(1000.0 + tq.POLL_S)


def test_other_start_errors_fail_the_job():
    agent = FakeAgent(starts={"ok": False, "error": "model not found"})
    q, svc, _ = _env(agent)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    svc.tick()
    assert svc.get(row["id"])["status"] == "failed"
    assert svc.get(row["id"])["message"] == "model not found"


def test_flag_never_seen_fails_after_grace():
    clock = Clock()
    agent = FakeAgent(states=[IDLE], clock=clock)
    q, svc, _ = _env(agent, clock=clock)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    svc.tick()
    assert svc.get(row["id"])["status"] == "failed"
    assert svc.get(row["id"])["message"] == "run did not start"


def test_unreachable_agent_fails_after_window():
    clock = Clock()
    agent = FakeAgent(states=[BUSY, None], clock=clock)
    q, svc, _ = _env(agent, clock=clock)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    svc.tick()
    assert svc.get(row["id"])["status"] == "failed"
    assert svc.get(row["id"])["message"] == "agent unreachable"
    assert clock.t - 1000.0 >= tq.UNREACHABLE_MAX_S


def test_restart_reentry_skips_the_start_and_only_waits():
    agent = FakeAgent(states=[BUSY, IDLE])
    q, svc, _ = _env(agent)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    svc.set_state(row["id"], {"run_id": "r9", "started": 990.0, "seen": True})
    svc.tick()
    done = svc.get(row["id"])
    assert done["status"] == "done" and done["result"] == {"run_id": "r9"}
    assert not [c for c in agent.calls if c[0] == "POST"]


def test_cancel_running_job_posts_the_tool_cancel_path():
    agent = FakeAgent(states=[BUSY])
    q, svc, _ = _env(agent)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    svc.set_state(row["id"], {"run_id": "r1", "started": 1000.0, "seen": True})
    q.on_cancel({**row, "status": "cancelled", "state": {"run_id": "r1"}})
    assert ("POST", A1, "/llama/bench/cancel", None) in agent.calls


def test_cancel_queued_job_never_touches_the_agent():
    agent = FakeAgent()
    q, svc, _ = _env(agent)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    q.on_cancel({**row, "status": "cancelled"})
    assert agent.calls == []


def test_kind_metadata():
    q, svc, _ = _env(FakeAgent())
    k = svc.kind(tq.KIND)
    assert k.exclusive(_spec()) == [f"perf:{A1}"]
    assert k.label(_spec(tool="autotune")) == "Autotune · m · gpu-01"
    assert k.resume == "requeue" and k.limit_s(_spec()) == tq.MAX_RUN_S
    assert k.validate({"provider": "llama", "agent_id": A1, "tool": "nope"}, {})[1]
    assert k.validate({"provider": "lms", "agent_id": A1, "tool": "quality"}, {})[1]
