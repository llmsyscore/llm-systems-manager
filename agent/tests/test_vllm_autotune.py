# agent/tests/test_vllm_autotune.py
"""#356: vLLM autotune — journal parsing, recommendation math, arg edits,
run-endpoint validation, busy guard, rollback-on-failure."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._vllm_load import load_vllm

vllm = load_vllm()


# ── journal line parsing ────────────────────────────────────────────────

def test_kv_size_line_parses_with_commas():
    m = vllm._AT_KV_SIZE_RE.search("INFO ... GPU KV cache size: 230,528 tokens")
    assert m and vllm._at_num(m.group(1)) == 230528


def test_max_concurrency_line_parses():
    m = vllm._AT_MAX_CONC_RE.search(
        "Maximum concurrency for 100,000 tokens per request: 2.31x")
    assert m and vllm._at_num(m.group(1)) == 100000
    assert float(m.group(2)) == 2.31


def test_estimated_max_len_error_parses():
    line = ("ValueError: To serve at least one request with the model's max seq "
            "len (221000), 10.12 GiB KV cache is needed, which is larger than "
            "the available KV cache memory (5.59 GiB). Based on the available "
            "memory, the estimated maximum model length is 56736.")
    m = vllm._AT_EST_MAX_RE.search(line)
    assert m and vllm._at_num(m.group(1)) == 56736


def test_old_kv_capacity_error_parses():
    line = ("ValueError: The model's max seq len (131072) is larger than the "
            "maximum number of tokens that can be stored in KV cache (56736).")
    m = vllm._AT_KV_CAP_OLD_RE.search(line)
    assert m and vllm._at_num(m.group(1)) == 56736


def test_fatal_matches_engine_failures_but_not_info_lines():
    assert vllm._AT_FATAL_RE.search("EngineCore failed to start.")
    assert vllm._AT_FATAL_RE.search("torch.OutOfMemoryError: CUDA out of memory")
    assert not vllm._AT_FATAL_RE.search("INFO: Started server process [123]")


# ── recommendation math ────────────────────────────────────────────────

def test_compute_floors_to_multiple_of_256():
    assert vllm.compute_recommended_max_len(230528) == 230400
    assert vllm.compute_recommended_max_len(230528, concurrency=2.0) == 115200


def test_compute_applies_kv_fraction_and_floor():
    assert vllm.compute_recommended_max_len(10000, kv_fraction=0.5) == 4864
    assert vllm.compute_recommended_max_len(100) == 256  # floor at 256


def test_compute_clamps_bad_inputs():
    assert vllm.compute_recommended_max_len(10000, concurrency=0.0) == \
        vllm.compute_recommended_max_len(10000, concurrency=1.0)
    assert vllm.compute_recommended_max_len(10000, kv_fraction=5.0) == \
        vllm.compute_recommended_max_len(10000, kv_fraction=1.0)


# ── ExecStart arg transforms ───────────────────────────────────────────

ARGS = [{"flag": "--host", "value": "0.0.0.0", "bool": False},
        {"flag": "--max-model-len", "value": "8192", "bool": False}]


def test_get_max_len_reads_value_or_none():
    assert vllm._at_get_max_len(ARGS) == 8192
    assert vllm._at_get_max_len([ARGS[0]]) is None


def test_args_with_max_len_replaces_without_mutating_input():
    out = vllm._at_args_with_max_len(ARGS, 4096)
    assert {"flag": "--max-model-len", "value": "4096", "bool": False} in out
    assert sum(1 for a in out if a["flag"] == "--max-model-len") == 1
    assert ARGS[1]["value"] == "8192"


def test_args_with_max_len_appends_when_absent():
    out = vllm._at_args_with_max_len([ARGS[0]], 4096)
    assert out[-1] == {"flag": "--max-model-len", "value": "4096", "bool": False}


EQ_ARGS = [{"flag": "--max-model-len=8192", "value": None, "bool": True}]


def test_get_max_len_reads_equals_form():
    assert vllm._at_get_max_len(EQ_ARGS) == 8192


def test_args_with_max_len_strips_equals_form():
    out = vllm._at_args_with_max_len(EQ_ARGS, 4096)
    assert out == [{"flag": "--max-model-len", "value": "4096", "bool": False}]


# ── journal watcher (fake journalctl via a print-then-idle python child) ─

import subprocess
import sys


@pytest.fixture
def ctx():
    cfg = SimpleNamespace(
        VLLM_ENABLED=True, VLLM_LORA_ENABLED=False,
        VLLM_SYSTEMD_UNIT="vllm.service",
        VLLM_API_URL="http://localhost:8000",
    )
    context = SimpleNamespace(config=cfg, check_bearer=lambda *_: None,
                              check_stream_auth=lambda *a, **k: None)
    vllm.set_context(context)
    return context


def _fake_journal(monkeypatch, lines, hold_open=True):
    """Replace journalctl Popen with a child that prints lines then idles."""
    script = "import sys,time\n"
    for l in lines:
        script += f"print({l!r}, flush=True)\n"
    if hold_open:
        script += "time.sleep(60)\n"
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        assert argv[0] == "journalctl"
        return real_popen([sys.executable, "-c", script],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, start_new_session=True)
    monkeypatch.setattr(vllm.subprocess, "Popen", popen)


def test_watch_returns_kv_and_concurrency(ctx, monkeypatch):
    _fake_journal(monkeypatch, [
        "INFO loading model...",
        "GPU KV cache size: 230,528 tokens",
        "Maximum concurrency for 8,192 tokens per request: 28.14x",
    ])
    r = vllm._at_watch_journal("vllm.service", timeout_s=15, step="probe")
    assert r["outcome"] == "kv" and r["kv_tokens"] == 230528
    assert r["max_conc"] == 28.14


def test_watch_estimated_max_beats_fatal_on_same_line(ctx, monkeypatch):
    _fake_journal(monkeypatch, [
        "ValueError: ... the estimated maximum model length is 56736.",
    ])
    r = vllm._at_watch_journal("vllm.service", timeout_s=15, step="probe")
    assert r["outcome"] == "est_max" and r["est_max_len"] == 56736


def test_watch_fatal_line(ctx, monkeypatch):
    _fake_journal(monkeypatch, ["EngineCore failed to start."])
    r = vllm._at_watch_journal("vllm.service", timeout_s=15, step="probe")
    assert r["outcome"] == "fatal" and "EngineCore" in r["fatal_line"]


def test_watch_timeout(ctx, monkeypatch):
    _fake_journal(monkeypatch, ["INFO still loading..."])
    r = vllm._at_watch_journal("vllm.service", timeout_s=2, step="probe")
    assert r["outcome"] == "timeout"


def test_watch_kv_returns_quickly_on_quiet_journal(ctx, monkeypatch):
    import time as _time
    _fake_journal(monkeypatch, ["GPU KV cache size: 230,528 tokens"])
    monkeypatch.setattr(vllm, "_AT_CONC_GRACE_S", 0.5)
    t0 = _time.monotonic()
    r = vllm._at_watch_journal("vllm.service", timeout_s=30, step="probe")
    assert r["outcome"] == "kv" and r["kv_tokens"] == 230528
    assert _time.monotonic() - t0 < 10


def test_watch_cancel(ctx, monkeypatch):
    _fake_journal(monkeypatch, ["INFO still loading..."])
    vllm._at_job.cancel_event.set()
    try:
        r = vllm._at_watch_journal("vllm.service", timeout_s=10, step="probe")
    finally:
        vllm._at_job.cancel_event.clear()
    assert r["outcome"] == "cancelled"


# ── run endpoint validation + busy guard ───────────────────────────────

from fastapi import HTTPException


def test_run_rejects_bad_params(ctx):
    for body in ({"probe_len": 100}, {"concurrency": 0.5},
                 {"kv_fraction": 0.01}, {"load_timeout_s": 5},
                 {"probe_len": "nope"}, {"probe_len": 0},
                 {"kv_fraction": 0}, {"concurrency": 0}):
        with pytest.raises(HTTPException) as ei:
            vllm.vllm_autotune_run(body)
        assert ei.value.status_code == 400


def test_run_busy_guard(ctx, monkeypatch):
    import threading
    release = threading.Event()
    monkeypatch.setattr(vllm, "_at_run", lambda params: release.wait())
    try:
        assert vllm.vllm_autotune_run({}) == {"ok": True}
        r = vllm.vllm_autotune_run({})
        assert r["ok"] is False and "in progress" in r["error"]
    finally:
        release.set()
        for _ in range(100):
            if not vllm._at_job.active:
                break
            import time as _t
            _t.sleep(0.01)


# ── orchestration: rollback + report-only (all side effects faked) ─────

def _drain(job):
    out = []
    while not job.queue.empty():
        out.append(job.queue.get_nowait())
    return out


UNIT_TEXT = """[Service]
ExecStart=/opt/vllm/bin/vllm serve org/model-8b --host 0.0.0.0 --max-model-len 8192
"""


def _wire_orchestration(monkeypatch, watch_results, applied):
    """Fake svc file read, svcconfig writes (recorded), restarts, watcher."""
    monkeypatch.setattr(vllm.Path, "read_text", lambda self: UNIT_TEXT)
    monkeypatch.setattr(vllm, "_at_apply",
                        lambda head, args: (applied.append(args), {"ok": True})[1])
    monkeypatch.setattr(vllm, "_vllm_systemctl", lambda *a, **k: {"ok": True})
    seq = iter(watch_results)
    monkeypatch.setattr(vllm, "_at_restart_and_watch",
                        lambda unit, timeout_s, step: next(seq))
    monkeypatch.setattr(vllm, "_at_wait_ready", lambda timeout_s=60.0: None)


def test_probe_failure_rolls_back_to_original(ctx, monkeypatch):
    applied = []
    _wire_orchestration(monkeypatch, [
        {"outcome": "fatal", "fatal_line": "EngineCore failed", "max_conc": None},
    ], applied)
    vllm._at_run({"probe_len": 4096, "concurrency": 1.0, "kv_fraction": 1.0,
                  "report_only": False, "load_timeout_s": 600})
    msgs = _drain(vllm._at_job)
    md = [m for m in msgs if m["type"] == "model_done"][0]
    assert md["ok"] is False and "EngineCore" in md["error"]
    # last svcconfig write restores the original 8192
    assert vllm._at_get_max_len(applied[-1]) == 8192
    assert msgs[-1]["type"] == "done" and msgs[-1]["ok"] is False


def test_success_applies_recommended_and_keeps_it(ctx, monkeypatch):
    applied = []
    _wire_orchestration(monkeypatch, [
        {"outcome": "kv", "kv_tokens": 230528, "max_conc": None},
        {"outcome": "kv", "kv_tokens": 230528, "max_conc": 1.0},
    ], applied)
    vllm._at_run({"probe_len": 4096, "concurrency": 1.0, "kv_fraction": 1.0,
                  "report_only": False, "load_timeout_s": 600})
    msgs = _drain(vllm._at_job)
    md = [m for m in msgs if m["type"] == "model_done"][0]
    assert md["ok"] and md["applied"] and md["max_model_len"] == 230400
    assert md["original_max_len"] == 8192
    # writes: probe 4096, then recommended 230400 — and NO rollback after
    assert [vllm._at_get_max_len(a) for a in applied] == [4096, 230400]
    assert msgs[-1] == {"type": "done", "ok": True, "cancelled": False}


def test_report_only_restores_original(ctx, monkeypatch):
    applied = []
    _wire_orchestration(monkeypatch, [
        {"outcome": "kv", "kv_tokens": 230528, "max_conc": None},
    ], applied)
    vllm._at_run({"probe_len": 4096, "concurrency": 1.0, "kv_fraction": 1.0,
                  "report_only": True, "load_timeout_s": 600})
    msgs = _drain(vllm._at_job)
    md = [m for m in msgs if m["type"] == "model_done"][0]
    assert md["ok"] and md["applied"] is False and md["max_model_len"] == 230400
    assert [vllm._at_get_max_len(a) for a in applied] == [4096, 8192]
    assert msgs[-1]["ok"] is True
