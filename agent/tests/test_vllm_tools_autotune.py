"""#894 PR 2: vLLM autotune routes on llama's shared autotune state; the backend edits ExecStart,
restarts, reads the journal, waits for /v1/models and sticks through the shim; rollback unless applied."""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from tests._vllm_tools_load import load

TOOLS_PY = Path(__file__).resolve().parents[1] / "providers" / "vllm_tools.py"
UNIT = "[Service]\nExecStart=/opt/vllm/venv/bin/vllm serve org/model-8b --host 127.0.0.1 --port 8000 --max-model-len 8192 --max-num-seqs 4\n"
UNIT_NO_LEN = "[Service]\nExecStart=/opt/vllm/venv/bin/vllm serve org/model-8b --host 127.0.0.1 --port 8000 --max-num-seqs 4\n"


@pytest.fixture(scope="module")
def mods():
    llama, vllm, tools, restore = load()
    yield llama, vllm, tools
    restore()


class _Cfg:
    VLLM_ENABLED = True; VLLM_API_URL = "http://localhost:8000"; VLLM_SYSTEMD_UNIT = "vllm.service"
    VLLM_BENCH_BIN = ""; AGENT_INSTALL_DIR = ""; SPEED_BENCH_PYTHON = ""; MANAGER_URL = "http://mgr:5000"


class _Sess:
    """GET fakes for /v1/models, /version, /metrics."""
    class _R:
        def __init__(self, ok, payload=None, text=""):
            self.ok, self._p, self.text = ok, payload, text
        def json(self):
            return self._p

    def get(self, url, timeout=None):
        if url.endswith("/v1/models"):
            return self._R(True, {"data": [{"id": "org/model-8b", "max_model_len": 8192}]})
        if url.endswith("/version"):
            return self._R(True, {"version": "0.30.0"})
        if url.endswith("/metrics"):
            return self._R(True, text="vllm:num_requests_running{model=\"m\"} 0\n")
        return self._R(False)


class _Ctx:
    def __init__(self, tmp):
        self.config = _Cfg(); self.config.AGENT_INSTALL_DIR = str(tmp); self.state = {"token": "tok", "agent_id": "a1"}
        self.posts = []
        outer = self

        class _Post:
            def post(self, url, json=None, timeout=None, headers=None):
                outer.posts.append({"url": url, "json": json})
        self.post_session = _Post()
    def check_bearer(self, a): return None
    def check_stream_auth(self, *a): return None


@pytest.fixture
def ctx(mods, monkeypatch, tmp_path):
    llama, vllm, tools = mods
    c = _Ctx(tmp_path)
    for m in (llama, vllm): m.set_context(c)
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(llama, "_autotune_active", False)
    monkeypatch.setattr(llama, "_autotune_quality", False)
    monkeypatch.setattr(llama, "_autotune_proc", None); monkeypatch.setattr(llama, "_autotune_pgid", None)
    monkeypatch.setattr(llama, "_autotune_aux_proc", None); monkeypatch.setattr(llama, "_autotune_aux_pgid", None)
    llama._autotune_cancel_event.clear()
    monkeypatch.setattr(vllm, "_get_session", _Sess)
    monkeypatch.setattr(vllm.Path, "read_text", lambda self, *a, **k: UNIT)
    monkeypatch.setattr(llama, "_bench_live_runtime", lambda: {"python": "/p", "script": "/s", "script_status": "ok", "source": "x", "commit": "c"})
    monkeypatch.setattr(llama, "_ram_total_mb", lambda: 8192)
    monkeypatch.setattr(tools, "_unit_active", lambda: True)
    return c


def _fn_src(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |^class |^# ──|^_ROUTES)", TOOLS_PY.read_text(), re.M | re.S)
    assert m, name
    return m.group(0)


# ── preflight / state ──

def test_preflight_reports_served_model_and_serve_flags(mods, ctx):
    tools = mods[2]
    p = tools.vllm_autotune_preflight()
    assert p["ok"] and p["provider"] == "vllm" and p["busy"] is False and p["unit_active"] is True
    assert p["server"]["up"] and p["models"] == [{"key": "org/model-8b", "loaded": True, "type": "llm"}]
    assert p["config"] == {"max_model_len": 8192, "max_num_seqs": 4}
    assert p["runtime"]["ok"] is True and p["drafts_for"] == {} and p["ram_total_mb"] == 8192


def test_tools_state_reports_the_shared_autotune_flag(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    monkeypatch.setattr(llama, "_autotune_active", True)
    st = tools.vllm_tools_state()
    assert st == {"ok": True, "bench_active": False, "autotune_active": True, "quality_active": False}
    assert tools._busy() is True and tools.vllm_autotune_preflight()["busy"] is True


# ── run gating ──

def test_run_validates_and_refuses(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        tools.vllm_autotune_run({"model_ids": [], "objective": "fit"})
    assert ei.value.status_code == 400
    assert "does not serve org/other" in tools.vllm_autotune_run({"model_ids": ["org/other"]})["error"]
    monkeypatch.setattr(vllm.Path, "read_text", lambda self, *a, **k: "[Service]\nnope\n")
    assert "ExecStart" in tools.vllm_autotune_run({"model_ids": ["org/model-8b"]})["error"]
    monkeypatch.setattr(vllm.Path, "read_text", lambda self, *a, **k: UNIT)
    monkeypatch.setattr(llama, "_bench_active", True)
    assert "in progress" in tools.vllm_autotune_run({"model_ids": ["org/model-8b"]})["error"]
    monkeypatch.setattr(llama, "_bench_active", False)
    monkeypatch.setattr(tools, "_served_ids", lambda: [])
    assert "not running" in tools.vllm_autotune_run({"model_ids": ["org/model-8b"]})["error"]
    assert "not running" in tools.vllm_autotune_run({"probe_len": 4096})["error"]


def test_run_starts_the_thread_with_execstart_and_the_validated_request(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    seen = {}

    def fake_run_all(req, head, args):
        seen.update(req=req, head=head, args=args)
        with llama._autotune_lock:
            llama._autotune_active = False
    monkeypatch.setattr(tools, "_autotune_run_all", fake_run_all)
    out = tools.vllm_autotune_run({"model_ids": ["org/model-8b"], "objective": "serve"})
    assert out["ok"] and out["run_id"] == llama._autotune_run_id
    for _ in range(50):
        if seen: break
        threading.Event().wait(0.02)
    assert seen["head"] == ["/opt/vllm/venv/bin/vllm", "serve", "org/model-8b"]
    assert [a["flag"] for a in seen["args"]] == ["--host", "--port", "--max-model-len", "--max-num-seqs"]
    assert seen["req"]["objective"] == "serve" and seen["req"]["dims"]["seqs"]["candidates"] == [8, 16, 32]


def test_run_accepts_legacy_body(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    seen = {}

    def fake_run_all(req, head, args):
        seen.update(req=req)
        with llama._autotune_lock:
            llama._autotune_active = False
    monkeypatch.setattr(tools, "_autotune_run_all", fake_run_all)
    assert tools.vllm_autotune_run({"probe_len": 2048, "concurrency": 2, "kv_fraction": 0.5, "report_only": True})["ok"]
    for _ in range(50):
        if seen: break
        threading.Event().wait(0.02)
    req = seen["req"]
    assert req["model_ids"] == ["org/model-8b"] and req["objective"] == "fit" and req["apply"] is False
    assert req["dims"]["context"] == {"on": True, "probe_len": 2048, "concurrency": 2.0, "kv_fraction": 0.5}
    assert not any(req["dims"][k]["on"] for k in ("seqs", "kvdtype", "spec", "prefix"))


def test_run_clears_a_stale_cancel_under_the_lock(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    seen = {}

    def fake_run_all(req, head, args):
        seen["cancel"] = llama._autotune_cancel_event.is_set()
        with llama._autotune_lock:
            llama._autotune_active = False
    monkeypatch.setattr(tools, "_autotune_run_all", fake_run_all)
    llama._autotune_cancel_event.set()
    assert tools.vllm_autotune_run({"model_ids": ["org/model-8b"]})["ok"]
    tools._autotune_thread.join(2)
    assert seen == {"cancel": False}


def test_shutdown_waits_for_the_autotune_thread(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    release = threading.Event()

    def fake_run_all(req, head, args):
        release.wait(5)
        with llama._autotune_lock:
            llama._autotune_active = False
    monkeypatch.setattr(tools, "_autotune_run_all", fake_run_all)
    assert tools.vllm_autotune_run({"model_ids": ["org/model-8b"]})["ok"]
    assert tools._autotune_thread.is_alive()
    release.set()
    tools.shutdown_children()
    assert not tools._autotune_thread.is_alive() and llama._autotune_active is False


def test_run_opens_the_replay_under_the_lock_before_the_thread():
    src = _fn_src("vllm_autotune_run")
    assert src.index("start_run(") < src.index("threading.Thread(")
    assert "_ll._autotune_lock" in src and "_at_job" not in TOOLS_PY.read_text()


def test_stream_and_cancel_use_the_shared_state(mods, ctx, monkeypatch):
    llama, _vllm, tools = mods
    seen = {}
    monkeypatch.setattr(tools._shared, "bench_replay_sse", lambda replay, cond, active, last: seen.update(replay=replay, cond=cond) or "sse")
    assert tools.vllm_autotune_stream(None, None, None) == "sse"
    assert seen["replay"] is llama._autotune_replay and seen["cond"] is llama._autotune_cond
    monkeypatch.setattr(llama, "_autotune_cancel_impl", lambda strays: {"ok": True, "strays": strays})
    assert tools.vllm_autotune_cancel() == {"ok": True, "strays": []}


# ── backend ──

_ARGS = [{"flag": "--host", "value": "127.0.0.1", "bool": False}, {"flag": "--max-model-len", "value": "8192", "bool": False}]


def _backend(tools, head_model="org/model-8b", args=None, **kw):
    head = ["/opt/vllm/venv/bin/vllm", "serve", head_model]
    args = _ARGS if args is None else args
    return tools._VllmBackend("org/model-8b", "run1", "vllm.service", head, args, "http://127.0.0.1:1", 600, **kw)


def test_backend_current_reads_execstart(mods, ctx, monkeypatch):
    vllm, tools = mods[1], mods[2]
    be = _backend(tools)
    assert be.current() == {"loaded": True, "served": "org/model-8b", "config": {"max_model_len": 8192}, "max_ctx": None}
    assert be.drafts() == []
    assert _backend(tools, head_model="/srv/models/model-8b").current()["served"] == "org/model-8b"
    _head, no_len = vllm._parse_vllm_execstart(UNIT_NO_LEN)
    assert _backend(tools, args=no_len).current()["max_ctx"] == 8192
    _head, with_len = vllm._parse_vllm_execstart(UNIT)
    assert _backend(tools, args=with_len).current()["max_ctx"] is None
    monkeypatch.setattr(tools, "_served_ids", lambda: [])
    assert _backend(tools, head_model="/srv/models/model-8b").current()["served"] == "/srv/models/model-8b"


def test_backend_load_writes_args_restarts_and_sticks(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    writes, sticks = [], []
    monkeypatch.setattr(vllm, "_svcconfig_write", lambda head, args, restart=False, **kw: (writes.append((head, args, restart)) or {"ok": True}))
    monkeypatch.setattr(tools, "_at_restart_and_watch", lambda unit, timeout_s, step: {"outcome": "kv", "kv_tokens": 230528, "max_conc": 3.5})
    monkeypatch.setattr(tools, "_at_wait_ready", lambda timeout_s=60.0: {"id": "org/model-8b", "max_model_len": 4096})
    monkeypatch.setattr(tools._lt, "stick_run", lambda url, mid, rid, n, measure, label=None: (sticks.append((url, mid, rid, n, measure)) or {"ok": True, "decode_tps": 23.4, "seconds": 20.0}))
    be = _backend(tools)
    res = be.load({"max_model_len": 4096, "kv_cache_dtype": "fp8", "enable_prefix_caching": False}, {"concurrency": 2, "limit": 4})
    assert res["ok"] and res["ctx"] == 4096 and res["kv_tokens"] == 230528 and res["stick"]["decode_tps"] == 23.4
    assert isinstance(res["load_s"], float)
    head, args, restart = writes[0]
    assert head == ["/opt/vllm/venv/bin/vllm", "serve", "org/model-8b"] and restart is False
    assert {"flag": "--max-model-len", "value": "4096", "bool": False} in args
    assert {"flag": "--kv-cache-dtype", "value": "fp8", "bool": False} in args
    assert {"flag": "--no-enable-prefix-caching", "value": None, "bool": True} in args
    assert sticks == [("http://127.0.0.1:1", "org/model-8b", "run1", 1, {"concurrency": 2, "limit": 4})]
    lines = [r["event"]["text"] for r in llama._autotune_replay.records_after_seq(0) if r["event"].get("type") == "line"]
    assert any("[autotune] restart with" in t and "--kv-cache-dtype fp8" in t for t in lines)


def test_backend_load_reports_rejected_on_fatal_journal(mods, ctx, monkeypatch):
    _llama, vllm, tools = mods
    monkeypatch.setattr(vllm, "_svcconfig_write", lambda head, args, restart=False, **kw: {"ok": True})
    monkeypatch.setattr(tools, "_at_restart_and_watch", lambda unit, timeout_s, step: {"outcome": "fatal", "fatal_line": "ValueError: fp8 is not supported on CPU"})
    res = _backend(tools).load({"kv_cache_dtype": "fp8"}, None)
    assert res == {"ok": False, "rejected": True, "error": "ValueError: fp8 is not supported on CPU"}
    monkeypatch.setattr(tools, "_at_restart_and_watch", lambda unit, timeout_s, step: {"outcome": "timeout"})
    assert "no KV-capacity answer" in _backend(tools).load({}, None)["error"]
    monkeypatch.setattr(tools, "_at_restart_and_watch", lambda unit, timeout_s, step: {"outcome": "est_max", "est_max_len": 56736})
    res = _backend(tools).load({}, None)
    assert res["rejected"] and "56,736" in res["error"]
    monkeypatch.setattr(tools, "_at_restart_and_watch", lambda unit, timeout_s, step: {"outcome": "kv", "kv_tokens": 1})
    monkeypatch.setattr(tools, "_at_wait_ready", lambda timeout_s=60.0: None)
    res = _backend(tools).load({}, None)
    assert res["rejected"] and "/v1/models" in res["error"]
    monkeypatch.setattr(vllm, "_svcconfig_write", lambda head, args, restart=False, **kw: {"ok": False, "error": "sudo refused"})
    assert _backend(tools).load({}, None) == {"ok": False, "error": "sudo refused"}


def test_backend_load_cancelled(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    writes = []
    monkeypatch.setattr(vllm, "_svcconfig_write", lambda head, args, restart=False, **kw: (writes.append(1) or {"ok": True}))
    llama._autotune_cancel_event.set()
    assert _backend(tools).load({}, None) == {"ok": False, "error": "cancelled"} and writes == []
    llama._autotune_cancel_event.clear()
    monkeypatch.setattr(tools, "_at_restart_and_watch", lambda unit, timeout_s, step: {"outcome": "cancelled"})
    assert _backend(tools).load({}, None) == {"ok": False, "error": "cancelled"}


# ── run_all: rollback / applied / ledger ──

class _FakeShim:
    def __init__(self, upstream, native=None, label=""):
        self.url, self.stopped = "http://127.0.0.1:1", False
    def __enter__(self): return self
    def __exit__(self, *a): self.stopped = True


def _wire_run_all(mods, monkeypatch, done_doc):
    llama, vllm, tools = mods
    writes = []
    monkeypatch.setattr(tools._shim, "Shim", _FakeShim)
    monkeypatch.setattr(vllm, "_svcconfig_write", lambda head, args, restart=False, **kw: (writes.append((args, restart)) or {"ok": True}))
    monkeypatch.setattr(tools, "_at_restart_and_watch", lambda unit, timeout_s, step: {"outcome": "kv", "kv_tokens": 1})
    monkeypatch.setattr(tools, "_at_wait_ready", lambda timeout_s=60.0: {"id": "org/model-8b"})

    def fake_run_model(mid, req, backend, put, cancelled, env):
        put({"type": "line", "text": "tuning"})
        if done_doc.get("_load") is not None:
            backend.load(done_doc["_load"], None)
        if done_doc.get("_cancel"):
            llama._autotune_cancel_event.set()
        doc = dict(done_doc, model_id=mid, run_id=env["run_id"]); doc.pop("_cancel", None); doc.pop("_load", None)
        put(doc)
        return doc
    monkeypatch.setattr(tools._vat, "run_model", fake_run_model)
    llama._autotune_cancel_event.clear()
    with llama._autotune_lock:
        llama._autotune_active = True
        llama._autotune_run_id = "rid1"
        with llama._autotune_cond:
            llama._autotune_replay.start_run("rid1")
    req = tools._vat.validate_request({"model_ids": ["org/model-8b"]})
    orig = [{"flag": "--max-model-len", "value": "8192", "bool": False}]
    tools._autotune_run_all(req, ["/opt/vllm/venv/bin/vllm", "serve", "org/model-8b"], orig)
    events = [r["event"] for r in llama._autotune_replay.records_after_seq(0)]
    return writes, events, orig


def test_run_all_restores_execstart_on_cancel(mods, ctx, monkeypatch):
    llama = mods[0]
    writes, events, orig = _wire_run_all(mods, monkeypatch, {"type": "model_done", "ok": False, "cancelled": True, "applied": False, "changes": [], "stages": [],
                                                              "_cancel": True, "_load": {"max_model_len": 4096}})
    assert len(writes) == 2 and writes[0][1] is False and writes[-1] == (orig, True)
    assert events[-1] == {"type": "done", "ok": False, "cancelled": True, "count": 1}
    assert llama._autotune_active is False and llama._autotune_cancel_event.is_set()
    assert any(e.get("type") == "line" and "restoring" in e.get("text", "") for e in events)


def test_run_all_keeps_an_applied_set_and_posts_the_ledger_row(mods, ctx, monkeypatch):
    writes, events, _orig = _wire_run_all(mods, monkeypatch, {"type": "model_done", "ok": True, "cancelled": False, "applied": True, "apply": True,
                                                              "changes": [{"key": "max_model_len", "recommended": "230400"}], "stages": [],
                                                              "after": {"ctx": 230400, "decode_tps": 20.0}, "kv_tokens": 230528, "verify": {"ok": True}})
    assert writes == []
    assert events[-1] == {"type": "done", "ok": True, "cancelled": False, "count": 1}
    body = ctx.posts[0]["json"]
    assert body["tool"] == "autotune" and body["provider"] == "vllm" and body["run_id"] == "rid1" and body["model_id"] == "org/model-8b"
    assert body["applied"] is True and body["kv_tokens"] == 230528 and body["max_model_len"] == 230400 and body["switches"] == {"max_model_len": "230400"}


def test_run_all_restores_after_a_normal_run(mods, ctx, monkeypatch):
    writes, events, orig = _wire_run_all(mods, monkeypatch, {"type": "model_done", "ok": True, "cancelled": False, "applied": False, "changes": [], "stages": [],
                                                              "_load": {"max_model_len": 4096}})
    assert len(writes) == 2 and writes[-1] == (orig, True) and events[-1]["ok"] is True


def test_run_all_skips_the_restore_when_the_unit_holds_the_original_flags(mods, ctx, monkeypatch):
    writes, events, orig = _wire_run_all(mods, monkeypatch, {"type": "model_done", "ok": True, "cancelled": False, "applied": False, "changes": [], "stages": [],
                                                              "_load": {"max_model_len": 8192}})
    assert writes == [(orig, False)] and events[-1]["ok"] is True
    assert not any(e.get("type") == "line" and "restoring" in e.get("text", "") for e in events)


def test_restore_and_restart_use_the_long_restart_timeout(mods, ctx, monkeypatch):
    llama, vllm, tools = mods
    seen = {}
    monkeypatch.setattr(vllm, "_svcconfig_write", lambda head, args, restart=False, **kw: (seen.update(restart=restart, **kw) or {"ok": True}))
    tools._restore(["/opt/vllm/venv/bin/vllm", "serve", "org/model-8b"], [])
    assert seen == {"restart": True, "restart_timeout": tools.RESTART_TIMEOUT_S} and tools.RESTART_TIMEOUT_S >= 180
    calls = []
    monkeypatch.setattr(vllm, "_vllm_systemctl", lambda action, timeout=30: (calls.append((action, timeout)) or {"ok": False, "error": "x"}))
    monkeypatch.setattr(tools, "_at_watch_journal", lambda *a, **k: {"outcome": "timeout"})
    tools._at_restart_and_watch("vllm.service", 5, "load")
    assert calls == [("restart", tools.RESTART_TIMEOUT_S)]
