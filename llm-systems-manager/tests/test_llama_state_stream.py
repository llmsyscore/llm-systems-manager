"""
Regression tests for the llama-state SSE generator's lifetime cap (#213).

The always-on /api/llama-state/stream generator releases its pool slot only via
Response.call_on_close, which under Cheroot fires only when the response ends.
A half-open/stalled-but-ACKing browser keeps the keepalive writes succeeding, so
without a deadline the loop never ends and the slot is held until restart. These
pin that the generator self-terminates at max_lifetime_s regardless of the client.
"""
from __future__ import annotations

import queue as _queue
import time

from manager_mod import _llama_state_sse


def _drain(gen, budget_s=3.0):
    """Pull frames until StopIteration; bail (finished=False) at budget so a
    non-terminating regression FAILS fast instead of hanging the suite."""
    frames, finished = [], False
    t0 = time.monotonic()
    while True:
        try:
            frames.append(next(gen))
        except StopIteration:
            finished = True
            break
        if time.monotonic() - t0 > budget_s:
            gen.close()
            break
    return frames, finished


class TestLlamaStateStreamLifetime:
    def test_terminates_at_deadline_when_idle(self):
        q: _queue.Queue = _queue.Queue()
        done = []
        gen = _llama_state_sse("data: init\n\n", q, max_lifetime_s=0.3,
                               keepalive_s=0.05, on_finish=lambda: done.append(1))
        frames, finished = _drain(gen, budget_s=3.0)
        assert finished, "generator must return at the deadline, not loop forever"
        assert frames[0] == "data: init\n\n"
        assert any("keepalive" in f for f in frames[1:]), "idle keepalives expected"
        assert done == [1], "on_finish must run in the finally"

    def test_forwards_queued_messages_before_deadline(self):
        q: _queue.Queue = _queue.Queue()
        q.put("data: change\n\n")
        gen = _llama_state_sse("data: init\n\n", q, max_lifetime_s=1.0,
                               keepalive_s=0.05)
        assert next(gen) == "data: init\n\n"
        assert next(gen) == "data: change\n\n"
        gen.close()

    def test_shutdown_sentinel_ends_stream(self):
        q: _queue.Queue = _queue.Queue()
        q.put(None)  # shutdown sentinel
        done = []
        gen = _llama_state_sse("data: init\n\n", q, max_lifetime_s=10.0,
                               keepalive_s=0.05, on_finish=lambda: done.append(1))
        frames, finished = _drain(gen, budget_s=2.0)
        assert finished
        assert done == [1]

    def test_is_shutting_down_ends_stream(self):
        q: _queue.Queue = _queue.Queue()
        flag = {"v": False}
        gen = _llama_state_sse("data: init\n\n", q, max_lifetime_s=10.0,
                               keepalive_s=0.05, is_shutting_down=lambda: flag["v"])
        assert next(gen) == "data: init\n\n"
        assert "keepalive" in next(gen)
        flag["v"] = True
        _frames, finished = _drain(gen, budget_s=2.0)
        assert finished

    def test_on_finish_runs_on_generator_close(self):
        q: _queue.Queue = _queue.Queue()
        done = []
        gen = _llama_state_sse("data: init\n\n", q, max_lifetime_s=10.0,
                               keepalive_s=0.05, on_finish=lambda: done.append(1))
        assert next(gen) == "data: init\n\n"
        gen.close()  # client disconnect -> GeneratorExit -> finally
        assert done == [1]
