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
    svc._store.update(row["id"], status="running", started=1000.0, lease=1030.0)
    svc.cancel(row["id"], actor="alice")
    assert ("POST", A1, "/llama/bench/cancel", None) in agent.calls
    assert svc.get(row["id"])["status"] == "cancelled"


def test_cancel_queued_job_never_touches_the_agent():
    agent = FakeAgent()
    q, svc, _ = _env(agent)
    row = svc.submit(tq.KIND, _spec(), user="alice")
    svc.cancel(row["id"], actor="alice")
    assert agent.calls == []
    assert svc.get(row["id"])["status"] == "cancelled"


def test_kind_metadata():
    q, svc, _ = _env(FakeAgent())
    k = svc.kind(tq.KIND)
    assert k.exclusive(_spec()) == [f"perf:{A1}"]
    assert k.label(_spec(tool="autotune")) == "Autotune · m · gpu-01"
    assert k.resume == "requeue" and k.limit_s(_spec()) == tq.MAX_RUN_S
    assert k.validate({"provider": "llama", "agent_id": A1, "tool": "nope"}, {})[1]
    assert k.validate({"provider": "lms", "agent_id": A1, "tool": "quality"}, {})[1]


# ── start-or-queue questions ──

def test_held_by_activity_by_live_row_and_by_batch():
    q, svc, _ = _env(FakeAgent(), held={A2})
    assert q.held(A2) is True and q.held(A1) is False
    svc.submit(tq.KIND, _spec(agent_id=A1), user="alice")
    assert q.held(A1) is True
    svc.register(jobs.Kind("autotune_batch", "Autotune batch", run=lambda j: jobs.ok(),
                           exclusive=lambda spec: [f"perf:{a}" for a in spec["agent_ids"]]))
    assert q.held("c" * 32) is False
    batch = svc.submit("autotune_batch", {"agent_ids": ["c" * 32]}, user="bob", not_before=5000.0)
    assert q.held("c" * 32) is False          # a batch that has not started holds nothing yet
    svc._store.update(batch["id"], status="running", started=1000.0, lease=1030.0)
    assert q.held("c" * 32) is True


def test_submit_positions_are_fifo_per_host_and_capped():
    q, svc, _ = _env(FakeAgent(), held={A1}, max_queued=2)
    first = q.submit(provider="llama", agent=AGENTS[A1], tool="benchmark", path="/llama/bench/run",
                     cancel="/llama/bench/cancel", body={"model_ids": ["org/m"]}, user="alice", role="operator")
    second = q.submit(provider="llama", agent=AGENTS[A1], tool="autotune", path="/llama/autotune/run",
                      cancel="/llama/autotune/cancel", body={"model_ids": ["org/n"]}, user="bob", role="operator")
    assert first["position"] == 1 and second["position"] == 2
    assert first["wait_for"] == "the run in progress"
    with pytest.raises(tq.QueueFull, match="2 runs already queued on gpu-01"):
        q.submit(provider="llama", agent=AGENTS[A1], tool="quality", path="/llama/autotune/run",
                 cancel="/llama/autotune/cancel", body={"model_ids": ["org/m"]}, user="alice", role="operator")
    other = q.submit(provider="llama", agent=AGENTS[A2], tool="benchmark", path="/llama/bench/run",
                     cancel="/llama/bench/cancel", body={}, user="alice", role="operator")
    assert other["position"] == 1
    row = svc.get(first["job_id"])
    assert row["spec"]["model_id"] == "org/m" and row["label"] == "Benchmark · m · gpu-01"
    assert row["exclusive"] == [f"perf:{A1}"] and row["source"] == "ui" and row["user"] == "alice"


def test_wait_for_names_the_holder():
    q, svc, _ = _env(FakeAgent(), held={A1}, holder={A1: "autotune"})
    assert q.wait_for(A1) == "Autotune on gpu-01"
    q2, svc2, _ = _env(FakeAgent())
    row = svc2.submit(tq.KIND, _spec(tool="quality"), user="alice")
    svc2._store.update(row["id"], status="running", started=1000.0, lease=2000.0)
    assert q2.wait_for(A1) == "Quality guard on gpu-01"


def test_snapshot_groups_by_host_running_first():
    q, svc, clock = _env(FakeAgent(), held={A1})
    a = svc.submit(tq.KIND, _spec(agent_id=A1, tool="benchmark"), user="alice")
    clock.t += 1
    b = svc.submit(tq.KIND, _spec(agent_id=A1, tool="autotune"), user="bob")
    clock.t += 1
    c = svc.submit(tq.KIND, _spec(agent_id=A2, tool="quality"), user="alice")
    svc._store.update(b["id"], status="running", started=clock.t, lease=clock.t + 30)
    snap = q.snapshot()
    assert [r["job_id"] for r in snap[A1]] == [b["id"], a["id"]]
    assert snap[A1][0]["status"] == "running" and snap[A1][1]["user"] == "alice"
    assert snap[A2] == [{"job_id": c["id"], "tool": "quality", "provider": "llama", "model_id": "org/m", "user": "alice",
                         "status": "queued", "created": c["created"], "path": "/llama/bench/run"}]


def test_can_wait_needs_a_readable_tools_state():
    q, _, _ = _env(FakeAgent(states=[IDLE]))
    assert q.can_wait(AGENTS[A1], "llama") is True
    q2, _, _ = _env(FakeAgent(states=[{"_status": 404}]))
    assert q2.can_wait(AGENTS[A1], "llama") is False
    q3, _, _ = _env(FakeAgent(states=[None]))
    assert q3.can_wait(AGENTS[A1], "llama") is False


def test_model_of_reads_every_body_shape():
    assert tq.model_of({"model_id": "a/b:Q4"}) == "a/b:Q4"
    assert tq.model_of({"model_ids": ["x/y", "z"]}) == "x/y"
    assert tq.model_of({"model": "vllm/m"}) == "vllm/m"
    assert tq.model_of({}) == "" and tq.model_of(None) == ""
