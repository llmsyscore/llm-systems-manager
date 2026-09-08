"""#879: live-bench routes validate, refuse when busy, and emit the level events."""
from __future__ import annotations

import contextlib
import importlib.util
import json
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
    from _bench_replay import BenchReplayBuffer  # real, stdlib-only
    _stub("_bench_replay", BenchReplayBuffer=BenchReplayBuffer)
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
    assert llama._bench_active is False
    marker = llama._bl.read_marker(str(tmp_path))
    assert marker["qualitative"]["categories"] == ["coding"]


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
