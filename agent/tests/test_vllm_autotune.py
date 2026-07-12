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
