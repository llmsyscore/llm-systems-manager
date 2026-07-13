# agent/tests/test_vllm_bench.py
"""#357: vLLM bench wizard — binary resolution, command build, result parse,
run preflight/busy guard, fake end-to-end run, cancel."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._vllm_load import load_vllm

vllm = load_vllm()


@pytest.fixture
def ctx(tmp_path):
    cfg = SimpleNamespace(
        VLLM_ENABLED=True, VLLM_LORA_ENABLED=False,
        VLLM_SYSTEMD_UNIT="vllm.service",
        VLLM_API_URL="http://localhost:8000",
        VLLM_BENCH_BIN="",
    )
    context = SimpleNamespace(config=cfg, check_bearer=lambda *_: None,
                              check_stream_auth=lambda *a, **k: None)
    vllm.set_context(context)
    return context


# ── binary resolution ──────────────────────────────────────────────────

def test_resolve_bin_override_wins(ctx, tmp_path):
    binf = tmp_path / "vllm"
    binf.write_text("#!/bin/sh\n")
    ctx.config.VLLM_BENCH_BIN = str(binf)
    path, err = vllm._bench_resolve_bin()
    assert path == str(binf) and err is None


def test_resolve_bin_override_missing_is_error(ctx, tmp_path):
    ctx.config.VLLM_BENCH_BIN = str(tmp_path / "nope")
    path, err = vllm._bench_resolve_bin()
    assert path is None and "VLLM_BENCH_BIN" in err


def test_resolve_bin_execstart_head_fallback(ctx, tmp_path, monkeypatch):
    binf = tmp_path / "vllm"
    binf.write_text("#!/bin/sh\n")
    unit = f"[Service]\nExecStart={binf} serve org/m --host 0.0.0.0\n"
    monkeypatch.setattr(vllm.Path, "read_text", lambda self: unit)
    path, err = vllm._bench_resolve_bin()
    assert path == str(binf) and err is None


def test_resolve_bin_skips_interpreter_head(ctx, monkeypatch):
    unit = "[Service]\nExecStart=/usr/bin/python3 -m vllm.entrypoints.openai.api_server\n"
    monkeypatch.setattr(vllm.Path, "read_text", lambda self: unit)
    monkeypatch.setattr(vllm.shutil, "which", lambda _: "/usr/local/bin/vllm")
    assert vllm._bench_resolve_bin() == ("/usr/local/bin/vllm", None)


def test_resolve_bin_which_then_error(ctx, monkeypatch):
    monkeypatch.setattr(vllm.Path, "read_text",
                        lambda self: (_ for _ in ()).throw(OSError("no unit")))
    monkeypatch.setattr(vllm.shutil, "which", lambda _: "/usr/bin/vllm")
    assert vllm._bench_resolve_bin() == ("/usr/bin/vllm", None)
    monkeypatch.setattr(vllm.shutil, "which", lambda _: None)
    path, err = vllm._bench_resolve_bin()
    assert path is None and "vllm[bench]" in err


# ── command build ──────────────────────────────────────────────────────

def test_build_cmd_switches_and_save_result():
    cmd = vllm._bench_build_cmd(
        "/opt/v/bin/vllm", "http://localhost:8000", "org/m",
        [{"flag": "--num-prompts", "value": "200"},
         {"flag": "--ignore-eos", "value": ""}],
        "/tmp/x")
    assert cmd[:3] == ["/opt/v/bin/vllm", "bench", "serve"]
    assert ["--base-url", "http://localhost:8000"] == cmd[3:5]
    assert ["--model", "org/m"] == cmd[5:7]
    assert "--disable-tqdm" in cmd
    assert "--save-result" in cmd and "/tmp/x" in cmd
    assert ["--num-prompts", "200"] == cmd[-3:-1]
    assert cmd[-1] == "--ignore-eos"


# ── result parse ───────────────────────────────────────────────────────

SAMPLE = {
    "backend": "vllm", "model_id": "org/m", "num_prompts": 200,
    "request_throughput": 8.31, "output_throughput": 1063.9,
    "total_token_throughput": 9573.2, "duration": 24.05, "completed": 200,
    "total_input_tokens": 204800, "total_output_tokens": 25600,
    "mean_ttft_ms": 123.4, "median_ttft_ms": 101.0, "p99_ttft_ms": 456.7,
    "mean_tpot_ms": 11.1, "median_tpot_ms": 10.5, "p99_tpot_ms": 22.2,
    "mean_itl_ms": 10.9, "median_itl_ms": 10.2, "p99_itl_ms": 30.3,
    "input_lens": [1024] * 200, "itls": [[1, 2]] * 200,
}


def test_extract_extra_scalars_only():
    e = vllm._bench_extract_extra(SAMPLE)
    assert e["backend"] == "vllm" and e["num_prompts"] == 200
    assert e["output_throughput"] == 1063.9 and e["p99_itl_ms"] == 30.3
    assert "input_lens" not in e and "itls" not in e


# ── run endpoint: preflight, validation, busy guard ────────────────────

from fastapi import HTTPException  # noqa: E402


class _FakeResp:
    def __init__(self, ok=True, data=None):
        self.ok = ok
        self._data = data or {"data": [{"id": "org/m"}]}

    def json(self):
        return self._data


def _server_up(monkeypatch, up=True):
    sess = SimpleNamespace(
        get=(lambda *a, **k: _FakeResp()) if up
        else (lambda *a, **k: (_ for _ in ()).throw(OSError("down"))))
    monkeypatch.setattr(vllm, "_get_session", lambda: sess)


def test_run_refuses_when_server_down(ctx, monkeypatch):
    _server_up(monkeypatch, up=False)
    r = vllm.vllm_bench_run({"switches": []})
    assert r["ok"] is False and "not running" in r["error"]


def test_run_rejects_bad_switches(ctx, monkeypatch):
    _server_up(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        vllm.vllm_bench_run({"switches": "nope"})
    assert ei.value.status_code == 400


def test_run_reports_missing_binary(ctx, monkeypatch):
    _server_up(monkeypatch)
    monkeypatch.setattr(vllm, "_bench_resolve_bin", lambda: (None, "no vllm"))
    r = vllm.vllm_bench_run({"switches": []})
    assert r["ok"] is False and r["error"] == "no vllm"


def test_run_busy_guard(ctx, monkeypatch):
    import threading
    _server_up(monkeypatch)
    monkeypatch.setattr(vllm, "_bench_resolve_bin", lambda: ("/bin/true", None))
    release = threading.Event()
    monkeypatch.setattr(vllm, "_bench_run_one", lambda *a: release.wait())
    try:
        assert vllm.vllm_bench_run({"switches": []}) == {"ok": True}
        r = vllm.vllm_bench_run({"switches": []})
        assert r["ok"] is False and "in progress" in r["error"]
    finally:
        release.set()
        vllm._bench_job.join(timeout=5)


# ── fake end-to-end run ────────────────────────────────────────────────

def _drain_replay():
    return [r["event"] for r in vllm._bench_replay.records_after_seq(0)]


def test_bench_run_one_end_to_end(ctx, monkeypatch, tmp_path):
    import json as _json
    import subprocess as _sp
    import sys as _sys

    result_json = _json.dumps(SAMPLE)
    script = (
        "import json, pathlib, sys\n"
        "print('starting bench', flush=True)\n"
        "d = sys.argv[sys.argv.index('--result-dir') + 1]\n"
        "pathlib.Path(d, 'result.json').write_text(" + repr(result_json) + ")\n"
        "print('done bench', flush=True)\n"
    )
    real_popen = _sp.Popen

    def popen(argv, **kw):
        assert argv[1:3] == ["bench", "serve"]
        return real_popen([_sys.executable, "-c", script] + argv[3:], **kw)

    monkeypatch.setattr(vllm.subprocess, "Popen", popen)
    vllm._bench_run_one("/opt/v/bin/vllm", "org/m",
                        [{"flag": "--num-prompts", "value": "5"}])
    events = _drain_replay()
    types = [e["type"] for e in events]
    assert types[0] == "model_start" and "cmd" in events[0]
    assert "line" in types
    res = [e for e in events if e["type"] == "result"][0]
    assert res["extra"]["output_throughput"] == 1063.9
    assert res["extra"]["backend"] == "vllm"
    md = [e for e in events if e["type"] == "model_done"][0]
    assert md["ok"] is True and md["rc"] == 0
    assert events[-1] == {"type": "done", "ok": True, "cancelled": False}


def test_bench_cancel_returns_ok(ctx):
    r = vllm.vllm_bench_cancel()
    assert r["ok"] is True
    vllm._bench_job.cancel_event.clear()
