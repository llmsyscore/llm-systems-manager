"""#897: _tool_start routing — proxy when idle, queue when held or refused, 409 when the queue is full."""
from __future__ import annotations

import json

import pytest

import manager_mod
import tool_queue

A1 = "a" * 32
AGENT = {"agent_id": A1, "hostname": "gpu-01", "token": "t"}


class FakeQueue:
    def __init__(self, held=False, can_wait=True, full=None):
        self._held, self._can_wait, self._full = held, can_wait, full
        self.submits = []

    def held(self, aid):
        return self._held

    def can_wait(self, agent, provider):
        return self._can_wait

    def submit(self, **kw):
        self.submits.append(kw)
        if self._full:
            raise tool_queue.QueueFull(self._full)
        return {"job_id": "j1", "position": 1, "wait_for": "Autotune on gpu-01"}

    def snapshot(self):
        return {}


def _ok_response(body, status=200):
    return manager_mod.app.response_class(json.dumps(body), status=status, mimetype="application/json")


@pytest.fixture
def env(monkeypatch):
    proxied = []

    def setup(queue, answer):
        def fake_proxy(provider, method, path, **kw):
            proxied.append((provider, method, path))
            return answer() if isinstance(answer, type(_start)) else answer
        monkeypatch.setattr(manager_mod, "_tool_queue", queue)
        monkeypatch.setattr(manager_mod, "_request_agent", lambda provider: dict(AGENT))
        monkeypatch.setattr(manager_mod.proxies, "proxy_to_primary", fake_proxy)
        monkeypatch.setattr(manager_mod.tower, "session_user", lambda session: "alice")
        monkeypatch.setattr(manager_mod.auth, "effective_role", lambda: "operator")
        return proxied
    return setup


def _start(**kw):
    with manager_mod.app.test_request_context():
        out = manager_mod._tool_start("llama", "benchmark", "/llama/bench/run", "/llama/bench/cancel",
                                      {"model_ids": ["org/m"]}, **kw)
        if isinstance(out, tuple):
            return out[0].get_json(), out[1], out
        return out.get_json(), out.status_code, out


def test_idle_host_returns_the_proxied_answer(env):
    q = FakeQueue(held=False)
    answer = _ok_response({"ok": True, "run_id": "r1"})
    proxied = env(q, answer)
    body, status, raw = _start()
    assert raw is answer and status == 200 and body == {"ok": True, "run_id": "r1"}
    assert proxied == [("llama", "POST", "/llama/bench/run")] and q.submits == []


def test_held_host_queues_with_202(env):
    q = FakeQueue(held=True)
    proxied = env(q, lambda: _ok_response({"ok": True}))
    body, status, _ = _start(pre="/llama/server/stop")
    assert status == 202 and proxied == []
    assert body["queued"] is True and body["job_id"] == "j1" and body["position"] == 1
    assert body["wait_for"] == "Autotune on gpu-01" and body["agent_id"] == A1
    assert q.submits[0]["user"] == "alice" and q.submits[0]["role"] == "operator"
    assert q.submits[0]["pre"] == "/llama/server/stop"


def test_proxied_refusal_queues_with_202(env):
    q = FakeQueue(held=False)
    env(q, lambda: _ok_response({"ok": False, "error": "a benchmark is already in progress"}))
    body, status, _ = _start()
    assert status == 202 and body["queued"] is True and len(q.submits) == 1


def test_held_but_unprobeable_host_passes_the_proxied_answer_through(env):
    q = FakeQueue(held=True, can_wait=False)
    answer = _ok_response({"ok": True, "run_id": "r2"})
    proxied = env(q, answer)
    body, status, raw = _start()
    assert raw is answer and status == 200 and q.submits == [] and len(proxied) == 1


def test_full_queue_answers_409(env):
    q = FakeQueue(held=True, full="5 runs already queued on gpu-01")
    env(q, lambda: _ok_response({"ok": True}))
    body, status, _ = _start()
    assert status == 409 and body == {"ok": False, "error": "5 runs already queued on gpu-01"}


def test_proxy_error_tuple_is_not_a_refusal(env):
    q = FakeQueue(held=False)

    def err():
        return manager_mod.jsonify({"ok": False, "error": "a benchmark is already in progress"}), 502
    env(q, err)
    body, status, raw = _start()
    assert isinstance(raw, tuple) and status == 502 and q.submits == []


def test_activity_stamps_rows_the_session_owns(env):
    q = FakeQueue()
    q.snapshot = lambda: {A1: [{"job_id": "j1", "user": "alice"}, {"job_id": "j2", "user": "bypass:x"}]}
    env(q, lambda: None)
    with manager_mod.app.test_request_context():
        rows = manager_mod.tool_activity_get().get_json()["queue"][A1]
    assert [r["mine"] for r in rows] == [True, False]


def test_tool_refused_reads_only_a_200_refusal_body():
    with manager_mod.app.test_request_context():
        refused = manager_mod.jsonify({"ok": False, "error": "a benchmark is already in progress"})
        assert manager_mod._tool_refused((refused, 200)) is True
        assert manager_mod._tool_refused((refused, 502)) is False
        assert manager_mod._tool_refused(_ok_response({"ok": False, "error": "model not found"})) is False
        assert manager_mod._tool_refused(_ok_response({"ok": True})) is False


def test_index_mints_one_bypass_session_id_before_the_first_api_call():
    """#1132: parallel first requests must share the id the queue rows are stamped with."""
    with manager_mod.app.test_request_context("/"):
        manager_mod._flask_session.clear()
        manager_mod.index()
        uid = manager_mod._flask_session.get("tower_uid")
        assert uid and manager_mod.tower.session_user(manager_mod._flask_session) == f"bypass:{uid}"
