"""#356/#894: vLLM journal parsing and the autotune journal watcher, now in providers/vllm_tools.py
on llama.py's shared autotune state."""
from __future__ import annotations

import subprocess
import sys

import pytest

from tests._vllm_tools_load import load


@pytest.fixture(scope="module")
def mods():
    llama, vllm, tools, restore = load()
    yield llama, vllm, tools
    restore()


# ── journal line parsing ────────────────────────────────────────────────

def test_kv_size_line_parses_with_commas(mods):
    tools = mods[2]
    m = tools._AT_KV_SIZE_RE.search("INFO ... GPU KV cache size: 230,528 tokens")
    assert m and tools._at_num(m.group(1)) == 230528


def test_kv_size_line_parses_on_a_cpu_host(mods):
    tools = mods[2]
    m = tools._AT_KV_SIZE_RE.search("(EngineCore pid=900) INFO [kv_cache_utils.py:2395] CPU KV cache size: 9,344 tokens, Maximum concurrency for 4,096 tokens per request: 2.28x")
    assert m and tools._at_num(m.group(1)) == 9344


def test_max_concurrency_line_parses(mods):
    tools = mods[2]
    m = tools._AT_MAX_CONC_RE.search("Maximum concurrency for 100,000 tokens per request: 2.31x")
    assert m and tools._at_num(m.group(1)) == 100000 and float(m.group(2)) == 2.31


def test_estimated_max_len_error_parses(mods):
    tools = mods[2]
    line = ("ValueError: To serve at least one request with the model's max seq len (221000), 10.12 GiB KV cache "
            "is needed, which is larger than the available KV cache memory (5.59 GiB). Based on the available "
            "memory, the estimated maximum model length is 56736.")
    m = tools._AT_EST_MAX_RE.search(line)
    assert m and tools._at_num(m.group(1)) == 56736


def test_old_kv_capacity_error_parses(mods):
    tools = mods[2]
    m = tools._AT_KV_CAP_OLD_RE.search("ValueError: The model's max seq len (131072) is larger than the maximum number of tokens that can be stored in KV cache (56736).")
    assert m and tools._at_num(m.group(1)) == 56736


def test_fatal_matches_engine_failures_but_not_info_lines(mods):
    tools = mods[2]
    assert tools._AT_FATAL_RE.search("EngineCore failed to start.")
    assert tools._AT_FATAL_RE.search("torch.OutOfMemoryError: CUDA out of memory")
    assert not tools._AT_FATAL_RE.search("INFO: Started server process [123]")


# ── journal watcher (fake journalctl via a print-then-idle python child) ─

def _fake_journal(monkeypatch, tools, lines, hold_open=True):
    script = "import sys,time\n" + "".join(f"print({l!r}, flush=True)\n" for l in lines) + ("time.sleep(60)\n" if hold_open else "")
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        assert argv[0] == "journalctl"
        return real_popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, start_new_session=True)
    monkeypatch.setattr(tools.subprocess, "Popen", popen)


@pytest.fixture
def watch_env(mods, monkeypatch):
    llama, vllm, tools = mods
    llama._autotune_cancel_event.clear()
    monkeypatch.setattr(llama, "_autotune_aux_proc", None); monkeypatch.setattr(llama, "_autotune_aux_pgid", None)
    return llama, tools


def test_watch_returns_kv_and_concurrency(watch_env, monkeypatch):
    _llama, tools = watch_env
    _fake_journal(monkeypatch, tools, ["INFO loading model...", "GPU KV cache size: 230,528 tokens",
                                       "Maximum concurrency for 8,192 tokens per request: 28.14x"])
    r = tools._at_watch_journal("vllm.service", timeout_s=15, step="load")
    assert r["outcome"] == "kv" and r["kv_tokens"] == 230528 and r["max_conc"] == 28.14


def test_watch_estimated_max_beats_fatal_on_same_line(watch_env, monkeypatch):
    _llama, tools = watch_env
    _fake_journal(monkeypatch, tools, ["ValueError: ... the estimated maximum model length is 56736."])
    r = tools._at_watch_journal("vllm.service", timeout_s=15, step="load")
    assert r["outcome"] == "est_max" and r["est_max_len"] == 56736


def test_watch_fatal_line(watch_env, monkeypatch):
    _llama, tools = watch_env
    _fake_journal(monkeypatch, tools, ["EngineCore failed to start."])
    r = tools._at_watch_journal("vllm.service", timeout_s=15, step="load")
    assert r["outcome"] == "fatal" and "EngineCore" in r["fatal_line"]


def test_watch_fatal_line_carries_the_derived_max(watch_env, monkeypatch):
    _llama, tools = watch_env
    _fake_journal(monkeypatch, tools, ["ValueError: User-specified max_model_len (230400) is greater than the derived max_model_len (40960)"])
    r = tools._at_watch_journal("vllm.service", timeout_s=15, step="load")
    assert r["outcome"] == "fatal" and r["derived_max"] == 40960


def test_watch_timeout(watch_env, monkeypatch):
    _llama, tools = watch_env
    _fake_journal(monkeypatch, tools, ["INFO still loading..."])
    assert tools._at_watch_journal("vllm.service", timeout_s=2, step="load")["outcome"] == "timeout"


def test_watch_kv_returns_quickly_on_quiet_journal(watch_env, monkeypatch):
    import time as _time
    _llama, tools = watch_env
    _fake_journal(monkeypatch, tools, ["GPU KV cache size: 230,528 tokens"])
    monkeypatch.setattr(tools, "_AT_CONC_GRACE_S", 0.5)
    t0 = _time.monotonic()
    r = tools._at_watch_journal("vllm.service", timeout_s=30, step="load")
    assert r["outcome"] == "kv" and r["kv_tokens"] == 230528 and _time.monotonic() - t0 < 10


def test_watch_cancel_untracks_the_watcher(watch_env, monkeypatch):
    llama, tools = watch_env
    _fake_journal(monkeypatch, tools, ["INFO still loading..."])
    llama._autotune_cancel_event.set()
    try:
        r = tools._at_watch_journal("vllm.service", timeout_s=10, step="load")
    finally:
        llama._autotune_cancel_event.clear()
    assert r["outcome"] == "cancelled"
    assert llama._autotune_aux_proc is None


def test_watch_fatal_keeps_the_traceback_message_line(watch_env, monkeypatch):
    _llama, tools = watch_env
    monkeypatch.setattr(tools, "_AT_CONC_GRACE_S", 0.5)
    _fake_journal(monkeypatch, tools, [
        "(Worker pid=9) ERROR [multiproc_executor.py:1055] Traceback (most recent call last):",
        "(Worker pid=9) ERROR [multiproc_executor.py:1055]     raise ValueError(",
        "(Worker pid=9) ERROR [multiproc_executor.py:1055] ValueError: Available memory on node 0 (0.3/1.81 GiB) is less than requested memory for kv (1.0 GiB).",
        "(EngineCore pid=8) ERROR [core.py:1366] Traceback (most recent call last):",
    ])
    r = tools._at_watch_journal("vllm.service", timeout_s=15, step="load")
    assert r["outcome"] == "fatal" and r["fatal_line"].endswith("requested memory for kv (1.0 GiB).")


def test_watch_fatal_without_a_message_line_keeps_the_first_match(watch_env, monkeypatch):
    _llama, tools = watch_env
    monkeypatch.setattr(tools, "_AT_CONC_GRACE_S", 0.5)
    _fake_journal(monkeypatch, tools, ["EngineCore failed to start.", "INFO shutting down"])
    r = tools._at_watch_journal("vllm.service", timeout_s=15, step="load")
    assert r["outcome"] == "fatal" and r["fatal_line"] == "EngineCore failed to start."
