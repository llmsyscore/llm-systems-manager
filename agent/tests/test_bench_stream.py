"""#377: shared bench replay-SSE generator — resume, keepalive, idle-done,
supersede. Both llama_bench_stream and vllm_bench_stream delegate to it."""
from __future__ import annotations

import json
import threading

import _bench_replay
from tests._vllm_load import load_vllm

load_vllm()  # stub requests/fastapi before importing providers._shared

from providers import _shared  # noqa: E402

BenchReplayBuffer = _bench_replay.BenchReplayBuffer


def _payload(frame: bytes) -> dict:
    for line in frame.decode().splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"no data line in frame: {frame!r}")


def _make():
    buf = BenchReplayBuffer(maxlen=100)
    buf.start_run("r1")
    return buf, threading.Condition()


def test_emits_records_and_stops_on_done():
    buf, cond = _make()
    buf.append({"type": "line", "text": "x"})
    buf.append({"type": "done", "ok": True})
    frames = list(_shared.bench_replay_iter(buf, cond, lambda: True, None,
                                            wait_timeout=0.05))
    events = [_payload(f) for f in frames]
    assert events[0] == {"type": "line", "text": "x"}
    assert events[-1]["type"] == "done"
    assert frames[0].startswith(b"id: r1:1\n")  # id: line present for resume


def test_keepalive_while_active_and_idle():
    buf, cond = _make()
    gen = _shared.bench_replay_iter(buf, cond, lambda: True, None,
                                    wait_timeout=0.02)
    assert next(gen) == b'data: {"type":"keepalive"}\n\n'


def test_idle_stream_terminates_when_not_active():
    # #377: the fix brought to llama — an idle stream with no active job ends
    # with a synthetic done instead of keepaliving forever (frees the pool slot).
    buf, cond = _make()
    frames = list(_shared.bench_replay_iter(buf, cond, lambda: False, None,
                                            wait_timeout=0.02))
    assert len(frames) == 1
    msg = _payload(frames[0])
    assert msg["type"] == "done" and msg["ok"] is False
    assert msg["error"] == "no active job"


def test_resume_from_last_event_id_skips_replayed_records():
    buf, cond = _make()
    buf.append({"type": "line", "text": "a"})
    r2 = buf.append({"type": "line", "text": "b"})
    buf.append({"type": "done", "ok": True})
    frames = list(_shared.bench_replay_iter(buf, cond, lambda: True, r2["id"],
                                            wait_timeout=0.05))
    events = [_payload(f) for f in frames]
    assert events == [{"type": "done", "ok": True}]  # a/b already delivered


def test_superseded_run_ends_stream():
    buf, cond = _make()
    buf.append({"type": "line", "text": "a"})
    gen = _shared.bench_replay_iter(buf, cond, lambda: True, None,
                                    wait_timeout=0.02)
    assert _payload(next(gen)) == {"type": "line", "text": "a"}
    buf.start_run("r2")  # a newer run supersedes the one this stream follows
    try:
        next(gen)
        raise AssertionError("stream must stop after run supersession")
    except StopIteration:
        pass
