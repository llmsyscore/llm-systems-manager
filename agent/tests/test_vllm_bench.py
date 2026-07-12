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
    binf = tmp_path / "venv-vllm"
    binf.write_text("#!/bin/sh\n")
    unit = f"[Service]\nExecStart={binf} serve org/m --host 0.0.0.0\n"
    monkeypatch.setattr(vllm.Path, "read_text", lambda self: unit)
    path, err = vllm._bench_resolve_bin()
    assert path == str(binf) and err is None


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
         {"flag": "--disable-tqdm", "value": ""}],
        "/tmp/x")
    assert cmd[:3] == ["/opt/v/bin/vllm", "bench", "serve"]
    assert ["--base-url", "http://localhost:8000"] == cmd[3:5]
    assert ["--model", "org/m"] == cmd[5:7]
    assert "--save-result" in cmd and "/tmp/x" in cmd
    assert ["--num-prompts", "200"] == cmd[-3:-1]
    assert cmd[-1] == "--disable-tqdm"


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


def test_extract_metrics_known_numeric_keys_only():
    m = vllm._bench_extract_metrics(SAMPLE)
    assert m["output_throughput"] == 1063.9
    assert m["p99_itl_ms"] == 30.3
    assert m["completed"] == 200
    assert "input_lens" not in m and "backend" not in m


def test_extract_extra_scalars_only():
    e = vllm._bench_extract_extra(SAMPLE)
    assert e["backend"] == "vllm" and e["num_prompts"] == 200
    assert "input_lens" not in e and "itls" not in e
