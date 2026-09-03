"""#782/#780: vLLM autotune serves the per-run replay buffer, both vLLM
wizards start their run under the busy lock, and both record ledger rows."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from tests._vllm_load import load_vllm

vllm = load_vllm()
from providers import _shared  # noqa: E402

VLLM_PY = Path(__file__).resolve().parents[1] / "providers" / "vllm.py"


def _fn_src(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |^# ──|^_ROUTES)", VLLM_PY.read_text(), re.M | re.S)
    assert m, f"could not extract {name}()"
    return m.group(0)


@pytest.fixture
def ctx(monkeypatch):
    posts = []

    class _Sess:
        def post(self, url, json=None, timeout=None, headers=None):
            posts.append({"url": url, "json": json, "headers": headers})

    class _Ctx:
        config = type("C", (), {"MANAGER_URL": "http://mgr:5000",
                                "VLLM_SYSTEMD_UNIT": "vllm.service",
                                "VLLM_API_URL": "http://127.0.0.1:8000"})()
        state = {"token": "tok", "agent_id": "agent1"}
        post_session = _Sess()

        def check_bearer(self, *_a, **_k):
            return None

        def check_stream_auth(self, *_a, **_k):
            return None

    c = _Ctx()
    c.posts = posts
    monkeypatch.setattr(vllm, "_require_ctx", lambda: c)
    monkeypatch.setattr(vllm, "_vllm_check_enabled", lambda: None)
    return c


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


# ── vLLM autotune stream: replay buffer wiring ─────────────────────────

def test_autotune_stream_route_uses_the_replay_buffer():
    src = _fn_src("vllm_autotune_stream")
    assert "bench_replay_sse" in src
    assert "_at_replay" in src and "_at_cond" in src
    assert 'alias="Last-Event-ID"' in src
    assert "sse_response" not in src


def test_autotune_events_land_in_the_replay_with_resumable_ids():
    vllm._at_start_run()
    run = vllm._at_replay.run_id
    vllm._at_job.put({"type": "step_start", "step": "probe"})
    vllm._at_job.put({"type": "done", "ok": True})
    frames = list(_shared.bench_replay_iter(
        vllm._at_replay, vllm._at_cond, lambda: True, None, wait_timeout=0.05))
    assert frames[0].startswith(f"id: {run}:1\n".encode())
    # A reconnect claiming it saw the first event only gets the terminal one.
    frames = list(_shared.bench_replay_iter(
        vllm._at_replay, vllm._at_cond, lambda: True, f"{run}:1", wait_timeout=0.05))
    assert len(frames) == 1 and b'"done"' in frames[0]


def test_autotune_and_bench_start_their_run_before_the_job_thread():
    for name, starter in (("vllm_autotune_run", "_at_start_run"),
                          ("vllm_bench_run", "_bench_start_run")):
        src = _fn_src(name)
        assert f"on_start={starter}" in src, name
    assert "start_run(" not in _fn_src("_bench_run_one")


# ── ledger rows ────────────────────────────────────────────────────────

def test_autotune_model_done_carries_run_id_and_posts_the_ledger_row(ctx, monkeypatch):
    unit = "[Service]\nExecStart=/opt/vllm/bin/vllm serve org/model-8b --max-model-len 8192\n"
    monkeypatch.setattr(vllm.Path, "read_text", lambda self: unit)
    monkeypatch.setattr(vllm, "_at_apply", lambda head, args: {"ok": True})
    monkeypatch.setattr(vllm, "_vllm_systemctl", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(vllm, "_at_restart_and_watch",
                        lambda unit, timeout_s, step: {"outcome": "kv", "kv_tokens": 460800,
                                                       "max_conc": 3.5})
    monkeypatch.setattr(vllm, "_at_wait_ready", lambda timeout_s=60.0: None)
    vllm._at_start_run()
    vllm._at_run({"probe_len": 4096, "concurrency": 1.0, "kv_fraction": 1.0,
                  "report_only": True, "load_timeout_s": 600})
    events = [r["event"] for r in vllm._at_replay.records_after_seq(0)]
    md = [e for e in events if e["type"] == "model_done"][0]
    assert md["model_id"] == "org/model-8b"
    assert md["run_id"] == vllm._at_replay.run_id
    assert len(ctx.posts) == 1
    body = ctx.posts[0]["json"]
    assert body["tool"] == "autotune" and body["provider"] == "vllm"
    assert body["model_id"] == "org/model-8b" and body["ok"] is True
    assert body["run_id"] == vllm._at_replay.run_id
    assert body["max_model_len"] == 460800 and body["report_only"] is True
    assert ctx.posts[0]["url"] == "http://mgr:5000/api/tools/runs"


def test_autotune_failure_posts_a_failed_row(ctx, monkeypatch):
    monkeypatch.setattr(vllm.Path, "read_text", lambda self: "[Service]\nnope\n")
    vllm._at_start_run()
    vllm._at_run({"probe_len": 4096, "concurrency": 1.0, "kv_fraction": 1.0,
                  "report_only": False, "load_timeout_s": 600})
    events = [r["event"] for r in vllm._at_replay.records_after_seq(0)]
    md = [e for e in events if e["type"] == "model_done"][0]
    assert md["ok"] is False and md["run_id"] == vllm._at_replay.run_id
    # No model id could be read, so nothing is posted (the ledger needs one).
    assert ctx.posts == []


def test_bench_model_done_carries_summary_and_posts_the_ledger_row(ctx, monkeypatch, tmp_path):
    import subprocess as _sp
    import sys as _sys

    result = json.dumps({"output_throughput": 1063.9, "total_token_throughput": 9800.2,
                         "backend": "vllm", "input_lens": [1, 2]})
    script = ("import pathlib, sys\n"
              "d = sys.argv[sys.argv.index('--result-dir') + 1]\n"
              "pathlib.Path(d, 'result.json').write_text(" + repr(result) + ")\n")
    real_popen = _sp.Popen
    monkeypatch.setattr(vllm.subprocess, "Popen",
                        lambda argv, **kw: real_popen([_sys.executable, "-c", script] + argv[3:], **kw))
    vllm._bench_start_run()
    vllm._bench_run_one("/opt/v/bin/vllm", "org/m", [])
    events = [r["event"] for r in vllm._bench_replay.records_after_seq(0)]
    res = [e for e in events if e["type"] == "result"][0]
    assert res["run_id"] == vllm._bench_replay.run_id
    md = [e for e in events if e["type"] == "model_done"][0]
    assert md["model_id"] == "org/m" and md["run_id"] == vllm._bench_replay.run_id
    assert md["gen_tps"] == 1063.9 and md["pg_tps"] == 9800.2
    assert md["bench_tool"] == "vllm-bench-serve"
    body = ctx.posts[0]["json"]
    assert body["tool"] == "benchmark" and body["provider"] == "vllm"
    assert body["model_id"] == "org/m" and body["ok"] is True
    assert body["gen_tps"] == 1063.9 and body["pg_tps"] == 9800.2
    assert body["run_id"] == vllm._bench_replay.run_id
