# agent/tests/test_log_stream.py
"""#672: LogStream fan-out — no backlog without subscribers, per-subscriber
bounded queues, stale-subscriber eviction, SSE framing + unsubscribe."""
from __future__ import annotations

import json
import time

from tests._vllm_load import load_vllm

load_vllm()  # stubs requests/fastapi before the providers import below

from providers import _shared  # noqa: E402


def _pump(ls, *lines):
    ls.streaming = True
    ls.pump(["sh", "-c", "; ".join(f"echo {ln}" for ln in lines)])


def _drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def test_lines_without_subscribers_are_dropped_not_buffered():
    ls = _shared.LogStream()
    _pump(ls, "a", "b")
    sub = ls.subscribe()
    assert _drain(sub) == []


def test_subscriber_receives_lines_and_fanout_duplicates():
    ls = _shared.LogStream()
    s1, s2 = ls.subscribe(), ls.subscribe()
    _pump(ls, "a", "b")
    assert _drain(s1) == ["a", "b"]
    assert _drain(s2) == ["a", "b"]


def test_per_subscriber_queue_keeps_newest():
    ls = _shared.LogStream(maxsize=2)
    sub = ls.subscribe()
    for ln in ("a", "b", "c"):
        ls.publish(ln)
    assert _drain(sub) == ["b", "c"]


def test_stale_subscriber_is_evicted_on_publish():
    ls = _shared.LogStream()
    dead, live = ls.subscribe(), ls.subscribe()
    dead.seen = time.monotonic() - _shared.LogStream.SUB_STALE_S - 1
    ls.publish("x")
    assert ls.subscriber_count == 1
    assert _drain(live) == ["x"]
    assert _drain(dead) == []


def test_should_keep_filters_before_publish():
    ls = _shared.LogStream()
    sub = ls.subscribe()
    ls.streaming = True
    ls.pump(["sh", "-c", "echo keep; echo drop"], should_keep=lambda ln: ln != "drop")
    assert _drain(sub) == ["keep"]


def test_sse_iter_frames_keepalive_and_unsubscribes_on_close():
    ls = _shared.LogStream()
    sub = ls.subscribe()
    ls.publish("hello")
    it = ls._sse_iter(sub, idle_timeout=0.05)
    first = json.loads(next(it).decode().removeprefix("data: ").strip())
    assert first == {"line": "hello"}
    second = json.loads(next(it).decode().removeprefix("data: ").strip())
    assert second == {"keepalive": True}
    assert ls.subscriber_count == 1
    it.close()
    assert ls.subscriber_count == 0
