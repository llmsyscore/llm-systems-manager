"""#879: live-bench routes validate, refuse when busy, and emit the level events."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
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
    from _bench_replay import BenchReplayBuffer, AUTOTUNE_KEEP_TYPES  # real, stdlib-only
    _stub("_bench_replay", BenchReplayBuffer=BenchReplayBuffer, AUTOTUNE_KEEP_TYPES=AUTOTUNE_KEEP_TYPES)
    _stub("collectors")
    _stub("collectors.gpu", collect_gpu=lambda *a, **k: {})
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
    def __init__(self, tmp):
        self.config = types.SimpleNamespace(LLAMA_API_URL="http://127.0.0.1:9931", AGENT_INSTALL_DIR=str(tmp),
                                            SPEED_BENCH_PYTHON="", MANAGER_URL="", LLAMA_BIN="", LLAMA_ENABLED=True,
                                            LLAMA_SYSTEMD_UNIT="llama-server", AGENT_USER="")
        self.state = {"token": "", "agent_id": ""}
        self.post_session = None

    def check_bearer(self, *a, **k):
        return None


def _events(llama):
    return [rec["event"] for rec in llama._bench_replay.records_after_seq(0)]


def test_run_rejects_bad_body_and_busy(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    with pytest.raises(_HTTPException) as ei:
        llama.llama_bench_live_run({"bench": "qualitative"})
    assert ei.value.status_code == 400
    monkeypatch.setattr(llama, "_bench_active", True)
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative"})
    assert out["ok"] is False and "in progress" in out["error"]


def test_run_refuses_when_server_down_or_runtime_missing(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_bench_live_server", lambda: {"up": False, "url": "", "models": [], "loaded_id": None})
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative"})
    assert out["ok"] is False and "server" in out["error"]
    monkeypatch.setattr(llama, "_bench_live_server",
                        lambda: {"up": True, "url": "http://127.0.0.1:9931", "models": [{"id": "org/m:Q4", "status": "loaded"}], "loaded_id": "org/m:Q4"})
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative"})
    assert out["ok"] is False and "runtime" in out["error"]


def test_run_levels_emits_level_results_and_done(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_bench_live_server",
                        lambda: {"up": True, "url": "http://127.0.0.1:9931", "models": [{"id": "org/m:Q4", "status": "loaded"}],
                                 "loaded_id": "org/m:Q4", "slots_idle": 2, "slots_total": 2, "spec": None})
    monkeypatch.setattr(llama._bl, "runtime_python", lambda inst, ov: ("/fake/python", "venv"))
    monkeypatch.setattr(llama._bl, "script_path", lambda inst, root: (Path("/fake/speed_bench.py"), "ok"))
    payload = {"summary": [{"category": "coding", "requests": 2, "turns": 2, "failed": 0, "avg_prompt_t_s": 2000.0,
                            "avg_pred_t_s": 100.0, "avg_latency": 4.0, "draft_n": 0, "accepted": 0, "accept_rate": None},
                           {"category": "overall", "requests": 2, "turns": 2, "failed": 0, "avg_prompt_t_s": 2000.0,
                            "avg_pred_t_s": 100.0, "avg_latency": 4.0, "draft_n": 0, "accepted": 0, "accept_rate": None}],
               "results": [{"ok": True, "completion_tokens": 200, "latency_s": 4.0}] * 2}

    def fake_level(cmd, env, put, model_id, level, cancel, track, untrack):
        out = cmd[cmd.index("--output") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(payload))
        put({"type": "line", "model_id": model_id, "text": f"fake level {level}"})
        return 0, False, 7.0
    monkeypatch.setattr(llama._bl, "run_level_subprocess", fake_level)
    monkeypatch.setattr(llama, "_live_power_w", lambda: (200.0, "psu"))
    posted = []
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: posted.append((a, k)))
    stored = []
    monkeypatch.setattr(llama, "_bench_live_store", lambda doc: stored.append(doc))
    monkeypatch.setattr(llama, "_conditions_facts", lambda mid: {"model_id": mid})
    monkeypatch.setattr(llama.run_conditions, "samples", lambda: (None, None))
    done = threading.Event()
    orig = llama._bench_live_run_all

    def wrapped(*a, **k):
        try:
            orig(*a, **k)
        finally:
            done.set()
    monkeypatch.setattr(llama, "_bench_live_run_all", wrapped)
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative", "concurrency": [1, 2], "limit": 2})
    assert out["ok"] is True and out["run_id"]
    assert done.wait(10)
    types_ = [e["type"] for e in _events(llama)]
    assert types_[0] == "model_start" and types_[-1] == "done"
    assert types_.count("level_start") == 2 and types_.count("level_result") == 2
    md = next(e for e in _events(llama) if e["type"] == "model_done")
    assert md["ok"] is True and len(md["levels"]) == 2
    assert md["levels"][0]["concurrency"] == 1 and md["levels"][0]["all"]["pred_tps"] == 100.0
    assert md["levels"][0]["wall_s"] == 7.0
    assert md["run_id"] == out["run_id"] and md["bench"] == "qualitative"
    assert stored and stored[0]["run_id"] == out["run_id"] and stored[0]["model_id"] == "org/m:Q4"
    assert posted and posted[0][0][1] == "benchmark" and posted[0][0][6]["bench_tool"] == "speed-bench"
    cond = md["conditions"]
    assert cond["captured_start"] and cond["captured_end"] and cond["model"]["id"] == "org/m:Q4"
    assert posted[0][1]["conditions"] is cond
    assert llama._bench_active is False
    marker = llama._bl.read_marker(str(tmp_path))
    assert marker["qualitative"]["categories"] == ["coding"]


def test_failed_level_still_records_conditions_end(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_bench_live_server",
                        lambda: {"up": True, "url": "http://127.0.0.1:9931", "models": [{"id": "org/m:Q4", "status": "loaded"}],
                                 "loaded_id": "org/m:Q4", "slots_idle": 2, "slots_total": 2, "spec": None})
    monkeypatch.setattr(llama._bl, "runtime_python", lambda inst, ov: ("/fake/python", "venv"))
    monkeypatch.setattr(llama._bl, "script_path", lambda inst, root: (Path("/fake/speed_bench.py"), "ok"))

    def boom(*a, **k):
        raise RuntimeError("level blew up")
    monkeypatch.setattr(llama._bl, "run_level_subprocess", boom)
    monkeypatch.setattr(llama, "_live_power_w", lambda: (None, None))
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: None)
    monkeypatch.setattr(llama, "_bench_live_store", lambda doc: None)
    monkeypatch.setattr(llama, "_conditions_facts", lambda mid: {"model_id": mid})
    monkeypatch.setattr(llama.run_conditions, "samples", lambda: (None, None))
    done = threading.Event()
    orig = llama._bench_live_run_all

    def wrapped(*a, **k):
        try:
            orig(*a, **k)
        finally:
            done.set()
    monkeypatch.setattr(llama, "_bench_live_run_all", wrapped)
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative", "concurrency": [1], "limit": 2})
    assert out["ok"] is True and done.wait(10)
    md = next(e for e in _events(llama) if e["type"] == "model_done")
    assert md["ok"] is False and md["levels"] == []
    assert md["conditions"]["captured_start"] and md["conditions"]["captured_end"]


def test_run_matrix_expands_cells_and_tags_events(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_bench_live_server",
                        lambda: {"up": True, "url": "http://127.0.0.1:9931", "models": [{"id": "org/m:Q4", "status": "loaded"}],
                                 "loaded_id": "org/m:Q4", "slots_idle": 2, "slots_total": 2, "spec": None})
    monkeypatch.setattr(llama._bl, "runtime_python", lambda inst, ov: ("/fake/python", "venv"))
    monkeypatch.setattr(llama._bl, "script_path", lambda inst, root: (Path("/fake/speed_bench.py"), "ok"))
    payload = {"summary": [{"category": "coding", "requests": 2, "turns": 2, "failed": 0, "avg_prompt_t_s": 2000.0,
                            "avg_pred_t_s": 100.0, "avg_latency": 4.0, "draft_n": 0, "accepted": 0, "accept_rate": None},
                           {"category": "overall", "requests": 2, "turns": 2, "failed": 0, "avg_prompt_t_s": 2000.0,
                            "avg_pred_t_s": 100.0, "avg_latency": 4.0, "draft_n": 0, "accepted": 0, "accept_rate": None}],
               "results": [{"ok": True, "completion_tokens": 200, "latency_s": 4.0}] * 2}
    argvs = []

    def fake_level(cmd, env, put, model_id, level, cancel, track, untrack):
        argvs.append(cmd)
        out = cmd[cmd.index("--output") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(payload))
        put({"type": "line", "model_id": model_id, "text": f"fake level {level}"})
        return 0, False, 7.0
    monkeypatch.setattr(llama._bl, "run_level_subprocess", fake_level)
    monkeypatch.setattr(llama, "_live_power_w", lambda: (200.0, "psu"))
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: None)
    monkeypatch.setattr(llama, "_bench_live_store", lambda doc: None)
    done = threading.Event()
    orig = llama._bench_live_run_all

    def wrapped(*a, **k):
        try:
            orig(*a, **k)
        finally:
            done.set()
    monkeypatch.setattr(llama, "_bench_live_run_all", wrapped)
    matrix = {"benches": ["throughput_1k", "throughput_8k"], "osls": [256, 1024]}
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "matrix": matrix, "concurrency": [1]})
    assert out["ok"] is True and out["run_id"]
    assert done.wait(10)
    events = _events(llama)
    cells = [{"bench": "throughput_1k", "osl": 256}, {"bench": "throughput_1k", "osl": 1024},
             {"bench": "throughput_8k", "osl": 256}, {"bench": "throughput_8k", "osl": 1024}]
    level_results = [e for e in events if e["type"] == "level_result"]
    assert len(level_results) == 4
    for e, cell in zip(level_results, cells):
        assert e["bench"] == cell["bench"] and e["osl"] == cell["osl"] and e["concurrency"] == 1
    level_starts = [e for e in events if e["type"] == "level_start"]
    assert len(level_starts) == 4
    for e, cell in zip(level_starts, cells):
        assert e["bench"] == cell["bench"] and e["osl"] == cell["osl"]
    md = next(e for e in events if e["type"] == "model_done")
    assert len(md["levels"]) == 4
    for row, cell in zip(md["levels"], cells):
        assert row["bench"] == cell["bench"] and row["osl"] == cell["osl"]
    assert md["config"]["matrix"] == matrix
    last_argv = argvs[-1]
    i = last_argv.index("--bench")
    assert last_argv[i + 1] == "throughput_8k"
    j = last_argv.index("--osl")
    assert last_argv[j + 1] == "1024"


def test_autotune_refuses_while_bench_active(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="inactive", stderr="", returncode=3))
    monkeypatch.setattr(llama, "_bench_active", True)
    out = llama.llama_autotune_run({"model_ids": ["m"], "target_mb": 1024})
    assert out["ok"] is False and "in progress" in out["error"]
    assert llama._autotune_active is False


def test_preflight_shape(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_bench_live_server",
                        lambda: {"up": True, "url": "http://127.0.0.1:9931", "models": [], "loaded_id": None, "slots_idle": 0, "slots_total": 0, "spec": None})
    out = llama.llama_bench_live_preflight()
    assert out["ok"] is True and out["server"]["up"] is True
    assert out["runtime"] == {"python": None, "source": "missing", "script": None, "script_status": "speed_bench.py not installed", "commit": llama._bl.SCRIPT_COMMIT}
    assert out["datasets"] == {} and out["benches"] == list(llama._bl.BENCHES)


def test_routes_registered(llama):
    paths = {p for _, p, _ in llama._ROUTES}
    assert {"/llama/bench/live/preflight", "/llama/bench/live/setup", "/llama/bench/live/run"} <= paths


def test_setup_cancel_between_steps_emits_terminal_event(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama._bl, "script_path", lambda inst, root: (Path("/fake/speed_bench.py"), "ok"))
    monkeypatch.setattr(llama._bl, "runtime_python", lambda inst, ov: ("/fake/python", "venv"))
    llama._bench_replay.start_run("cancelsetup")
    llama._bench_cancel_event.set()
    try:
        llama._bench_live_setup_job([])
    finally:
        llama._bench_cancel_event.clear()
    ev = _events(llama)
    assert ev and ev[-1] == {"type": "setup_done", "ok": False, "error": "cancelled"}


def _failed_payload(ok_n: int, failed_n: int):
    tps = 100.0 if ok_n else None
    results = [{"ok": True, "completion_tokens": 200, "latency_s": 4.0, "error": None}] * ok_n \
        + [{"ok": False, "completion_tokens": 0, "latency_s": 60.0, "error": "Read timed out. (read timeout=60.0)"}] * failed_n
    return {"summary": [{"category": "overall", "requests": ok_n, "turns": ok_n, "failed": failed_n,
                         "avg_prompt_t_s": 2000.0 if ok_n else None, "avg_pred_t_s": tps,
                         "avg_latency": 4.0 if ok_n else None, "draft_n": 0, "accepted": 0, "accept_rate": None}],
            "results": results}


def _run_with_payload(llama, tmp_path, monkeypatch, payload, rc):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_bench_live_server",
                        lambda: {"up": True, "url": "http://127.0.0.1:9931", "models": [{"id": "org/m:Q4", "status": "loaded"}],
                                 "loaded_id": "org/m:Q4", "slots_idle": 2, "slots_total": 2, "spec": None})
    monkeypatch.setattr(llama._bl, "runtime_python", lambda inst, ov: ("/fake/python", "venv"))
    monkeypatch.setattr(llama._bl, "script_path", lambda inst, root: (Path("/fake/speed_bench.py"), "ok"))

    def fake_level(cmd, env, put, model_id, level, cancel, track, untrack):
        out = cmd[cmd.index("--output") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(payload))
        return rc, False, 7.0
    monkeypatch.setattr(llama._bl, "run_level_subprocess", fake_level)
    monkeypatch.setattr(llama, "_live_power_w", lambda: (None, None))
    posted = []
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(llama, "_bench_live_store", lambda doc: None)
    done = threading.Event()
    orig = llama._bench_live_run_all

    def wrapped(*a, **k):
        try:
            orig(*a, **k)
        finally:
            done.set()
    monkeypatch.setattr(llama, "_bench_live_run_all", wrapped)
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative", "concurrency": [1, 2], "limit": 2})
    assert out["ok"] is True
    assert done.wait(10)
    return _events(llama), posted


def test_failed_samples_flag_the_run_and_ledger(llama, tmp_path, monkeypatch):
    # #1127: speed-bench exits 1 when a sample failed; the run still completes but carries the count.
    events, posted = _run_with_payload(llama, tmp_path, monkeypatch, _failed_payload(1, 2), rc=1)
    lv = [e for e in events if e["type"] == "level_result"]
    assert len(lv) == 2 and lv[0]["all"]["failed"] == 2
    assert lv[0]["all"]["first_error"].startswith("Read timed out")
    md = next(e for e in events if e["type"] == "model_done")
    assert md["ok"] is True and md["failed_samples"] == 4
    assert md["first_error"].startswith("Read timed out")
    dn = events[-1]
    assert dn["type"] == "done" and dn["ok"] is True and dn["failed_samples"] == 4
    summary = posted[0][0][6]
    assert posted[0][0][5] is True
    assert summary["failed_samples"] == 4 and summary["first_error"].startswith("Read timed out")


def test_level_with_every_sample_failed_fails_the_run(llama, tmp_path, monkeypatch):
    # #1127: no sample answered, so there is no measurement; the run stops and is recorded as failed.
    events, posted = _run_with_payload(llama, tmp_path, monkeypatch, _failed_payload(0, 2), rc=1)
    lv = [e for e in events if e["type"] == "level_result"]
    assert len(lv) == 1
    md = next(e for e in events if e["type"] == "model_done")
    assert md["ok"] is False and md["failed_samples"] == 2 and md["cancelled"] is False
    assert md["first_error"].startswith("Read timed out")
    assert events[-1]["type"] == "done" and events[-1]["ok"] is False
    assert posted[0][0][5] is False and posted[0][0][6]["failed_samples"] == 2


_OK_PAYLOAD = {"summary": [{"category": "overall", "requests": 2, "turns": 2, "failed": 0, "avg_prompt_t_s": 2000.0,
                            "avg_pred_t_s": 100.0, "avg_latency": 4.0, "draft_n": 0, "accepted": 0, "accept_rate": None}],
               "results": [{"ok": True, "completion_tokens": 200, "latency_s": 4.0}] * 2}


def _builds_env(llama, tmp_path, monkeypatch, start_loaded="org/m:Q4", switch_hook=None, fail=(), fail_back=False):
    """Live run with builds: switch/load/server stubs that track which build the service runs."""
    ctx = _Ctx(tmp_path)
    paths = {n: str(tmp_path / f"llama-{n}" / "build" / "bin" / "llama-server") for n in ("a", "b", "c")}
    ctx.config.LLAMA_BIN = paths["a"]
    ctx.config.AGENT_OS = "linux"
    svc = tmp_path / "llama-server.service"
    svc.write_text("[Service]\nExecStart=x\n")
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_llama_svc_file_path", lambda: str(svc))
    monkeypatch.setattr(llama, "_validate_builds", lambda raw: list(raw or []))
    st = {"loaded": start_loaded, "switches": [], "loads": [], "urls": [], "waits": []}

    def server():
        name = Path(ctx.config.LLAMA_BIN).parents[2].name
        return {"up": True, "url": f"http://127.0.0.1:9931/{name}", "models": [{"id": "org/m:Q4", "status": "loaded"}],
                "loaded_id": st["loaded"], "slots_idle": 2, "slots_total": 2, "spec": None}

    def switch(path, wait_s=None):
        st["switches"].append(path)
        st["waits"].append(wait_s)
        if switch_hook:
            switch_hook(path)
        if path in fail or (fail_back and path == paths["a"]):
            return {"ok": False, "stage": "check", "error": "the version check timed out", "changed": False}
        ctx.config.LLAMA_BIN = path
        st["loaded"] = None
        return {"ok": True, "active": path}

    def load(mid, timeout_s=300):
        st["loads"].append((Path(ctx.config.LLAMA_BIN).parents[2].name, mid))
        st["loaded"] = mid
        return True, None
    monkeypatch.setattr(llama, "_bench_live_server", server)
    monkeypatch.setattr(llama, "_switch_build", switch)
    monkeypatch.setattr(llama, "_live_load_and_wait", load)
    monkeypatch.setattr(llama._bl, "runtime_python", lambda inst, ov: ("/fake/python", "venv"))
    monkeypatch.setattr(llama._bl, "script_path", lambda inst, root: (Path("/fake/speed_bench.py"), "ok"))

    def fake_level(cmd, env, put, model_id, level, cancel, track, untrack):
        st["urls"].append(cmd[cmd.index("--url") + 1])
        out = cmd[cmd.index("--output") + 1]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(_OK_PAYLOAD))
        return 0, False, 7.0
    monkeypatch.setattr(llama._bl, "run_level_subprocess", fake_level)
    monkeypatch.setattr(llama, "_live_power_w", lambda: (None, None))
    monkeypatch.setattr(llama._shared, "post_tool_run", lambda *a, **k: None)
    st["stored"] = []
    monkeypatch.setattr(llama, "_bench_live_store", lambda doc: st["stored"].append(doc))
    monkeypatch.setattr(llama, "_conditions_facts", lambda mid, bin_path=None: {"model_id": mid, "bin": bin_path})
    monkeypatch.setattr(llama.run_conditions, "samples", lambda: (None, None))
    done = threading.Event()
    orig = llama._bench_live_run_all

    def wrapped(*a, **k):
        try:
            orig(*a, **k)
        finally:
            done.set()
    monkeypatch.setattr(llama, "_bench_live_run_all", wrapped)
    return ctx, paths, st, done


def _run_builds(llama, builds, done):
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative", "concurrency": [1],
                                      "limit": 2, "builds": builds})
    assert out["ok"] is True
    assert done.wait(10)
    return _events(llama)


def test_live_builds_switch_each_then_back(llama, tmp_path, monkeypatch):
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch)
    ev = _run_builds(llama, [p["a"], p["b"], p["c"]], done)
    assert st["switches"] == [p["b"], p["c"], p["a"]]
    assert ctx.config.LLAMA_BIN == p["a"]
    bs = [e for e in ev if e["type"] == "build_start"]
    assert [(e["build"], e["path"], e["index"], e["total"]) for e in bs] == \
        [("llama-a", p["a"], 0, 3), ("llama-b", p["b"], 1, 3), ("llama-c", p["c"], 2, 3)]
    md = [e for e in ev if e["type"] == "model_done"]
    assert [(e["build"], e["build_path"]) for e in md] == [("llama-a", p["a"]), ("llama-b", p["b"]), ("llama-c", p["c"])]
    assert all(e["ok"] for e in md)
    assert [d["build_path"] for d in st["stored"]] == [p["a"], p["b"], p["c"]]
    base = llama._bench_replay.run_id
    assert [d["run_id"] for d in st["stored"]] == [base, f"{base}-2", f"{base}-3"]
    assert [e["run_id"] for e in md] == [base] * 3
    assert [e["stored_run_id"] for e in md] == [base, f"{base}-2", f"{base}-3"]
    assert st["waits"] == [None, None, 420.0]
    assert st["urls"] == ["http://127.0.0.1:9931/llama-a", "http://127.0.0.1:9931/llama-b", "http://127.0.0.1:9931/llama-c"]
    assert st["loads"] == [("llama-b", "org/m:Q4"), ("llama-c", "org/m:Q4"), ("llama-a", "org/m:Q4")]
    sb = [e for e in ev if e["type"] == "switch_back"]
    assert sb == [{"type": "switch_back", "ok": True, "error": None}]
    assert ev[-1]["type"] == "done" and ev[-1]["ok"] is True and ev[-1]["count"] == 3
    assert llama._bench_active is False


def test_live_failed_switch_continues(llama, tmp_path, monkeypatch):
    ctx2, p, st, done = _builds_env(llama, tmp_path, monkeypatch, fail=(str(tmp_path / "llama-b" / "build" / "bin" / "llama-server"),))
    ev = _run_builds(llama, [p["a"], p["b"], p["c"]], done)
    bf = [e for e in ev if e["type"] == "build_failed"]
    assert bf == [{"type": "build_failed", "build": "llama-b", "stage": "check", "error": "the version check timed out"}]
    md = [e for e in ev if e["type"] == "model_done"]
    assert [e["build_path"] for e in md] == [p["a"], p["c"]]
    assert st["switches"] == [p["b"], p["c"], p["a"]]
    assert ctx2.config.LLAMA_BIN == p["a"]
    assert ev[-1]["type"] == "done" and ev[-1]["ok"] is False and ev[-1]["cancelled"] is False


def test_live_cancel_mid_switch_switches_back(llama, tmp_path, monkeypatch):
    def hook(path):
        if path.endswith("llama-b/build/bin/llama-server"):
            assert llama._bench_cancel_impl([])["ok"] is True
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch, switch_hook=hook)
    ev = _run_builds(llama, [p["a"], p["b"], p["c"]], done)
    assert st["switches"] == [p["b"], p["a"]]
    assert ctx.config.LLAMA_BIN == p["a"]
    md = [e for e in ev if e["type"] == "model_done"]
    assert [e["build_path"] for e in md] == [p["a"]]
    assert not any(e["type"] == "build_start" and e["path"] == p["c"] for e in ev)
    assert [e for e in ev if e["type"] == "switch_back"] == [{"type": "switch_back", "ok": True, "error": None}]
    assert ev[-1]["type"] == "done" and ev[-1]["cancelled"] is True and ev[-1]["ok"] is False
    llama._bench_cancel_event.clear()


@pytest.mark.parametrize("start_loaded", ["org/m:Q4", None])
def test_live_reloads_start_model_after_switch_back(llama, tmp_path, monkeypatch, start_loaded):
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch, start_loaded=start_loaded)
    ev = _run_builds(llama, [p["b"]], done)
    assert st["switches"] == [p["b"], p["a"]]
    assert not [e for e in ev if e["type"] == "build_start"]
    if start_loaded:
        assert st["loads"] == [("llama-b", "org/m:Q4"), ("llama-a", "org/m:Q4")]
        assert st["loaded"] == "org/m:Q4"
    else:
        assert st["loads"] == [("llama-b", "org/m:Q4")]
    md = next(e for e in ev if e["type"] == "model_done")
    assert md["build_path"] == p["b"] and md["run_id"] == llama._bench_replay.run_id
    assert [e["type"] for e in ev][-2:] == ["switch_back", "done"]


def test_live_builds_conditions_record_the_build(llama, tmp_path, monkeypatch):
    seen = []
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_conditions_facts", lambda mid, bin_path=None: seen.append(bin_path) or {"model_id": mid})
    _run_builds(llama, [p["a"], p["b"]], done)
    assert seen == [p["a"], p["b"]]


def test_live_no_builds_never_switches(llama, tmp_path, monkeypatch):
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_switch_build", lambda path: pytest.fail("must not switch"))
    monkeypatch.setattr(llama, "_live_load_and_wait", lambda *a, **k: pytest.fail("must not load"))
    monkeypatch.setattr(llama, "_validate_builds", lambda raw: pytest.fail("no builds to validate") if raw else [])
    out = llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative", "concurrency": [1], "limit": 2})
    assert out["ok"] is True and done.wait(10)
    ev = _events(llama)
    assert [e["type"] for e in ev if e["type"] in ("build_start", "build_failed", "switch_back")] == []
    md = next(e for e in ev if e["type"] == "model_done")
    assert "build" not in md and "build_path" not in md
    assert ev[-1]["type"] == "done" and ev[-1]["count"] == 1


def test_live_builds_need_linux_service(llama, tmp_path, monkeypatch):
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch)
    ctx.config.AGENT_OS = "darwin"
    with pytest.raises(_HTTPException) as ei:
        llama.llama_bench_live_run({"model_id": "org/m:Q4", "bench": "qualitative", "builds": [p["b"]]})
    assert ei.value.status_code == 400 and "systemd service on Linux" in ei.value.detail


def _activate_env(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    ctx.config.AGENT_OS = "linux"
    target = str(tmp_path / "llama-b" / "build" / "bin" / "llama-server")
    svc = tmp_path / "llama-server.service"
    svc.write_text("[Service]\nExecStart=x\n")
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_llama_svc_file_path", lambda: str(svc))
    monkeypatch.setattr(llama.llama_inventory, "cached", lambda: {"builds": []})
    monkeypatch.setattr(llama.llama_inventory, "listed_build",
                        lambda pth: {"path": target, "ok": True, "active": False})
    return target


def test_activate_still_409_when_bench_active(llama, tmp_path, monkeypatch):
    target = _activate_env(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_bench_active", True)
    with pytest.raises(_HTTPException) as ei:
        llama.llama_builds_activate({"path": target})
    assert ei.value.status_code == 409 and "benchmark" in ei.value.detail
    assert not llama._switch_lock.locked()
    monkeypatch.setattr(llama, "_bench_active", False)
    assert llama._switch_lock.acquire(blocking=False)
    try:
        with pytest.raises(_HTTPException) as ei:
            llama.llama_builds_activate({"path": target})
        assert ei.value.status_code == 409 and ei.value.detail == "a switch is already running"
    finally:
        llama._switch_lock.release()


def test_switch_build_waits_then_gives_up_when_lock_held(llama, tmp_path, monkeypatch):
    _activate_env(llama, tmp_path, monkeypatch)
    monkeypatch.setattr(llama, "_SWITCH_LOCK_WAIT_S", 0.05)
    assert llama._switch_lock.acquire(blocking=False)
    try:
        assert llama._switch_build("/x/llama-server") == {"ok": False, "error": "a switch is already running"}
    finally:
        llama._switch_lock.release()


def test_switch_build_runs_switch_and_forgets_cache(llama, tmp_path, monkeypatch):
    target = _activate_env(llama, tmp_path, monkeypatch)
    ctx = llama._require_ctx()
    ctx.config.LLAMA_BIN = str(tmp_path / "llama-a" / "build" / "bin" / "llama-server")
    monkeypatch.setattr(llama, "_bench_active", True)
    monkeypatch.setattr(llama, "_llama_exec_tokens", lambda: [ctx.config.LLAMA_BIN, "--port", "8080"])
    seen = {}
    monkeypatch.setattr(llama.llama_inventory, "switch",
                        lambda tgt, tokens, **k: seen.update(tgt=tgt, tokens=tokens) or {"ok": True, "active": tgt})
    forgot = []
    monkeypatch.setattr(llama.llama_inventory, "forget_cache", lambda: forgot.append(1))
    assert llama._switch_build(target) == {"ok": True, "active": target}
    assert seen == {"tgt": target, "tokens": [ctx.config.LLAMA_BIN, "--port", "8080"]} and forgot == [1]
    assert not llama._switch_lock.locked()


def test_live_load_and_wait_polls_until_loaded(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "reconcile_now", lambda: None)
    calls = []

    class R:
        def __init__(self, code=200, body=None):
            self.status_code, self.ok, self._b, self.text = code, code < 400, body or {}, ""
        def json(self):
            return self._b
    polls = iter([{"data": [{"id": "m", "status": {"value": "loading"}}]},
                  {"data": [{"id": "m", "status": {"value": "loaded"}}]}])
    monkeypatch.setattr(llama, "_llama_load_request", lambda api, mid: calls.append(mid) or R())
    monkeypatch.setattr(llama, "_llama_fetch_model_entries",
                        lambda: {e["id"]: e for e in next(polls)["data"]})
    monkeypatch.setattr(llama.time, "sleep", lambda s: None)
    assert llama._live_load_and_wait("m") == (True, None)
    assert calls == ["m"]
    monkeypatch.setattr(llama, "_llama_load_request", lambda api, mid: None)
    ok, err = llama._live_load_and_wait("m")
    assert ok is False and "did not unload" in err


def test_live_build_label_not_stale_after_switch(llama, tmp_path, monkeypatch):
    inv = llama.llama_inventory
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch, switch_hook=lambda path: inv.forget_cache())
    monkeypatch.setattr(llama, "_llama_build_last", "b1000-old")
    seeded = {"builds": [{"path": os.path.realpath(p[n]), "name": f"llama-{n}", "backend": "ROCm",
                          "version": v, "build": f"b{v}", "note": "", "ok": True}
                         for n, v in (("a", "1000"), ("b", "2000"))]}
    monkeypatch.setitem(inv._state, "last", seeded)
    monkeypatch.setattr(inv, "forget_cache", lambda: inv._state.update(last=None))
    scans = []
    monkeypatch.setattr(inv, "scan", lambda *a, **k: scans.append(1) or inv._state.update(last=seeded) or seeded)
    monkeypatch.setattr(llama.llama_install, "installed_build_id", lambda *a: None, raising=False)
    monkeypatch.setattr(llama.llama_install, "_build_root", lambda cfg: tmp_path, raising=False)
    monkeypatch.setattr(llama, "_conditions_facts",
                        lambda mid, bin_path=None: {"model_id": mid, "install": llama._build_identity(bin_path)})
    _run_builds(llama, [p["a"], p["b"]], done)
    assert [(d["build_path"], d["llama_build"]) for d in st["stored"]] == [(p["a"], "b1000"), (p["b"], "b2000")]
    assert [d["conditions"]["backend"]["install"]["version"] for d in st["stored"]] == ["1000", "2000"]
    assert scans


def test_live_switch_back_failure_skips_reload(llama, tmp_path, monkeypatch):
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch, fail_back=True)
    ev = _run_builds(llama, [p["b"]], done)
    assert st["switches"] == [p["b"], p["a"]]
    assert st["loads"] == [("llama-b", "org/m:Q4")]
    assert [e for e in ev if e["type"] == "switch_back"] == \
        [{"type": "switch_back", "ok": False, "error": "the version check timed out"}]
    assert ev[-1]["type"] == "done"


def test_live_build_run_error_still_reports_and_switches_back(llama, tmp_path, monkeypatch):
    ctx, p, st, done = _builds_env(llama, tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("level blew up")
    monkeypatch.setattr(llama._bl, "run_level_subprocess", boom)
    ev = _run_builds(llama, [p["a"], p["b"]], done)
    md = [e for e in ev if e["type"] == "model_done"]
    assert [(e["build_path"], e["ok"], e["failed_samples"]) for e in md] == [(p["a"], False, 0), (p["b"], False, 0)]
    assert st["switches"] == [p["b"], p["a"]] and ctx.config.LLAMA_BIN == p["a"]
    assert ev[-1] == {"type": "done", "ok": False, "cancelled": False, "count": 2, "failed_samples": 0, "first_error": None}


def test_setup_rebuilds_missing_venv_with_clear(llama, tmp_path, monkeypatch):
    ctx = _Ctx(tmp_path)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama._bl, "script_path", lambda inst, root: (Path("/fake/speed_bench.py"), "ok"))
    monkeypatch.setattr(llama._bl, "runtime_python", lambda inst, ov: (None, "missing"))
    monkeypatch.setattr(llama._bl, "venv_python", lambda frozen=False: "/fake/python3")
    cmds = []

    class _Proc:
        def __init__(self, cmd, **k):
            cmds.append(cmd)
            self.pid, self.returncode = 0, 1
            self.stdout = io.StringIO("")

        def wait(self):
            return 1
    monkeypatch.setattr(llama.subprocess, "Popen", _Proc)
    monkeypatch.setattr(llama.os, "getpgid", lambda pid: 0)
    llama._bench_replay.start_run("venvclear")
    llama._bench_live_setup_job([])
    venv = str(llama._bl.bench_dir(str(tmp_path)) / "venv")
    assert cmds == [["/fake/python3", "-m", "venv", venv, "--clear"]]
