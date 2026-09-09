"""#880: autotune routes — preflight, v1/v2 body handling, busy refusal, cancel kills the aux group."""
from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code, self.detail = status_code, detail


def _load_llama():
    _stub("requests")
    _stub("fastapi", Header=lambda **k: None, HTTPException=_HTTPException,
          Query=lambda *a, **k: None, Request=object)
    _stub("fastapi.responses", Response=object, StreamingResponse=object)
    _stub("starlette.concurrency", run_in_threadpool=None)
    _stub("starlette")
    _stub("stream_pool")

    @contextlib.contextmanager
    def _be(*a, **k):
        yield
    _stub("_best_effort", best_effort=_be)
    from _bench_replay import BenchReplayBuffer  # real, stdlib-only
    _stub("_bench_replay", BenchReplayBuffer=BenchReplayBuffer)
    _stub("collectors")
    _stub("collectors.gpu", collect_gpu=lambda *a, **k: {"vram_total_bytes": 32 * 2**30})
    pkg = types.ModuleType("providers")
    pkg.__path__ = [str(_AGENT_ROOT / "providers")]
    sys.modules["providers"] = pkg
    for sub in ("llama_install", "llama_sse", "llama_upgrade"):
        sys.modules[f"providers.{sub}"] = types.ModuleType(f"providers.{sub}")
    spec = importlib.util.spec_from_file_location("providers.llama", _AGENT_ROOT / "providers" / "llama.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["providers.llama"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def llama():
    sys.path.insert(0, str(_AGENT_ROOT))
    return _load_llama()


class _Ctx:
    def __init__(self, tmp, llama_bin=""):
        self.config = types.SimpleNamespace(LLAMA_API_URL="http://127.0.0.1:9931", AGENT_INSTALL_DIR=str(tmp),
                                            SPEED_BENCH_PYTHON="", MANAGER_URL="", LLAMA_BIN=llama_bin,
                                            LLAMA_ENABLED=True, LLAMA_SYSTEMD_UNIT="llama-server",
                                            LLAMA_CONFIG_INI=str(tmp / "config.ini"), AGENT_USER="")
        self.state = {"token": "", "agent_id": ""}
        self.post_session = None

    def check_bearer(self, *a, **k):
        return None


def _wire(llama, tmp_path, monkeypatch, **ctx_kw):
    ctx = _Ctx(tmp_path, **ctx_kw)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_llama_unit_active", lambda: False)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_autotune_active", False)
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path / "hub")
    monkeypatch.setattr(llama, "_llama_catalog_sweep", lambda: ({"org/m:Q4": 18_000_000_000}, {}))
    return ctx


def test_preflight_shape(llama, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "llama-server").write_text("")
    _wire(llama, tmp_path, monkeypatch, llama_bin=str(bin_dir / "llama-server"))
    out = llama.llama_autotune_preflight()
    assert out["ok"] and out["busy"] is False and out["unit_active"] is False
    assert set(out["cores"]) == {"physical", "logical"} and out["cores"]["logical"] >= 1
    assert out["perplexity"] is False                      # no llama-perplexity beside the server binary
    assert out["runtime"]["ok"] is False and out["drafts"] == []
    assert out["sizes"] == {"org/m:Q4": 18_000_000_000} and out["vram_total_mb"] == 32768
    assert isinstance(out["ram_total_mb"], int)
    (bin_dir / "llama-perplexity").write_text("")
    monkeypatch.setattr(llama, "_autotune_kl_text", lambda: tmp_path / "kl.txt")
    (tmp_path / "kl.txt").write_text("x")
    assert llama.llama_autotune_preflight()["perplexity"] is True


def test_run_accepts_v1_and_v2_bodies(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(llama.threading, "Thread",
                        lambda target, args=(), daemon=None: types.SimpleNamespace(start=lambda: started.append(args)))
    out = llama.llama_autotune_run({"model_ids": ["org/m:Q4"], "target_mb": 900, "optional_params": {"ctk": "q8_0"}})
    assert out["ok"] is True and out["run_id"]
    req = started[-1][0]
    assert req["objective"] == "fit" and req["dims"]["context"]["custom_args"] == ["-ctk", "q8_0"]
    llama._autotune_active = False
    out = llama.llama_autotune_run({"model_ids": ["org/m:Q4"], "objective": "serve", "budget_min": 30,
                                    "dims": {"slots": {"candidates": [1, 2, 4]}}})
    assert out["ok"] is True
    assert started[-1][0]["dims"]["slots"]["candidates"] == [1, 2, 4]


def test_run_rejects_bad_body_busy_and_active_unit(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    with pytest.raises(llama.HTTPException) as ei:
        llama.llama_autotune_run({"model_ids": ["m"], "objective": "nope"})
    assert ei.value.status_code == 400
    monkeypatch.setattr(llama, "_bench_active", True)
    out = llama.llama_autotune_run({"model_ids": ["m"], "objective": "fit"})
    assert out["ok"] is False and "in progress" in out["error"]
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_llama_unit_active", lambda: True)
    out = llama.llama_autotune_run({"model_ids": ["m"], "objective": "fit"})
    assert out["ok"] is False and "running" in out["error"]


class _Proc:
    def __init__(self, pid=4242, fail_waits=0):
        self.pid, self.killed, self.waited = pid, [], 0
        self.fail_waits = fail_waits

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited += 1
        if self.waited <= self.fail_waits:
            raise subprocess.TimeoutExpired("llama-server", timeout or 0)
        return 0


def _cancel_with(llama, monkeypatch, killed, **procs):
    monkeypatch.setattr(llama.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(llama, "_autotune_proc", procs["server"])
    monkeypatch.setattr(llama, "_autotune_pgid", 100)
    monkeypatch.setattr(llama, "_autotune_aux_proc", procs["aux"])
    monkeypatch.setattr(llama, "_autotune_aux_pgid", 200)
    return llama.llama_autotune_cancel()


def test_cancel_kills_server_and_aux_groups(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    killed = []
    out = _cancel_with(llama, monkeypatch, killed, server=_Proc(1), aux=_Proc(2))
    assert out["ok"] is True
    assert (200, llama.signal.SIGTERM) in killed and (100, llama.signal.SIGTERM) in killed
    # The stick/perplexity child must go first — it holds the server busy.
    assert killed.index((200, llama.signal.SIGTERM)) < killed.index((100, llama.signal.SIGTERM))
    assert llama.signal.SIGKILL not in [sig for _, sig in killed]
    assert llama._autotune_cancel_event.is_set()


def test_cancel_escalates_to_sigkill_on_both_groups(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    killed = []
    out = _cancel_with(llama, monkeypatch, killed,
                       server=_Proc(1, fail_waits=1), aux=_Proc(2, fail_waits=1))
    assert out["ok"] is True
    assert (200, llama.signal.SIGKILL) in killed and (100, llama.signal.SIGKILL) in killed


def test_backend_load_normalises_run_iter_result(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    seen = {}

    def fake_iter(model_id, fitt, extra, env, idx, *, ctx=None, hold=None):
        seen.update({"ctx": ctx, "extra": list(extra)})
        return {"ok": False, "sentinel": True, "model_loaded": True, "oom": False, "ctx_seq": ctx,
                "actual_free_mb": None, "total_vram_mb": None, "load_s": 12.5,
                "facts_lines": ["print_info: n_layer = 48", "print_info: n_expert = 128"],
                "hold": hold() if hold else None}
    monkeypatch.setattr(llama, "_autotune_run_iter", fake_iter)
    be = llama._AutotuneBackend("org/m:Q4", {}, "r1")
    monkeypatch.setattr(be, "_stick", lambda measure: {"ok": True, "decode_tps": 99.0, "prefill_tps": 1.0, "latency_s": 1.0,
                                                      "agg_tps": 99.0, "accept": None, "completion_tokens": 100,
                                                      "seconds": 3.0, "error": None})
    res = be.load(["--threads", "8"], 65536, {"concurrency": 1, "limit": 4})
    assert res["ok"] is True and res["ctx"] == 65536 and res["free_mb"] is None
    assert res["facts"]["n_expert"] == 128 and res["load_s"] == 12.5 and res["stick"]["decode_tps"] == 99.0
    assert seen == {"ctx": 65536, "extra": ["--threads", "8"]}
    res = be.load(["--threads", "8"], None, None)
    assert res["stick"] is None and seen["ctx"] is None


def test_autotune_port_from_api_url(llama, tmp_path, monkeypatch):
    ctx = _wire(llama, tmp_path, monkeypatch)
    assert llama._autotune_port() == 9931
    ctx.config.LLAMA_API_URL = "http://localhost"
    assert llama._autotune_port() == 8080


# Prints the load banner, floods stdout past the pipe buffer, then waits for SIGTERM
# before emitting the shutdown memory breakdown.
_FAKE_SERVER = """#!/usr/bin/env python3
import signal, sys, time
_stop = []
signal.signal(signal.SIGTERM, lambda *a: _stop.append(1))
print("llama_context: n_ctx_seq (4096)", flush=True)
print("main: model loaded", flush=True)
pad = "y" * 900
for i in range(320):
    print("srv  log_server_r: request %d %s" % (i, pad), flush=True)
open(sys.argv[0] + ".done", "w").write("done")
while not _stop:
    time.sleep(0.05)
print("common_memory_breakdown_print: | - CUDA0 (RTX) | 32768 = 1024 + (20000 = 18000 + 1000 + 1000) + 11744 |", flush=True)
sys.stdout.flush()
"""


def _fake_server(tmp_path, name):
    script = tmp_path / name
    script.write_text(_FAKE_SERVER)
    script.chmod(0o755)
    return script, Path(str(script) + ".done")


def test_run_iter_drains_stdout_while_hold_runs(llama, tmp_path, monkeypatch):
    """A held server that floods stdout must not deadlock on a full pipe (#880)."""
    script, marker = _fake_server(tmp_path, "fake-server-hold")
    _wire(llama, tmp_path, monkeypatch, llama_bin=str(script))
    llama._autotune_cancel_event.clear()
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: "o/r:Q4")
    monkeypatch.setattr(llama, "_autotune_port", lambda: 8080)

    def hold():
        # Only completes once the child got past the flood — i.e. someone drained stdout.
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return {"ok": True, "decode_tps": 1.0}

    out = {}

    def _call():
        out["res"] = llama._autotune_run_iter("org/m:Q4", 0, [], os.environ.copy(), 0, ctx=4096, hold=hold)
    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "_autotune_run_iter deadlocked on a full stdout pipe"
    res = out["res"]
    assert res["model_loaded"] is True and res["hold"] == {"ok": True, "decode_tps": 1.0}
    assert res["actual_free_mb"] == 1024 and res["total_vram_mb"] == 32768
    assert res["ok"] is True and res["ctx_seq"] == 4096


def test_run_iter_without_hold_still_drains(llama, tmp_path, monkeypatch):
    script, _marker = _fake_server(tmp_path, "fake-server-nohold")
    _wire(llama, tmp_path, monkeypatch, llama_bin=str(script))
    llama._autotune_cancel_event.clear()
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: "o/r:Q4")
    monkeypatch.setattr(llama, "_autotune_port", lambda: 8080)
    out = {}
    t = threading.Thread(target=lambda: out.update(
        res=llama._autotune_run_iter("org/m:Q4", 0, [], os.environ.copy(), 0, ctx=4096)), daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive(), "_autotune_run_iter hung without a hold"
    assert out["res"]["ok"] is True and out["res"]["hold"] is None
    assert out["res"]["actual_free_mb"] == 1024


def test_stick_discards_a_stale_output_file(llama, tmp_path, monkeypatch):
    """A failed speed-bench writes nothing; a leftover file must not be read as its result."""
    _wire(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_bench_live_runtime",
                        lambda: {"python": "py", "script": "sc", "source": "x",
                                 "script_status": "ok", "commit": "c"})
    monkeypatch.setattr(llama, "_autotune_port", lambda: 8080)
    calls = []
    monkeypatch.setattr(llama._bl, "run_level_subprocess",
                        lambda *a, **k: (calls.append(a[0]), (1, False, 1.0))[1])
    be = llama._AutotuneBackend("org/m:Q4", {}, "r1")
    monkeypatch.setattr(be, "_server_ready", lambda url: "org/m:Q4")
    out_dir = llama._bl.bench_dir(str(tmp_path)) / "runs" / "at-r1"
    out_dir.mkdir(parents=True, exist_ok=True)
    stale = out_dir / "stick-1-org_m_Q4.json"
    stale.write_text('{"summary": [{"category": "overall", "avg_pred_t_s": 999.0}], "results": []}')
    res = be._stick({"concurrency": 1, "limit": 4})
    assert res["ok"] is False and res.get("decode_tps") is None
    assert not stale.exists(), "stale output survived into the run"
    assert calls, "run_level_subprocess was never invoked"


def test_stick_runs_single_turn_1k_prompts(llama, tmp_path, monkeypatch):
    """The stick must not fall back to the multi-turn qualitative set — it costs minutes per measure."""
    _wire(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_bench_live_runtime",
                        lambda: {"python": "py", "script": "sc", "source": "x",
                                 "script_status": "ok", "commit": "c"})
    monkeypatch.setattr(llama, "_autotune_port", lambda: 8080)
    seen = {}
    real_build = llama._bl.build_cmd

    def _spy(python, script, url, req, level, out):
        seen.update(req)
        return real_build(python, script, url, req, level, out)
    monkeypatch.setattr(llama._bl, "build_cmd", _spy)
    monkeypatch.setattr(llama._bl, "run_level_subprocess", lambda *a, **k: (1, False, 1.0))
    be = llama._AutotuneBackend("org/m:Q4", {}, "r1")
    monkeypatch.setattr(be, "_server_ready", lambda url: "org/m:Q4")
    be._stick({"concurrency": 2, "limit": 9})
    assert seen["bench"] == "throughput_1k" == llama._at.STICK_BENCH
    assert seen["categories"] == "all" and seen["osl"] == llama._at.STICK_OSL
    assert seen["limit"] == 9 and seen["concurrency"] == [2]
    assert seen["extra_inputs"] == {"temperature": 0}


def test_stick_paths_do_not_collide_across_models(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_bench_live_runtime",
                        lambda: {"python": "py", "script": "sc", "source": "x",
                                 "script_status": "ok", "commit": "c"})
    monkeypatch.setattr(llama, "_autotune_port", lambda: 8080)
    written = []

    def _fake_run(cmd, *a, **k):
        written.append(cmd[cmd.index("--output") + 1])
        return (1, False, 1.0)
    monkeypatch.setattr(llama._bl, "run_level_subprocess", _fake_run)
    for mid in ("org/a:Q4", "org/b:Q8"):
        be = llama._AutotuneBackend(mid, {}, "r1")
        monkeypatch.setattr(be, "_server_ready", lambda url, m=mid: m)
        be._stick({"concurrency": 1, "limit": 4})
    assert len(set(written)) == 2, written
    assert all(Path(w).name.startswith("stick-1-") for w in written)


def test_backend_load_flags_an_oom_after_the_model_loaded(llama, tmp_path, monkeypatch):
    """A load that OOMs after 'main: model loaded' must not report ok with no error."""
    _wire(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_autotune_run_iter",
                        lambda *a, **k: {"ok": False, "model_loaded": True, "oom": True, "error": None,
                                         "ctx_seq": 65536, "actual_free_mb": 12, "total_vram_mb": 32768,
                                         "facts_lines": [], "load_s": 9.0, "hold": None})
    res = llama._AutotuneBackend("org/m:Q4", {}, "r1").load(["--threads", "8"], 65536, None)
    assert res["ok"] is False and res["oom"] is True and res["error"] == "OOM after load"


@pytest.mark.parametrize("line, hit", [
    ("srv  params_from_: allocating slot 3 for that request failed", False),
    ("ggml_backend_cuda_buffer_type_alloc_buffer: allocating 512 MB failed", True),
    ("cudaMalloc failed", True),
    ("ggml_gallocr_reserve_n: failed to allocate CUDA0 buffer", True),
    ("CUDA error: out of memory", True),
])
def test_oom_regex_needs_an_allocation_not_any_failure(llama, line, hit):
    assert bool(llama._AT_OOM_RE.search(line)) is hit


def _kl_backend(llama, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "llama-perplexity").write_text("")
    _wire(llama, tmp_path, monkeypatch, llama_bin=str(bin_dir / "llama-server"))
    (tmp_path / "kl.txt").write_text("x")
    monkeypatch.setattr(llama, "_autotune_kl_text", lambda: tmp_path / "kl.txt")
    monkeypatch.setattr(llama, "_autotune_put", lambda msg: None)
    return llama._AutotuneBackend("org/m:Q4", {}, "r1")


def test_kl_refuses_a_model_with_no_hf_reference(llama, tmp_path, monkeypatch):
    be = _kl_backend(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: None)
    assert be.kl(["--cache-type-k", "q8_0"], False) == {"ok": False, "kl": None,
                                                        "error": "no HF reference for model"}


def test_kl_tracks_then_untracks_the_perplexity_process(llama, tmp_path, monkeypatch):
    be = _kl_backend(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: "o/r:Q4")

    class _KlProc:
        returncode = 0
        pid = 4242

        def communicate(self, timeout=None):
            return ("llama_perplexity: Mean KLD: 0.01\n", "")

    monkeypatch.setattr(llama.subprocess, "Popen", lambda *a, **k: _KlProc())
    order = []
    monkeypatch.setattr(llama, "_autotune_track_aux", lambda p: order.append("track"))
    monkeypatch.setattr(llama, "_autotune_untrack_aux", lambda: order.append("untrack"))
    assert be.kl(["--cache-type-k", "q8_0"], False) == {"ok": True, "kl": 0.01, "error": None}
    assert order == ["track", "untrack"]


def test_run_validates_against_the_hf_cache_root(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    seen = {}
    real = llama._at.validate_request

    def _spy(body, **kw):
        seen.update(kw)
        return real(body, **kw)
    monkeypatch.setattr(llama._at, "validate_request", _spy)
    monkeypatch.setattr(llama.threading, "Thread",
                        lambda target, args=(), daemon=None: types.SimpleNamespace(start=lambda: None))
    assert llama.llama_autotune_run({"model_ids": ["org/m:Q4"], "objective": "fit"})["ok"] is True
    assert seen["cache_root"] == llama._hf_cache_root() == tmp_path / "hub"
    assert seen["v1_args"] is llama._autotune_build_optional_args


_HELP_FIXTURE = """usage: llama-server [options]

----- common params -----

  -h,    --help                            print usage and exit
  -c,    --ctx-size N                      size of the prompt context
  -ngl,  --gpu-layers, --n-gpu-layers N    number of layers to store in VRAM
  -fa,   --flash-attn [on|off|auto]        set Flash Attention use
         --reasoning, --think on|off       enable reasoning
         --reasoning-preserve on|off       preserve reasoning content
         --reasoning-budget-message STRING
                                           message appended when the budget runs out
         --check-tensors                   check model tensor data for invalid values
         --kv-unified                      use single unified KV buffer
         --log-disable                     Log disable
         --no-webui                        Disable the Web UI
"""


def _fake_llama_bin(tmp_path, rc=0, text=_HELP_FIXTURE):
    p = tmp_path / "bin" / "llama-server"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/usr/bin/env python3\nimport sys\n"
                 f"sys.stdout.write({text!r})\nsys.exit({rc})\n")
    p.chmod(0o755)
    return p


def test_help_valued_parses_and_caches(llama, tmp_path, monkeypatch):
    binp = _fake_llama_bin(tmp_path)
    _wire(llama, tmp_path, monkeypatch, llama_bin=str(binp))
    first = llama._llama_help_valued()
    assert {"ctx-size", "flash-attn", "reasoning", "think", "reasoning-budget-message"} <= first
    assert not ({"help", "check-tensors", "kv-unified", "log-disable", "no-webui"} & first)

    def _boom(*a, **k):
        raise AssertionError("llama-server --help re-run instead of using the cache")
    monkeypatch.setattr(llama.subprocess, "run", _boom)
    assert llama._llama_help_valued() == first


def test_help_valued_is_none_without_a_binary(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch, llama_bin="")
    assert llama._llama_help_valued() is None


def test_help_valued_rejects_thin_output_and_never_caches_it(llama, tmp_path, monkeypatch):
    """A parse that finds almost nothing is a broken binary, not a flagless llama-server."""
    binp = _fake_llama_bin(tmp_path, text="usage: llama-server [options]\n  -c, --ctx-size N  ctx\n")
    _wire(llama, tmp_path, monkeypatch, llama_bin=str(binp))
    assert llama._llama_help_valued() is None
    runs = []
    real = llama.subprocess.run
    monkeypatch.setattr(llama.subprocess, "run", lambda *a, **k: (runs.append(1), real(*a, **k))[1])
    assert llama._llama_help_valued() is None
    assert runs == [1]                                     # failures are re-tried, not cached


def test_preflight_reports_help_valued(llama, tmp_path, monkeypatch):
    binp = _fake_llama_bin(tmp_path)
    _wire(llama, tmp_path, monkeypatch, llama_bin=str(binp))
    hv = llama.llama_autotune_preflight()["help_valued"]
    assert hv["ok"] is True and hv["count"] >= 5
    _wire(llama, tmp_path, monkeypatch, llama_bin="")
    assert llama.llama_autotune_preflight()["help_valued"] == {"ok": False, "count": 0}


def test_run_all_passes_help_valued_into_the_engine_env(llama, tmp_path, monkeypatch):
    binp = _fake_llama_bin(tmp_path)
    _wire(llama, tmp_path, monkeypatch, llama_bin=str(binp))
    monkeypatch.setattr(llama, "_autotune_set_perf_mode", lambda mode: None)
    monkeypatch.setattr(llama, "_bench_live_runtime",
                        lambda: {"python": "", "script": "", "source": "", "script_status": "ok", "commit": ""})
    monkeypatch.setattr(llama, "_list_cache_ggufs", lambda root: [])
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: llama.configparser.ConfigParser())
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: "o/r:Q4")
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: None)
    monkeypatch.setattr(llama, "_autotune_put", lambda ev: None)
    seen = []

    def _fake_run_model(mid, section, req, backend, put, cancelled, env, **kw):
        seen.append(env.get("valued"))
        return {"ok": True, "model_id": mid}
    monkeypatch.setattr(llama._at, "run_model", _fake_run_model)
    llama._autotune_cancel_event.clear()
    llama._autotune_run_all({"model_ids": ["org/m:Q4"], "objective": "fit", "budget_min": 45,
                            "dims": dict(llama._at.DEFAULT_DIMS)})
    assert len(seen) == 1 and "flash-attn" in seen[0] and "check-tensors" not in seen[0]


def test_run_all_warns_when_help_is_unreadable(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch, llama_bin="")
    monkeypatch.setattr(llama, "_autotune_set_perf_mode", lambda mode: None)
    monkeypatch.setattr(llama, "_bench_live_runtime",
                        lambda: {"python": "", "script": "", "source": "", "script_status": "ok", "commit": ""})
    monkeypatch.setattr(llama, "_list_cache_ggufs", lambda root: [])
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: llama.configparser.ConfigParser())
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: "o/r:Q4")
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(llama, "_autotune_put", events.append)
    seen = []

    def _fake_run_model(mid, section, req, backend, put, cancelled, env, **kw):
        seen.append((len(events), env.get("valued")))
        return {"ok": True, "model_id": mid}
    monkeypatch.setattr(llama._at, "run_model", _fake_run_model)
    llama._autotune_cancel_event.clear()
    llama._autotune_run_all({"model_ids": ["org/m:Q4"], "objective": "fit", "budget_min": 45,
                            "dims": dict(llama._at.DEFAULT_DIMS)})
    warn = [e for e in events if e.get("type") == "line" and "--help" in e.get("text", "")]
    assert warn == [{"type": "line",
                     "text": "[autotune] could not read llama-server --help; on/off flags are guessed"}]
    assert seen[0][1] is None and seen[0][0] > 0            # warned before the first model


def test_quality_mode_dispatches_run_quality_and_posts_quality_ledger(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_autotune_set_perf_mode", lambda mode: None)
    monkeypatch.setattr(llama, "_bench_live_runtime",
                        lambda: {"python": "", "script": "", "source": "", "script_status": "ok", "commit": ""})
    monkeypatch.setattr(llama, "_list_cache_ggufs", lambda root: [])
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: llama.configparser.ConfigParser())
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: "org/m:Q4")
    monkeypatch.setattr(llama, "_llama_help_valued", lambda: set())
    llama._llama_build_last = "b10850-abc"
    seen = {}

    def fake_quality(mid, section, req, backend, put, cancelled, env):
        seen.update(mid=mid, section=section, req=req, env=env)
        doc = {"type": "model_done", "model_id": mid, "ok": True, "mode": "quality",
               "guard": {"kl": 0.01, "pass": True}, "llama_build": env.get("llama_build"),
               "after": {}, "before": {}, "stages": []}
        put(doc)
        return doc
    monkeypatch.setattr(llama._at, "run_quality", fake_quality)
    monkeypatch.setattr(llama._at, "run_model", lambda *a, **k: pytest.fail("run_model must not run in quality mode"))
    posted = []
    monkeypatch.setattr(llama._shared, "post_tool_run",
                        lambda ctx, tool, prov, rid, mid, ok, summary: posted.append((tool, summary)))
    llama._autotune_cancel_event.clear()
    llama._autotune_run_all({"model_ids": ["org/m:Q4"], "objective": "fit", "mode": "quality",
                             "overrides": {"cache-type-k": "q4_0"}})
    assert seen["req"]["mode"] == "quality" and seen["env"]["llama_build"] == "b10850-abc"
    assert posted == [("quality", {"objective": None, "mode": "quality", "llama_build": "b10850-abc",
                                   "ctx_size": None, "free_mb": None, "decode_tps": None, "gain_pct": None,
                                   "stages_done": 0, "verify_ok": None, "wh_per_ktok": None, "n_expert": None,
                                   "kl": 0.01, "kl_pass": True, "regressed": None})]


def test_preflight_reports_llama_build(llama, tmp_path, monkeypatch):
    _wire(llama, tmp_path, monkeypatch)
    llama._llama_build_last = "b10850-abc"
    assert llama.llama_autotune_preflight()["llama_build"] == "b10850-abc"


def test_run_all_removes_the_run_scratch_dir(llama, tmp_path, monkeypatch):
    """base.kld and the stick JSONs are GBs per run; the run dir must not survive it."""
    _wire(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_autotune_run_id", "rX")
    monkeypatch.setattr(llama, "_autotune_set_perf_mode", lambda mode: None)
    monkeypatch.setattr(llama, "_bench_live_runtime",
                        lambda: {"python": "", "script": "", "source": "", "script_status": "ok", "commit": ""})
    monkeypatch.setattr(llama, "_list_cache_ggufs", lambda root: [])
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: llama.configparser.ConfigParser())
    monkeypatch.setattr(llama, "_bench_get_hf_arg", lambda mid: "o/r:Q4")
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: None)
    events = []
    monkeypatch.setattr(llama, "_autotune_put", events.append)
    run_dir = llama._bl.bench_dir(str(tmp_path)) / "runs" / "at-rX"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "base.kld").write_bytes(b"x" * 32)
    (run_dir / "stick-1-org_m_Q4.json").write_text("{}")
    seen = []

    def _fake_run_model(mid, section, req, backend, put, cancelled, env, **kw):
        seen.append(run_dir.exists())
        return {"ok": True, "model_id": mid}
    monkeypatch.setattr(llama._at, "run_model", _fake_run_model)
    llama._autotune_cancel_event.clear()
    llama._autotune_run_all({"model_ids": ["org/m:Q4"], "objective": "fit", "budget_min": 45,
                            "dims": dict(llama._at.DEFAULT_DIMS)})
    assert seen == [True]                                  # scratch is live during the run
    assert events[-1] == {"type": "done", "ok": True, "cancelled": False, "count": 1}
    assert not run_dir.exists()
