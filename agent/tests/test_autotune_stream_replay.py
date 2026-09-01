"""#778: llama autotune stream serves the per-run replay buffer — a
reconnecting EventSource resumes from Last-Event-ID."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import _bench_replay
from tests._vllm_load import load_vllm

load_vllm()  # stub requests/fastapi before importing providers._shared

from providers import _shared  # noqa: E402

LLAMA_PY = Path(__file__).resolve().parents[1] / "providers" / "llama.py"


def _extract_put(replay, cond):
    src = LLAMA_PY.read_text()
    m = re.search(r"^def _autotune_put\(.*?(?=^\S)", src, re.M | re.S)
    assert m, "could not extract _autotune_put()"
    ns = {"_autotune_replay": replay, "_autotune_cond": cond}
    exec(compile(m.group(0), str(LLAMA_PY), "exec"), ns)
    return ns["_autotune_put"]


def _payload(frame: bytes) -> dict:
    for line in frame.decode().splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"no data line in frame: {frame!r}")


def _make():
    replay = _bench_replay.BenchReplayBuffer(maxlen=100)
    replay.start_run("at1")
    cond = threading.Condition()
    return replay, cond, _extract_put(replay, cond)


def test_put_appends_with_ids_and_done_ends_stream():
    replay, cond, put = _make()
    put({"type": "model_start", "model_id": "m"})
    put({"type": "done", "ok": True})
    frames = list(_shared.bench_replay_iter(replay, cond, lambda: True, None,
                                            wait_timeout=0.05))
    assert frames[0].startswith(b"id: at1:1\n")
    events = [_payload(f) for f in frames]
    assert events[0]["type"] == "model_start"
    assert events[-1]["type"] == "done"


def test_reconnect_resumes_after_last_event_id():
    replay, cond, put = _make()
    put({"type": "iter_start", "iter": 1})
    put({"type": "iter_result", "iter": 1})
    put({"type": "done", "ok": True})
    # Reconnect claiming it saw at1:1 — replay skips it, terminal still arrives.
    frames = list(_shared.bench_replay_iter(replay, cond, lambda: False, "at1:1",
                                            wait_timeout=0.05))
    events = [_payload(f) for f in frames]
    assert [e["type"] for e in events] == ["iter_result", "done"]


def test_terminal_survives_a_prior_consumer():
    # The replay buffer re-serves the done event to every (re)connect.
    replay, cond, put = _make()
    put({"type": "done", "ok": True})
    for _ in range(2):
        frames = list(_shared.bench_replay_iter(replay, cond, lambda: False,
                                                None, wait_timeout=0.05))
        assert _payload(frames[-1])["type"] == "done"


def test_run_resets_buffer_while_still_holding_the_run_lock():
    # A stream landing between active=True and start_run() would replay the
    # prior run's stale done and abandon the new run.
    src = LLAMA_PY.read_text()
    for fn, lock, cond in (("llama_autotune_run", "_autotune_lock", "_autotune_cond"),
                           ("llama_bench_run", "_bench_lock", "_bench_cond")):
        m = re.search(rf"^def {fn}\(.*?(?=^def )", src, re.M | re.S)
        assert m, f"{fn} not found"
        body = m.group(0)
        lock_at = body.index(f"with {lock}:")
        cond_at = body.index(f"with {cond}:")
        start_at = body.index("start_run(")
        assert lock_at < cond_at < start_at, f"{fn}: start_run escaped {lock}"
        # The cond block is indented under the lock block, not a sibling.
        cond_line = body[body.rindex("\n", 0, cond_at) + 1:cond_at]
        lock_line = body[body.rindex("\n", 0, lock_at) + 1:lock_at]
        assert len(cond_line) > len(lock_line), f"{fn}: {cond} not nested in {lock}"


def test_stream_route_wiring_uses_replay():
    src = LLAMA_PY.read_text()
    m = re.search(r"^def llama_autotune_stream\(.*?(?=^def )", src, re.M | re.S)
    assert m, "llama_autotune_stream not found"
    body = m.group(0)
    assert "bench_replay_sse" in body
    assert "_autotune_replay" in body and "_autotune_cond" in body
    assert 'alias="Last-Event-ID"' in body
