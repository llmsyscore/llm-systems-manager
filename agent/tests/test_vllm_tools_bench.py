"""#894: vllm bench serve on llama.py's shared bench state — binary resolution, command build,
result parse, run preflight, shared busy lock, fake end-to-end run with ledger row, cancel."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from tests._vllm_tools_load import load


@pytest.fixture(scope="module")
def mods():
    llama, vllm, tools, restore = load()
    yield llama, vllm, tools
    restore()


class _Cfg:
    VLLM_ENABLED = True; VLLM_API_URL = "http://localhost:8000"; VLLM_SYSTEMD_UNIT = "vllm.service"
    VLLM_BENCH_BIN = ""; AGENT_INSTALL_DIR = ""; SPEED_BENCH_PYTHON = ""; MANAGER_URL = ""


class _Ctx:
    def __init__(self): self.config = _Cfg(); self.state = {"token": ""}
    def check_bearer(self, a): return None
    def check_stream_auth(self, *a): return None


@pytest.fixture
def ctx(mods, monkeypatch):
    llama, vllm, tools = mods
    c = _Ctx()
    for m in (llama, vllm): m.set_context(c)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_bench_proc", None); monkeypatch.setattr(llama, "_bench_pgid", None)
    llama._bench_cancel_event.clear()
    monkeypatch.setattr(tools._shared, "post_tool_run", lambda *a, **k: None)
    return c


def _events(llama):
    return [r["event"] for r in llama._bench_replay.records_after_seq(0)]


# ── binary resolution ──────────────────────────────────────────────────

def test_resolve_bin_override_wins(mods, ctx, tmp_path):
    tools = mods[2]
    binf = tmp_path / "vllm"
    binf.write_text("#!/bin/sh\n")
    ctx.config.VLLM_BENCH_BIN = str(binf)
    path, err = tools._bench_resolve_bin()
    assert path == str(binf) and err is None


def test_resolve_bin_override_missing_is_error(mods, ctx, tmp_path):
    tools = mods[2]
    ctx.config.VLLM_BENCH_BIN = str(tmp_path / "nope")
    path, err = tools._bench_resolve_bin()
    assert path is None and "VLLM_BENCH_BIN" in err


def test_resolve_bin_execstart_head_fallback(mods, ctx, tmp_path, monkeypatch):
    tools = mods[2]
    binf = tmp_path / "vllm"
    binf.write_text("#!/bin/sh\n")
    unit = f"[Service]\nExecStart={binf} serve org/m --host 0.0.0.0\n"
    monkeypatch.setattr(tools.Path, "read_text", lambda self: unit)
    path, err = tools._bench_resolve_bin()
    assert path == str(binf) and err is None


def test_resolve_bin_skips_interpreter_head(mods, ctx, monkeypatch):
    tools = mods[2]
    unit = "[Service]\nExecStart=/usr/bin/python3 -m vllm.entrypoints.openai.api_server\n"
    monkeypatch.setattr(tools.Path, "read_text", lambda self: unit)
    monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/local/bin/vllm")
    assert tools._bench_resolve_bin() == ("/usr/local/bin/vllm", None)


def test_resolve_bin_which_then_error(mods, ctx, monkeypatch):
    tools = mods[2]
    monkeypatch.setattr(tools.Path, "read_text",
                        lambda self: (_ for _ in ()).throw(OSError("no unit")))
    monkeypatch.setattr(tools.shutil, "which", lambda _: "/usr/bin/vllm")
    assert tools._bench_resolve_bin() == ("/usr/bin/vllm", None)
    monkeypatch.setattr(tools.shutil, "which", lambda _: None)
    path, err = tools._bench_resolve_bin()
    assert path is None and "vllm[bench]" in err


# ── command build + result parse ───────────────────────────────────────

def test_build_cmd_switches_and_save_result(mods):
    tools = mods[2]
    cmd = tools._bench_build_cmd(
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


def test_extract_extra_scalars_only(mods):
    e = mods[2]._bench_extract_extra(SAMPLE)
    assert e["backend"] == "vllm" and e["num_prompts"] == 200
    assert e["output_throughput"] == 1063.9 and e["p99_itl_ms"] == 30.3
    assert "input_lens" not in e and "itls" not in e


# ── run endpoint: preflight, validation, shared busy lock ──────────────

class _FakeResp:
    ok = True
    def json(self): return {"data": [{"id": "org/m"}]}


def _server_up(vllm, monkeypatch, up=True):
    sess = SimpleNamespace(
        get=(lambda *a, **k: _FakeResp()) if up
        else (lambda *a, **k: (_ for _ in ()).throw(OSError("down"))))
    monkeypatch.setattr(vllm, "_get_session", lambda: sess)


def test_run_refuses_when_server_down(mods, ctx, monkeypatch):
    _llama, vllm, tools = mods
    _server_up(vllm, monkeypatch, up=False)
    r = tools.vllm_bench_run({"switches": []})
    assert r["ok"] is False and "not running" in r["error"]


def test_run_rejects_bad_switches(mods, ctx, monkeypatch):
    _llama, vllm, tools = mods
    _server_up(vllm, monkeypatch)
    with pytest.raises(Exception) as ei:
        tools.vllm_bench_run({"switches": "nope"})
    assert ei.value.status_code == 400


def test_run_reports_missing_binary(mods, ctx, monkeypatch):
    _llama, vllm, tools = mods
    _server_up(vllm, monkeypatch)
    monkeypatch.setattr(tools, "_bench_resolve_bin", lambda: (None, "no vllm"))
    r = tools.vllm_bench_run({"switches": []})
    assert r["ok"] is False and r["error"] == "no vllm"


def test_bench_serve_and_live_share_the_busy_lock(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    class R:  # /v1/models up
        ok = True
        def json(self): return {"data": [{"id": "org/m"}]}
    class _S:
        def get(self, *a, **k): return R()
    monkeypatch.setattr(vllm, "_get_session", _S)
    monkeypatch.setattr(tools, "_bench_resolve_bin", lambda: ("/bin/true", None))
    release = threading.Event()
    monkeypatch.setattr(tools, "_bench_run_one", lambda *a: release.wait())
    try:
        assert tools.vllm_bench_run({"switches": []})["ok"] is True
        assert llama._bench_active is True
        r = tools.vllm_bench_run({"switches": []})
        assert r["ok"] is False and "in progress" in r["error"]
    finally:
        release.set()


def test_run_refused_while_llama_autotune_runs(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    _server_up(vllm, monkeypatch)
    monkeypatch.setattr(tools, "_bench_resolve_bin", lambda: ("/bin/true", None))
    monkeypatch.setattr(llama, "_autotune_active", True)
    r = tools.vllm_bench_run({"switches": []})
    assert r["ok"] is False and "in progress" in r["error"]
    assert llama._bench_active is False


# ── fake end-to-end run + ledger row ───────────────────────────────────

def _fake_popen(tools, monkeypatch, result: dict, chatter: bool = True):
    import subprocess as _sp
    import sys as _sys
    script = (
        "import pathlib, sys\n"
        + ("print('starting bench', flush=True)\n" if chatter else "")
        + "d = sys.argv[sys.argv.index('--result-dir') + 1]\n"
        "pathlib.Path(d, 'result.json').write_text(" + repr(json.dumps(result)) + ")\n"
        + ("print('done bench', flush=True)\n" if chatter else "")
    )
    real_popen = _sp.Popen

    def popen(argv, **kw):
        assert argv[1:3] == ["bench", "serve"]
        return real_popen([_sys.executable, "-c", script] + argv[3:], **kw)
    monkeypatch.setattr(tools.subprocess, "Popen", popen)


def test_bench_run_one_end_to_end(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    _fake_popen(tools, monkeypatch, SAMPLE)
    assert tools._claim() is None
    tools._bench_run_one("/opt/v/bin/vllm", "org/m", [{"flag": "--num-prompts", "value": "5"}])
    events = _events(llama)
    types_ = [e["type"] for e in events]
    assert types_[0] == "model_start" and "cmd" in events[0]
    assert "line" in types_
    res = [e for e in events if e["type"] == "result"][0]
    assert res["extra"]["output_throughput"] == 1063.9 and res["extra"]["backend"] == "vllm"
    md = [e for e in events if e["type"] == "model_done"][0]
    assert md["ok"] is True and md["rc"] == 0
    assert md["run_id"] == llama._bench_replay.run_id and md["bench_tool"] == "vllm-bench-serve"
    assert events[-1] == {"type": "done", "ok": True, "cancelled": False}
    assert llama._bench_active is False and llama._bench_proc is None


def test_bench_model_done_carries_summary_and_posts_the_ledger_row(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    posts = []
    monkeypatch.setattr(tools._shared, "post_tool_run",
                        lambda _c, tool, provider, run_id, model, ok, summary:
                        posts.append({"tool": tool, "provider": provider, "run_id": run_id,
                                      "model_id": model, "ok": ok, **summary}))
    _fake_popen(tools, monkeypatch, {"output_throughput": 1063.9, "total_token_throughput": 9800.2,
                                     "backend": "vllm", "input_lens": [1, 2]}, chatter=False)
    assert tools._claim() is None
    tools._bench_run_one("/opt/v/bin/vllm", "org/m", [])
    events = _events(llama)
    res = [e for e in events if e["type"] == "result"][0]
    assert res["run_id"] == llama._bench_replay.run_id
    md = [e for e in events if e["type"] == "model_done"][0]
    assert md["model_id"] == "org/m" and md["run_id"] == llama._bench_replay.run_id
    assert md["gen_tps"] == 1063.9 and md["pg_tps"] == 9800.2
    assert md["bench_tool"] == "vllm-bench-serve"
    body = posts[0]
    assert body["tool"] == "benchmark" and body["provider"] == "vllm"
    assert body["model_id"] == "org/m" and body["ok"] is True
    assert body["gen_tps"] == 1063.9 and body["pg_tps"] == 9800.2
    assert body["run_id"] == llama._bench_replay.run_id


def test_bench_cancel_returns_ok(mods, ctx):
    llama, _vllm, tools = mods
    r = tools.vllm_bench_cancel()
    assert r["ok"] is True
    assert llama._bench_cancel_event.is_set()
    llama._bench_cancel_event.clear()


def test_tools_state_reports_shared_bench(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    monkeypatch.setattr(llama, "_bench_active", True)
    r = tools.vllm_tools_state()
    assert r["ok"] is True and r["bench_active"] is True and r["autotune_active"] is False


def test_run_refused_while_shared_autotune_runs(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    _server_up(vllm, monkeypatch)
    monkeypatch.setattr(tools, "_bench_resolve_bin", lambda: ("/bin/true", None))
    monkeypatch.setattr(llama, "_autotune_active", True)
    r = tools.vllm_bench_run({"switches": []})
    assert r["ok"] is False and "in progress" in r["error"]
    assert llama._bench_active is False


# ── cancel kills the whole process group ───────────────────────────────

_GROUP_SCRIPT = (
    "import subprocess, sys, time, pathlib\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "pathlib.Path(sys.argv[1]).write_text(str(c.pid))\n"
    "print('line one', flush=True)\n"
    "time.sleep(0.3)\n"
    "print('line two', flush=True)\n"
    "time.sleep(60)\n"
)


def _group_popen(tools, monkeypatch, pidfile, spawned):
    import subprocess as _sp
    import sys as _sys
    real_popen = _sp.Popen

    def popen(argv, **kw):
        p = real_popen([_sys.executable, "-c", _GROUP_SCRIPT, str(pidfile)], **kw)
        spawned.append(p)
        return p
    monkeypatch.setattr(tools.subprocess, "Popen", popen)


def _gone(pid, timeout=5.0):
    import os as _os
    import time as _t
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().split(")")[-1].split()[0] in ("Z", "X"):
                    return True
        except FileNotFoundError:
            return True
        _t.sleep(0.05)
    _os.kill(pid, 9)
    return False


def test_cancel_before_start_kills_group_without_reading(mods, ctx, monkeypatch, tmp_path):
    llama, _vllm, tools = mods
    spawned = []
    _group_popen(tools, monkeypatch, tmp_path / "child.pid", spawned)
    assert tools._claim() is None
    llama._bench_cancel_event.set()
    tools._bench_run_one("/opt/v/bin/vllm", "org/m", [])
    events = _events(llama)
    assert not [e for e in events if e["type"] == "line"]
    assert events[-1] == {"type": "done", "ok": False, "cancelled": True}
    assert spawned[0].poll() is not None
    assert llama._bench_proc is None and llama._bench_pgid is None
    llama._bench_cancel_event.clear()


def test_cancel_mid_run_kills_child_processes(mods, ctx, monkeypatch, tmp_path):
    llama, _vllm, tools = mods
    pidfile = tmp_path / "child.pid"
    spawned = []
    _group_popen(tools, monkeypatch, pidfile, spawned)
    real_put = llama._bench_put

    def put(ev):
        real_put(ev)
        if ev.get("type") == "line":
            llama._bench_cancel_event.set()
    monkeypatch.setattr(llama, "_bench_put", put)
    assert tools._claim() is None
    tools._bench_run_one("/opt/v/bin/vllm", "org/m", [])
    events = _events(llama)
    assert [e["text"] for e in events if e["type"] == "line"] == ["line one"]
    assert events[-1] == {"type": "done", "ok": False, "cancelled": True}
    assert spawned[0].poll() is not None
    assert _gone(int(pidfile.read_text()))
    llama._bench_cancel_event.clear()


# ── live bench routes ─────────────────────────────────────────────────

EXEC = 'ExecStart=/opt/vllm/venv/bin/vllm serve Qwen/Qwen3-0.6B --host 127.0.0.1 --port 8000 --max-num-seqs 4 --speculative-config "{\\"method\\":\\"ngram\\",\\"num_speculative_tokens\\":5}"\n'

class _Sess:
    """Scripted vLLM: /v1/models, /version, /metrics."""
    def __init__(self, up=True, running=1):
        self.up, self.running = up, running
    def get(self, url, **kw):
        if not self.up: raise Exception("refused")
        class R:
            ok = True
            def __init__(self, body, text=""): self._b, self.text = body, text
            def json(self): return self._b
        if url.endswith("/v1/models"): return R({"data": [{"id": "Qwen/Qwen3-0.6B", "max_model_len": 4096}]})
        if url.endswith("/version"): return R({"version": "0.30.0"})
        if url.endswith("/metrics"): return R({}, f"vllm:num_requests_running{{model=\"m\"}} {self.running}\n")
        return R({})

def test_bench_server_reads_served_model_slots_spec_and_build(mods, ctx, monkeypatch, tmp_path):
    llama, vllm, tools = mods
    unit = tmp_path / "vllm.service"; unit.write_text("[Service]\n" + EXEC)
    monkeypatch.setattr(vllm, "_vllm_svc_file_path", lambda: str(unit))
    monkeypatch.setattr(vllm, "_get_session", lambda: _Sess(running=1))
    s = tools._bench_server()
    assert s["up"] and s["provider"] == "vllm" and s["loaded_id"] == "Qwen/Qwen3-0.6B"
    assert s["models"] == [{"id": "Qwen/Qwen3-0.6B", "status": "loaded"}]
    assert (s["slots_total"], s["slots_idle"]) == (4, 3)
    assert s["spec"] == {"method": "ngram", "n": 5} and s["build"] == "0.30.0"

def test_bench_server_defaults_without_unit_file(mods, ctx, monkeypatch, tmp_path):
    llama, vllm, tools = mods
    monkeypatch.setattr(vllm, "_vllm_svc_file_path", lambda: str(tmp_path / "missing.service"))
    monkeypatch.setattr(vllm, "_get_session", lambda: _Sess(running=0))
    s = tools._bench_server()
    assert s["up"] and s["slots_total"] == 256 and s["slots_idle"] == 256 and s["spec"] is None

def test_bench_server_malformed_execstart_falls_back(mods, ctx, monkeypatch, tmp_path):
    llama, vllm, tools = mods
    unit = tmp_path / "vllm.service"; unit.write_text('[Service]\nExecStart=/opt/vllm/bin/vllm serve m --x "unbalanced\n')
    monkeypatch.setattr(vllm, "_vllm_svc_file_path", lambda: str(unit))
    monkeypatch.setattr(vllm, "_get_session", lambda: _Sess(running=0))
    s = tools._bench_server()
    assert s["up"] and s["slots_total"] == 256

def test_bench_server_down(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    monkeypatch.setattr(vllm, "_get_session", lambda: _Sess(up=False))
    s = tools._bench_server()
    assert s == {"up": False, "url": "http://localhost:8000", "provider": "vllm", "models": [], "loaded_id": None,
                 "slots_idle": 0, "slots_total": 0, "spec": None, "build": ""}

def test_preflight_shape(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    monkeypatch.setattr(vllm, "_get_session", _Sess)
    monkeypatch.setattr(llama, "_bench_live_runtime", lambda: {"python": "/p", "script": "/s", "script_status": "ok"})
    p = tools.vllm_bench_live_preflight()
    assert p["ok"] and p["provider"] == "vllm" and p["server"]["up"] and p["busy"] is False
    assert set(p) >= {"server", "runtime", "datasets", "benches"}
    monkeypatch.setattr(llama, "_autotune_active", True)
    assert tools.vllm_bench_live_preflight()["busy"] is True

def test_run_refuses_unserved_model(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    monkeypatch.setattr(vllm, "_get_session", _Sess)
    monkeypatch.setattr(llama, "_bench_live_runtime", lambda: {"python": "/p", "script": "/s", "script_status": "ok"})
    r = tools.vllm_bench_live_run({"model_id": "org/other", "bench": "throughput_1k"})
    assert r["ok"] is False and "does not serve org/other" in r["error"] and llama._bench_active is False

def test_run_starts_through_the_streaming_shim(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    monkeypatch.setattr(vllm, "_get_session", _Sess)
    monkeypatch.setattr(llama, "_bench_live_runtime", lambda: {"python": "/p", "script": "/s", "script_status": "ok"})
    seen = {}
    class FakeShim:
        def __init__(self, upstream, native=None, label=""): seen["ctor"] = (upstream, native, label); self.url = "http://127.0.0.1:1"
        def native_available(self): return False
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(tools._shim, "Shim", FakeShim)
    def run_all(req, srv, python, script, provider="llama"):
        seen["run"] = (req["model_id"], srv["url"], srv["lms_url"], srv["timings"], provider)
        with llama._bench_lock: llama._bench_active = False
    monkeypatch.setattr(llama, "_bench_live_run_all", run_all)
    r = tools.vllm_bench_live_run({"model_id": "Qwen/Qwen3-0.6B", "bench": "throughput_1k"})
    assert r["ok"] and r["run_id"]
    import time
    for _ in range(50):
        if "run" in seen: break
        time.sleep(0.02)
    assert seen["ctor"] == ("http://localhost:8000", False, "vLLM")
    assert seen["run"] == ("Qwen/Qwen3-0.6B", "http://127.0.0.1:1", "http://localhost:8000", "shim", "vllm")


def test_shim_start_failure_frees_the_slot_and_ends_the_run(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    monkeypatch.setattr(vllm, "_get_session", _Sess)
    monkeypatch.setattr(llama, "_bench_live_runtime", lambda: {"python": "/p", "script": "/s", "script_status": "ok"})
    class FailShim:
        def __init__(self, *a, **k): self.url = "http://127.0.0.1:1"
        def __enter__(self): raise OSError("bind failed")
        def __exit__(self, *a): return False
    monkeypatch.setattr(tools._shim, "Shim", FailShim)
    monkeypatch.setattr(llama, "_bench_live_run_all", lambda *a, **k: pytest.fail("run must not start"))
    started = []
    real_thread = threading.Thread
    def thread(*a, **k):
        t = real_thread(*a, **k); started.append(t); return t
    monkeypatch.setattr(tools.threading, "Thread", thread)
    r = tools.vllm_bench_live_run({"model_id": "Qwen/Qwen3-0.6B", "bench": "throughput_1k"})
    assert r["ok"]
    started[0].join(5)
    assert llama._bench_active is False
    events = _events(llama)
    assert events[-1]["type"] == "done" and events[-1]["ok"] is False
    assert any(e["type"] == "line" and "bind failed" in e["text"] for e in events)


def test_route_table_covers_live_and_serve(mods):
    _l, vllm, tools = mods
    paths = {(m, p) for m, p, _h in tools._ROUTES}
    assert paths == {("GET", "/vllm/bench/live/preflight"), ("POST", "/vllm/bench/live/setup"),
                     ("POST", "/vllm/bench/live/run"), ("POST", "/vllm/bench/run"), ("GET", "/vllm/bench/stream"),
                     ("POST", "/vllm/bench/cancel"), ("GET", "/vllm/tools/state"),
                     ("GET", "/vllm/autotune/preflight"), ("POST", "/vllm/autotune/run"),
                     ("GET", "/vllm/autotune/stream"), ("POST", "/vllm/autotune/cancel")}
    assert not any("/vllm/bench" in p or "/vllm/autotune" in p or p == "/vllm/tools/state" for _m, p, _h in vllm._ROUTES)


def test_live_preflight_reports_the_kind_of_run_holding_the_slot(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    class R:  # /v1/models up
        ok = True
        def json(self): return {"data": [{"id": "org/m"}]}
    class _S:
        def get(self, *a, **k): return R()
    monkeypatch.setattr(vllm, "_get_session", _S)
    monkeypatch.setattr(tools, "_bench_resolve_bin", lambda: ("/bin/true", None))
    monkeypatch.setattr(tools, "_bench_server", lambda: {"up": True, "models": [{"id": "org/m"}]})
    monkeypatch.setattr(tools._bl, "read_marker", lambda d: {})
    assert tools.vllm_bench_live_preflight()["busy_kind"] is None
    release = threading.Event()
    monkeypatch.setattr(tools, "_bench_run_one", lambda *a: release.wait())
    try:
        assert tools.vllm_bench_run({"switches": []})["ok"] is True
        p = tools.vllm_bench_live_preflight()
        assert p["busy"] is True and p["busy_kind"] == "offline"
    finally:
        release.set()
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_autotune_active", True)
    assert tools.vllm_bench_live_preflight()["busy_kind"] == "autotune"
