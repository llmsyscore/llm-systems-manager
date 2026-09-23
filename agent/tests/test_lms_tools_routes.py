"""#916: LM Studio live-bench + autotune routes share llama's run state; the backend loads through the native API."""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_AGENT_ROOT))


class _Timeout(Exception):
    pass


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code, self.detail = status_code, detail


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _file_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load():
    """Hermetic loader: stubs the agent's third-party imports, loads the real provider files,
    and hands back a restore() that puts sys.modules back so no other test file sees the stubs."""
    import contextlib
    snapshot = dict(sys.modules)
    _stub("requests", Session=object, RequestException=Exception,
          exceptions=types.SimpleNamespace(Timeout=_Timeout, RequestException=Exception))
    _stub("fastapi", Header=lambda **k: None, HTTPException=_HTTPException, Query=lambda *a, **k: None, Request=object)
    _stub("fastapi.responses", Response=object, StreamingResponse=object)
    _stub("starlette.concurrency", run_in_threadpool=None)
    _stub("starlette")
    _stub("stream_pool")

    @contextlib.contextmanager
    def _be(*a, **k):
        yield
    _stub("_best_effort", best_effort=_be)
    _file_module("_bench_replay", _AGENT_ROOT / "_bench_replay.py")
    _stub("collectors")
    _stub("collectors.gpu", collect_gpu=lambda *a, **k: {})
    pkg = types.ModuleType("providers")
    pkg.__path__ = [str(_AGENT_ROOT / "providers")]
    sys.modules["providers"] = pkg
    for sub in ("llama_install", "llama_sse", "llama_upgrade"):
        sys.modules[f"providers.{sub}"] = types.ModuleType(f"providers.{sub}")
    llama = _file_module("providers.llama", _AGENT_ROOT / "providers" / "llama.py")
    for name in ("lms", "lms_autotune", "lms_tools"):
        _file_module(f"providers.{name}", _AGENT_ROOT / "providers" / f"{name}.py")

    def restore():
        for k in list(sys.modules):
            if k not in snapshot:
                sys.modules.pop(k, None)
        sys.modules.update(snapshot)
    return llama, sys.modules["providers.lms"], sys.modules["providers.lms_tools"], restore


@pytest.fixture(scope="module")
def mods():
    llama, lms, tools, restore = _load()
    yield llama, lms, tools
    restore()


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.ok, self.text = status, body, status < 400, "raw"

    def json(self):
        return self._body


NATIVE = {"models": [
    {"type": "llm", "key": "qwen3.5-9b@q6_k", "display_name": "Qwen3.5 9B", "params_string": "9B", "size_bytes": 9_000_000_000,
     "max_context_length": 262144, "quantization": {"name": "Q6_K"},
     "loaded_instances": [{"id": "qwen3.5-9b@q6_k", "config": {"context_length": 32768, "parallel": 2, "flash_attention": True,
                                                              "speculative_draft_mtp": True, "speculative_draft_max_tokens": 3,
                                                              "speculative_draft_min_tokens": 0,
                                                              "speculative_draft_min_continue_probability": 0}}]},
    {"type": "llm", "key": "qwen3.5-0.8b", "params_string": "0.8B", "size_bytes": 800_000_000, "max_context_length": 32768,
     "loaded_instances": []},
    {"type": "embeddings", "key": "nomic-embed", "size_bytes": 100, "loaded_instances": []},
]}
OPENAI = {"data": [{"id": "qwen3.5-9b@q6_k"}, {"id": "qwen3.5-0.8b"}, {"id": "nomic-embed"}]}


class _Session:
    """Scripted LM Studio: GETs answer from `state`, POSTs are recorded and mutate the loaded instances."""

    def __init__(self, native=None, up=True, load_status=200, load_error=None):
        self.native = native if native is not None else {"models": [dict(m) for m in NATIVE["models"]]}
        self.up, self.calls, self.load_status, self.load_error = up, [], load_status, load_error
        self.chat_status = 200

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if not self.up:
            raise Exception("connection refused")
        if url.endswith("/api/v1/models"):
            return _Resp(200, self.native)
        if url.endswith("/v1/models"):
            return _Resp(200, OPENAI)
        return _Resp(404, {})

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        if url.endswith("/v1/chat/completions"):
            if self.chat_status != 200:
                return _Resp(self.chat_status, {"error": {"message": "model failed to load", "type": "server_error"}})
            return _Resp(200, {"choices": [{"message": {"role": "assistant", "content": "Hi"}, "finish_reason": "length"}]})
        if url.endswith("/api/v1/models/unload"):
            for m in self.native["models"]:
                m["loaded_instances"] = [i for i in m.get("loaded_instances") or [] if i.get("id") != (json or {}).get("instance_id")]
            return _Resp(200, {"ok": True})
        if url.endswith("/api/v1/models/load"):
            if self.load_status != 200:
                return _Resp(self.load_status, {"error": {"message": self.load_error or "boom", "type": "invalid_request"}})
            key = (json or {}).get("model")
            target = next(m for m in self.native["models"] if m["key"] == key)
            cfg = {k: v for k, v in (json or {}).items() if k != "model"}
            eff = {"context_length": 32768, "parallel": 1, "flash_attention": True}
            eff.update(cfg)
            target["loaded_instances"] = [{"id": key, "config": eff}]
            return _Resp(200, {"instance_id": key})
        return _Resp(404, {})


def _ctx(tmp_path):
    cfg = types.SimpleNamespace(LMS_ENABLED=True, LMS_CMD="/usr/bin/lms", LMS_API_URL="http://x:1235",
                                AGENT_INSTALL_DIR=str(tmp_path), SPEED_BENCH_PYTHON="", MANAGER_URL="",
                                LMS_LOAD_TIMEOUT_S=5, LMS_UNLOAD_TIMEOUT_S=5, LLAMA_API_URL="", LLAMA_BIN="", LLAMA_ENABLED=False)
    return types.SimpleNamespace(config=cfg, check_bearer=lambda *a, **k: None, check_stream_auth=lambda *a, **k: None,
                                 state={"token": "", "agent_id": ""}, post_session=None)


@pytest.fixture
def env(mods, tmp_path, monkeypatch):
    llama, lms, tools = mods
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(lms, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(llama, "_require_ctx", lambda: ctx)
    monkeypatch.setattr(lms, "lms_get_ps", lambda: [{"identifier": "qwen3.5-9b@q6_k", "status": "IDLE"}])
    monkeypatch.setattr(lms, "reconcile_now", lambda: None)
    sess = _Session()
    monkeypatch.setattr(tools, "_session", lambda: sess)

    class _FakeShim:
        """The timings shim needs a real socket + requests; the routes only need its url."""
        def __init__(self, upstream):
            self.upstream, self.url = upstream, "http://127.0.0.1:1"
        mode = "streaming"
        def native_available(self): return False
        def start(self): return self
        def stop(self): return None
        def __enter__(self): return self
        def __exit__(self, *a): return None
    monkeypatch.setattr(tools._shim, "Shim", _FakeShim)
    monkeypatch.setattr(tools, "_free_mb", lambda: 12345)
    _real_sleep = time.sleep
    monkeypatch.setattr(tools.time, "sleep", lambda s: _real_sleep(min(float(s), 0.001)))   # yields to worker threads
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_autotune_active", False)
    return types.SimpleNamespace(llama=llama, lms=lms, tools=tools, sess=sess, ctx=ctx)


def test_bench_server_reads_loaded_instances_and_spec(env):
    s = env.tools._bench_server()
    assert s["up"] and s["provider"] == "lms" and s["loaded_id"] == "qwen3.5-9b@q6_k"
    assert {m["id"]: m["status"] for m in s["models"]} == {"qwen3.5-9b@q6_k": "loaded", "qwen3.5-0.8b": "unloaded", "nomic-embed": "unloaded"}
    assert s["slots_total"] == 2 and s["slots_idle"] == 2 and s["spec"] == {"n_min": 0, "n_max": 3, "p_min": 0}
    env.sess.up = False
    assert env.tools._bench_server()["up"] is False


def test_preflight_and_tools_state_shape(env, monkeypatch):
    monkeypatch.setattr(env.llama, "_bench_live_runtime", lambda: {"python": None, "script": None, "source": "missing", "script_status": "x", "commit": "c"})
    p = env.tools.lms_bench_live_preflight()
    assert p["ok"] and p["provider"] == "lms" and p["server"]["up"] and p["busy"] is False and "throughput_1k" in p["benches"]
    st = env.tools.lms_tools_state()
    assert st == {"ok": True, "bench_active": False, "autotune_active": False, "quality_active": False}
    a = env.tools.lms_autotune_preflight()
    assert a["ok"] and a["provider"] == "lms" and a["unit_active"] is False and a["runtime"]["ok"] is False
    keys = [m["key"] for m in a["models"]]
    assert keys == ["qwen3.5-9b@q6_k", "qwen3.5-0.8b", "nomic-embed"]
    assert a["models"][0]["loaded"] and a["models"][0]["config"]["parallel"] == 2
    assert a["drafts_for"] == {"qwen3.5-9b@q6_k": "qwen3.5-0.8b", "qwen3.5-0.8b": None}


def test_bench_run_gating_and_provider_tag(env, monkeypatch):
    t = env.tools
    with pytest.raises(_HTTPException):
        t.lms_bench_live_run({"bench": "qualitative"})
    monkeypatch.setattr(env.llama, "_autotune_active", True)
    assert "in progress" in t.lms_bench_live_run({"model_id": "qwen3.5-9b@q6_k"})["error"]
    monkeypatch.setattr(env.llama, "_autotune_active", False)
    assert "does not list" in t.lms_bench_live_run({"model_id": "other-model"})["error"]
    monkeypatch.setattr(env.llama, "_bench_live_runtime", lambda: {"python": None, "script": None})
    assert "runtime" in t.lms_bench_live_run({"model_id": "qwen3.5-9b@q6_k"})["error"]
    monkeypatch.setattr(env.llama, "_bench_live_runtime", lambda: {"python": "/p", "script": "/s"})
    seen = {}

    def fake_run_all(req, server, python, script, provider="llama"):
        seen.update(req=req, server=server, provider=provider)
        assert server["url"] == "http://127.0.0.1:1" and server["timings"] == "shim" and server["lms_url"].startswith("http://x")
        with env.llama._bench_lock:
            env.llama._bench_active = False
    monkeypatch.setattr(env.llama, "_bench_live_run_all", fake_run_all)
    out = t.lms_bench_live_run({"model_id": "qwen3.5-0.8b", "bench": "throughput_1k"})
    assert out["ok"] and out["run_id"]
    import time
    for _ in range(50):
        if seen:
            break
        time.sleep(0.01)
    assert seen["provider"] == "lms" and seen["req"]["model_id"] == "qwen3.5-0.8b" and seen["server"]["provider"] == "lms"
    # an unloaded model gets a note that LM Studio loads it on the first request
    events = [rec["event"] for rec in env.llama._bench_replay.records_after_seq(0)]
    assert any(e.get("type") == "line" and "not loaded" in e.get("text", "") for e in events)


def test_backend_current_load_and_restore(env, monkeypatch):
    monkeypatch.setattr(env.llama, "_bench_live_runtime", lambda: {"python": None, "script": None})
    be = env.tools._LmsBackend("qwen3.5-9b@q6_k", "run1")
    cur = be.current()
    assert cur["loaded"] and cur["config"]["parallel"] == 2 and cur["max_ctx"] == 262144 and cur["free_mb"] == 12345
    res = be.load({"context_length": 8192, "parallel": 4, "bogus": 1}, None)
    assert res["ok"] and res["ctx"] == 8192 and res["config"]["parallel"] == 4 and res["stick"] is None
    posts = [c for c in env.sess.calls if c[0] == "POST"]
    assert posts[0][1].endswith("/unload") and posts[0][2] == {"instance_id": "qwen3.5-9b@q6_k"}
    assert posts[1][2] == {"model": "qwen3.5-9b@q6_k", "context_length": 8192, "parallel": 4}
    # the warm-up request follows every load; free memory is the lowest reading while it ran
    assert posts[2][1].endswith("/v1/chat/completions") and posts[2][2]["model"] == "qwen3.5-9b@q6_k" and posts[2][2]["max_tokens"] == 32
    assert res["free_mb"] == 12345
    env.sess.chat_status = 500
    assert be.load({"context_length": 8192}, None) == {"ok": False, "error": "model failed to load"}
    env.sess.chat_status = 200
    # stick without a runtime reports the reason instead of crashing
    res2 = be.load({"context_length": 4096}, {"concurrency": 1, "limit": 4})
    assert res2["ok"] and res2["stick"]["ok"] is False and "runtime" in res2["stick"]["error"]
    # LM Studio's error message comes back verbatim
    env.sess.load_status, env.sess.load_error = 400, "Insufficient system resources"
    assert be.load({"context_length": 999999}, None) == {"ok": False, "error": "Insufficient system resources"}
    env.sess.load_status = 200
    # restore puts every snapshot instance back with its config; an empty snapshot leaves everything unloaded
    posts_of = lambda: [c for c in env.sess.calls if c[0] == "POST"]  # noqa: E731
    env.tools._restore([("qwen3.5-9b@q6_k", {"context_length": 32768, "parallel": 2})], "run1")
    loads = [c for c in posts_of() if c[1].endswith("/load")]
    assert loads[-1][2] == {"model": "qwen3.5-9b@q6_k", "context_length": 32768, "parallel": 2}
    env.tools._restore([], "run1")
    assert posts_of()[-1][1].endswith("/unload") and not env.tools._all_instances(env.sess.native["models"])
    missing = env.tools._LmsBackend("nope", "run1").current()
    assert missing["missing"] and not missing["loaded"]
    # a later model in a batch reads its original config from the run snapshot once everything is unloaded
    be2 = env.tools._LmsBackend("qwen3.5-9b@q6_k", "run1", orig={"context_length": 32768, "parallel": 2})
    cur2 = be2.current()
    assert cur2["loaded"] and cur2["config"] == {"context_length": 32768, "parallel": 2}


def test_unload_all_clears_every_model_and_snapshot_keeps_load_keys(env):
    env.sess.native["models"][1]["loaded_instances"] = [{"id": "qwen3.5-0.8b", "config": {"context_length": 4096, "ttl": 9}}]
    snap = dict(env.tools._snapshot())
    assert list(snap) == ["qwen3.5-9b@q6_k", "qwen3.5-0.8b"]
    assert snap["qwen3.5-0.8b"] == {"context_length": 4096}                      # ttl is not a load key
    assert snap["qwen3.5-9b@q6_k"]["speculative_draft_mtp"] is True and snap["qwen3.5-9b@q6_k"]["parallel"] == 2
    env.tools._LmsBackend("qwen3.5-9b@q6_k", "run1").unload_all()
    unloads = [c[2]["instance_id"] for c in env.sess.calls if c[0] == "POST" and c[1].endswith("/unload")]
    assert sorted(unloads) == ["qwen3.5-0.8b", "qwen3.5-9b@q6_k"] and not env.tools._all_instances(env.sess.native["models"])


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     4350.
Pages active:                                  49519.
Pages inactive:                                44427.
Pages speculative:                             16893.
Pages throttled:                                   0.
Pages wired down:                             762161.
Pages purgeable:                                1908.
"Translation faults":                     5289582413.
File-backed pages:                            123803.
Anonymous pages:                              111764.
Pages occupied by compressor:                  12682.
"""


def test_ram_free_matches_activity_monitor_on_macos_and_available_elsewhere(env):
    # top: "15G used (12G wired, 198M compressor), 329M unused" → spare = unused + cached files = (4350+16893+123803) pages
    assert env.tools._darwin_free_mb(VM_STAT) == (4350 + 16893 + 123803) * 16384 // (1024 * 1024) == 2266
    assert env.tools._darwin_free_mb("garbage") is None
    vm = types.SimpleNamespace(total=16 * 1024 ** 3, available=823 * 1024 ** 2, wired=11876 * 1024 ** 2)
    assert env.tools._ram_free_mb(vm, True, VM_STAT) == 2266
    assert env.tools._ram_free_mb(vm, True, None) == 16384 - 11876          # vm_stat unavailable → unwired
    assert env.tools._ram_free_mb(vm, False, VM_STAT) == 823
    assert env.tools._ram_free_mb(types.SimpleNamespace(total=1, available=2 * 1024 ** 2), True) == 2


def test_stick_samples_free_memory_through_the_traffic(env, monkeypatch, tmp_path):
    t = env.tools
    monkeypatch.setattr(env.llama, "_bench_live_runtime", lambda: {"python": "/p", "script": "/s"})
    readings = iter([2400, 1900, 2100, 2300])
    sampled = threading.Event()

    def free_mb():
        v = next(readings, 2300)
        if v == 2300:
            sampled.set()                       # the bench "runs" until the sampler has seen every reading
        return v
    monkeypatch.setattr(t, "_free_mb", free_mb)
    monkeypatch.setattr(t._bl, "build_cmd", lambda *a, **k: ["x"])

    def fake_run(cmd, benv, put, mid, level, cancel, track, untrack):
        sampled.wait(5)
        (tmp_path / "data" / "bench" / "runs" / "at-run1" / "stick-1-qwen3.5-9b_q6_k.json").write_text('{"ok": true}')
        return 0, False, 12.0
    monkeypatch.setattr(t._bl, "run_level_subprocess", fake_run)
    monkeypatch.setattr(t._bl, "level_summary", lambda payload, wall: {"all": {"pred_tps": 20.0, "prompt_tps": 200.0, "agg_pred_tps": 18.0}})
    be = t._LmsBackend("qwen3.5-9b@q6_k", "run1")
    st = be._stick("qwen3.5-9b@q6_k", {"concurrency": 1, "limit": 4})
    assert st["ok"] and st["decode_tps"] == 20.0 and st["free_mb"] == 1900
    # load() keeps the lower of the warm-up and the traffic reading
    monkeypatch.setattr(t, "_free_mb", lambda: 2500)
    monkeypatch.setattr(be, "_stick", lambda iid, m: {"ok": True, "decode_tps": 20.0, "free_mb": 1700})
    res = be.load({"context_length": 8192}, {"concurrency": 1, "limit": 4})
    assert res["free_mb"] == 1700


def test_free_mb_under_takes_the_lowest_reading_while_the_request_runs(env):
    readings = iter([7300, 7200, 6650, 6700, 7250, 7300])
    ticks = {"n": 0}

    def work():
        # three sampler ticks while the "request" runs
        while ticks["n"] < 3:
            pass
    def tick(s):
        ticks["n"] += 1
        time.sleep(0.001)
    low = env.tools._free_mb_under(work, sample=lambda: next(readings, 7300), sleep=tick, every=0)
    assert low == 6650                                  # four samples: the lowest still wins
    assert env.tools._free_mb_under(lambda: None, sample=lambda: None, sleep=lambda s: None) is None
    # a single one-second dip across a long window is ignored: the 10th percentile, not the minimum
    dips = iter([2000] * 10 + [900] + [2000] * 9)
    n = {"n": 0}

    def long_work():
        while n["n"] < 19:
            pass
    def tick2(s):
        n["n"] += 1
        time.sleep(0.001)
    assert env.tools._free_mb_under(long_work, sample=lambda: next(dips, 2000), sleep=tick2, every=0) == 2000
def test_autotune_run_gating(env, monkeypatch):
    t = env.tools
    with pytest.raises(_HTTPException):
        t.lms_autotune_run({"model_ids": []})
    env.sess.up = False
    assert "not running" in t.lms_autotune_run({"model_ids": ["qwen3.5-9b@q6_k"]})["error"]
    env.sess.up = True
    monkeypatch.setattr(env.llama, "_bench_active", True)
    assert "in progress" in t.lms_autotune_run({"model_ids": ["qwen3.5-9b@q6_k"]})["error"]
    monkeypatch.setattr(env.llama, "_bench_active", False)
    started = {}

    def fake_run_all(req):
        started["req"] = req
        with env.llama._autotune_lock:
            env.llama._autotune_active = False
    monkeypatch.setattr(t, "_autotune_run_all", fake_run_all)
    out = t.lms_autotune_run({"model_ids": ["qwen3.5-9b@q6_k"], "objective": "speed"})
    assert out["ok"] and out["run_id"] == env.llama._autotune_run_id
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.01)
    assert started["req"]["objective"] == "speed" and started["req"]["dims"]["flash"]["on"] is True
    assert t.lms_autotune_cancel()["ok"]
    assert t.lms_bench_cancel()["ok"]


def test_run_all_restores_and_posts_ledger(env, monkeypatch):
    t = env.tools
    monkeypatch.setattr(env.llama, "_bench_live_runtime", lambda: {"python": None, "script": None})
    posted = []
    monkeypatch.setattr(t._shared, "post_tool_run", lambda *a, **k: posted.append(a))
    monkeypatch.setattr(env.llama, "_autotune_run_id", "rid1")
    monkeypatch.setattr(env.llama, "_autotune_active", True)
    req = t._lat.validate_request({"model_ids": ["qwen3.5-9b@q6_k", "ghost"], "objective": "fit",
                                   "dims": {"context": {"target_mb": 1000, "tolerance_mb": 100}}})
    with env.llama._autotune_cond:
        env.llama._autotune_replay.start_run("rid1")
    t._autotune_run_all(req)
    events = [rec["event"] for rec in env.llama._autotune_replay.records_after_seq(0)]
    dones = [e for e in events if e.get("type") == "model_done"]
    assert [d["model_id"] for d in dones] == ["qwen3.5-9b@q6_k", "ghost"]
    assert dones[0]["ok"] and dones[0]["provider"] == "lms" and dones[1]["stop_reason"].startswith("LM Studio does not list")
    assert events[-1]["type"] == "done" and events[-1]["ok"]
    assert posted and posted[0][1:3] == ("autotune", "lms")
    # the last load put the original instance config back
    last_load = [c for c in env.sess.calls if c[0] == "POST" and c[1].endswith("/load")][-1]
    assert last_load[2]["parallel"] == 2 and last_load[2]["context_length"] == 32768
    assert env.llama._autotune_active is False
