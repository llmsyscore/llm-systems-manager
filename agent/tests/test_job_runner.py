# agent/tests/test_job_runner.py
"""#356: JobRunner — single-job guard, bounded queue, cancel, SSE framing."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

from tests import _vllm_load  # noqa: F401  # installs requests/fastapi stubs
from providers import _shared


def test_try_start_rejects_second_job():
    job = _shared.JobRunner("t")
    release = threading.Event()
    assert job.try_start(release.wait) is True
    assert job.try_start(lambda: None) is False
    release.set()
    for _ in range(100):
        if not job.active:
            break
        time.sleep(0.01)
    assert job.active is False
    assert job.try_start(lambda: None) is True


def test_queue_drops_oldest_when_full():
    job = _shared.JobRunner("t", maxsize=2)
    job.put({"n": 1})
    job.put({"n": 2})
    job.put({"n": 3})
    assert job.queue.get_nowait() == {"n": 2}
    assert job.queue.get_nowait() == {"n": 3}


def test_target_exception_emits_done_and_clears_active():
    job = _shared.JobRunner("t")

    def boom():
        raise RuntimeError("kaput")

    assert job.try_start(boom) is True
    msg = job.queue.get(timeout=5)
    assert msg["type"] == "done" and msg["ok"] is False and "kaput" in msg["error"]
    for _ in range(100):
        if not job.active:
            break
        time.sleep(0.01)
    assert job.active is False


def test_cancel_kills_tracked_process_group():
    job = _shared.JobRunner("t")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    job.track(proc)
    assert job.cancel() is True
    assert job.cancel_event.is_set()
    assert proc.wait(timeout=5) is not None


def test_sse_generator_frames_and_stops_on_done():
    job = _shared.JobRunner("t")
    job.put({"type": "line", "text": "x"})
    job.put({"type": "done", "ok": True})
    frames = list(job._sse_iter(idle_timeout=1.0))
    events = [json.loads(f.decode().removeprefix("data: ").strip()) for f in frames]
    assert events[0] == {"type": "line", "text": "x"}
    assert events[-1]["type"] == "done"


def test_sse_generator_keepalive_on_idle():
    job = _shared.JobRunner("t")
    job.active = True
    gen = job._sse_iter(idle_timeout=0.05)
    assert next(gen) == b'data: {"type":"keepalive"}\n\n'


def test_sse_generator_ends_when_no_job_active():
    job = _shared.JobRunner("t")
    frames = list(job._sse_iter(idle_timeout=0.05))
    assert len(frames) == 1
    msg = json.loads(frames[0].decode().removeprefix("data: ").strip())
    assert msg["type"] == "done" and msg["ok"] is False


def test_cancel_before_start_does_not_leak_into_next_run():
    job = _shared.JobRunner("t")
    job.cancel()
    seen = {}
    assert job.try_start(lambda: seen.update(c=job.cancel_event.is_set())) is True
    job.join(timeout=5)
    assert seen == {"c": False}
