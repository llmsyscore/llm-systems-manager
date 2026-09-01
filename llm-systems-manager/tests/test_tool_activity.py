"""#775/#776: fleet-wide tool run state — record, confirm, expire, expose."""
from __future__ import annotations

import pytest

import tool_activity as ta

A1, A2 = "a" * 32, "b" * 32


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


def _wire(states, rc_agents=(), calls=None):
    """states: agent_id (or (agent_id, provider)) -> the /tools/state body,
    or None for an agent too old to answer the probe."""
    def _call(method, agent, path, **kw):
        aid = agent["agent_id"]
        provider = path.strip("/").split("/")[0]
        if calls is not None:
            calls.append((aid, path))
        body = states.get((aid, provider), states.get(aid))
        return None if body is None else _Resp(body)

    ta.configure(agent_for=lambda aid: {"agent_id": aid, "token": "t"},
                 agent_call=_call,
                 reportcard_active=lambda: list(rc_agents))


@pytest.fixture(autouse=True)
def _clean():
    ta.reset()
    ta.configure(agent_for=lambda aid: None, agent_call=None,
                 reportcard_active=lambda: [])
    yield
    ta.reset()


def test_started_run_shows_before_any_probe():
    _wire({})
    ta.note_start(A1, "llama", "benchmark", now=1000.0)
    snap = ta.snapshot(sync=True, now=1001.0)          # inside the start grace
    assert snap["benchmark"] is True
    assert snap["agents"] == {A1: ["benchmark"]}


def test_probe_confirming_active_keeps_the_run():
    _wire({A1: {"bench_active": True, "autotune_active": False}})
    ta.note_start(A1, "llama", "benchmark", now=1000.0)
    assert ta.snapshot(sync=True, now=1010.0)["benchmark"] is True


def test_a_probe_confirms_per_provider_not_per_agent():
    """One host can serve llama and vLLM; a llama probe must not decide the
    fate of a vLLM run cached under the same agent id."""
    _wire({(A1, "llama"): {"bench_active": True, "autotune_active": False},
           (A1, "vllm"): {"bench_active": False, "autotune_active": True}})
    ta.note_start(A1, "llama", "benchmark", now=1000.0)
    ta.note_start(A1, "vllm", "autotune", now=1000.0)
    snap = ta.snapshot(sync=True, now=1010.0)
    assert snap["benchmark"] is True and snap["autotune"] is True


def test_probe_reporting_idle_clears_the_run():
    _wire({A1: {"bench_active": False, "autotune_active": False}})
    ta.note_start(A1, "llama", "benchmark", now=1000.0)
    snap = ta.snapshot(sync=True, now=1100.0)
    assert snap["benchmark"] is False
    assert snap["agents"] == {}


def test_unknown_tool_and_blank_agent_are_ignored():
    ta.note_start(A1, "llama", "nmap")
    ta.note_start("", "llama", "benchmark")
    assert ta.snapshot()["agents"] == {}


def test_old_agent_without_the_probe_drops_out_instead_of_sticking():
    """A pre-probe agent must not pin a 'running' pill on every dashboard."""
    _wire({A1: None})
    ta.note_start(A1, "llama", "autotune", now=1000.0)
    assert ta.snapshot(sync=True, now=1010.0)["autotune"] is True
    later = 1000.0 + ta.UNCONFIRMED_MAX_S + 1
    assert ta.snapshot(sync=True, now=later)["autotune"] is False


def test_probe_result_is_cached_within_its_ttl():
    calls = []
    _wire({A1: {"bench_active": True, "autotune_active": False}}, calls=calls)
    ta.note_start(A1, "llama", "benchmark", now=1000.0)
    ta.snapshot(sync=True, now=1100.0)
    ta.snapshot(sync=True, now=1100.0 + ta.PROBE_TTL_S / 2)
    assert len(calls) == 1
    ta.snapshot(sync=True, now=1100.0 + ta.PROBE_TTL_S + 1)
    assert len(calls) == 2


def test_note_end_drops_the_run_immediately():
    _wire({A1: {"bench_active": True, "autotune_active": False}})
    ta.note_start(A1, "llama", "benchmark", now=1000.0)
    ta.note_end(A1, "benchmark")
    assert ta.snapshot(sync=True, now=1100.0)["benchmark"] is False


def test_report_card_jobs_come_from_the_manager_not_a_probe():
    _wire({}, rc_agents=(A2,))
    snap = ta.snapshot(sync=True, now=1000.0)
    assert snap["reportcard"] is True
    assert snap["agents"] == {A2: ["reportcard"]}


def test_busy_agents_unions_every_tool():
    _wire({A1: {"bench_active": True, "autotune_active": False}}, rc_agents=(A2,))
    ta.note_start(A1, "llama", "benchmark", now=1000.0)
    ta.snapshot(sync=True, now=1010.0)
    assert ta.busy_agents(now=1010.0) == {A1, A2}


def test_a_failing_probe_does_not_raise():
    def _boom(*a, **kw):
        raise RuntimeError("agent unreachable")

    ta.configure(agent_for=lambda aid: {"agent_id": aid, "token": "t"},
                 agent_call=_boom, reportcard_active=lambda: [])
    ta.note_start(A1, "llama", "benchmark", now=1000.0)
    assert ta.snapshot(sync=True, now=1010.0)["benchmark"] is True
