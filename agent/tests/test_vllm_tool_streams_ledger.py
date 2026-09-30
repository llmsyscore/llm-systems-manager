"""#782/#780: JobRunner sink/on_start, and the vLLM bench runner starts its run under the busy lock."""
from __future__ import annotations

import re
import threading
from pathlib import Path

from tests._vllm_load import load_vllm

vllm = load_vllm()
from providers import _shared  # noqa: E402

VLLM_PY = Path(__file__).resolve().parents[1] / "providers" / "vllm.py"
VLLM_TOOLS_PY = VLLM_PY.with_name("vllm_tools.py")


def _fn_src(name: str, path: Path = VLLM_PY) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |^# ──|^_ROUTES)", path.read_text(), re.M | re.S)
    assert m, f"could not extract {name}()"
    return m.group(0)


# ── JobRunner: sink + on_start ─────────────────────────────────────────

def test_job_runner_sink_receives_events_instead_of_the_queue():
    seen = []
    job = _shared.JobRunner("t", sink=seen.append)
    job.put({"type": "line", "text": "x"})
    assert seen == [{"type": "line", "text": "x"}]
    assert job.queue.empty()


def test_job_runner_on_start_runs_under_the_busy_lock_before_the_thread():
    order = []
    job = _shared.JobRunner("t")
    started = threading.Event()

    def on_start():
        order.append(("on_start", job.active, job.lock.locked()))

    def target():
        order.append(("target",))
        started.set()

    assert job.try_start(target, on_start=on_start)
    started.wait(2)
    job.join(2)
    assert order[0] == ("on_start", True, True)
    assert order[1] == ("target",)


def test_job_runner_target_exception_reaches_the_sink():
    seen = []
    job = _shared.JobRunner("t", sink=seen.append)

    def boom():
        raise RuntimeError("boom")

    assert job.try_start(boom)
    job.join(2)
    assert seen[-1]["type"] == "done" and seen[-1]["ok"] is False
    assert job.active is False


# ── vLLM bench: run opened before the job thread ──────────────────────

def test_bench_starts_its_run_before_the_job_thread():
    bench_run = _fn_src("vllm_bench_run", VLLM_TOOLS_PY)
    assert bench_run.index("_claim(") < bench_run.index("threading.Thread(")
    assert "start_run(" not in _fn_src("_bench_run_one", VLLM_TOOLS_PY)
