"""collect_llama_for_metrics() /props surfacing (#137)."""
from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))


def _stub_module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)
    return sys.modules[name]


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# The agent test venv has no requests/fastapi (see conftest.py) — stub the
# third-party imports so the full llama provider module can load.
if not _module_available("requests"):
    _stub_module("requests", get=None, Session=object)
if not _module_available("fastapi"):
    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail)
            self.status_code = status_code
    _stub_module(
        "fastapi",
        Header=lambda *a, **k: None, HTTPException=_HTTPException,
        Query=lambda *a, **k: None, Request=object,
    )
    _stub_module("fastapi.responses", Response=object, StreamingResponse=object)
    _stub_module("starlette")
    _stub_module("starlette.concurrency", run_in_threadpool=lambda f, *a, **k: f(*a, **k))

if not _module_available("psutil"):
    # collectors/__init__ pulls system.py (psutil); stub the whole package.
    _collectors = _stub_module("collectors")
    _collectors.__path__ = []
    _stub_module("collectors.gpu", collect_gpu=lambda: {})

_pkg = types.ModuleType("providers")
_pkg.__path__ = [str(_AGENT_ROOT / "providers")]
sys.modules.setdefault("providers", _pkg)

_spec = importlib.util.spec_from_file_location(
    "providers.llama", _AGENT_ROOT / "providers" / "llama.py")
llama = importlib.util.module_from_spec(_spec)
sys.modules["providers.llama"] = llama
_spec.loader.exec_module(llama)


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.text = ""

    def json(self):
        return self._payload


def _fake_get_factory(props_payload):
    def _fake_get(url, **kwargs):
        if url.endswith("/v1/models"):
            return _Resp({"data": [{"id": "test-model", "status": {"value": "loaded"}}]})
        if url.endswith("/props"):
            # Router mode: per-model props only when ?model= is supplied.
            if (kwargs.get("params") or {}).get("model") != "test-model":
                return _Resp({"role": "router", "build_info": "b1-test"})
            return _Resp(props_payload)
        raise ConnectionError(f"unexpected fetch: {url}")
    return _fake_get


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    cfg = SimpleNamespace(
        LLAMA_ENABLED=True,
        POLL_INTERVAL_S=2.0,
        LLAMA_API_URL="http://127.0.0.1:9999",
        LLAMA_BUILD_METHOD="custom_script",
    )
    context = SimpleNamespace(
        config=cfg,
        state={},
        probe_http=lambda url, timeout=1.5: (True, "ok"),
        runtime_lock=threading.RLock(),
    )
    llama.set_context(context)
    monkeypatch.setattr(llama, "_llama_info_last_poll", 0.0)
    monkeypatch.setattr(llama, "_llama_info_cache", {})
    monkeypatch.setattr(llama, "_llama_loaded", {"last": None})
    monkeypatch.setattr(llama, "collect_gpu", lambda: {}, raising=False)
    return context


def test_props_fields_surface(ctx, monkeypatch):
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({
        "chat_template": "{{ bos }}" * 10,
        "modalities": {"vision": True, "audio": False},
        "total_slots": 4,
        "is_sleeping": False,
    })))
    out = llama.collect_llama_for_metrics()
    assert out["model"] == "test-model"
    assert out["chat_template"] == "{{ bos }}" * 10
    assert out["chat_template_len"] == len("{{ bos }}") * 10
    assert out["modalities"] == {"vision": True, "audio": False}
    assert out["total_slots"] == 4
    assert out["is_sleeping"] is False


def test_props_chat_template_truncated(ctx, monkeypatch):
    big = "x" * (llama._LLAMA_CHAT_TEMPLATE_MAX_CHARS + 500)
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({
        "chat_template": big,
        "total_slots": 2,
    })))
    out = llama.collect_llama_for_metrics()
    assert len(out["chat_template"]) == llama._LLAMA_CHAT_TEMPLATE_MAX_CHARS
    assert out["chat_template_len"] == len(big)


def test_props_failure_leaves_fields_none(ctx, monkeypatch):
    def _get(url, **kwargs):
        if url.endswith("/v1/models"):
            return _Resp({"data": [{"id": "test-model", "status": {"value": "loaded"}}]})
        raise ConnectionError("props down")
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_get))
    out = llama.collect_llama_for_metrics()
    assert out["chat_template"] is None
    assert out["modalities"] is None
    assert out["total_slots"] is None
    assert out["is_sleeping"] is None


def test_props_is_sleeping_sets_canonical_sleeping_flag(ctx, monkeypatch):
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({
        "is_sleeping": True,
        "total_slots": 1,
    })))
    out = llama.collect_llama_for_metrics()
    assert out["is_sleeping"] is True
    assert out["sleeping"] is True


def test_props_skipped_when_no_model_loaded(ctx, monkeypatch):
    calls = []

    def _get(url, **kwargs):
        calls.append(url)
        if url.endswith("/v1/models"):
            return _Resp({"data": []})
        raise ConnectionError(f"should not fetch {url}")

    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_get))
    out = llama.collect_llama_for_metrics()
    assert not any(u.endswith("/props") for u in calls)
    assert out["total_slots"] is None


def test_props_ignores_malformed_values(ctx, monkeypatch):
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({
        "chat_template": 42,
        "modalities": "vision",
        "total_slots": "four",
        "is_sleeping": "no",
    })))
    out = llama.collect_llama_for_metrics()
    assert out["chat_template"] is None
    assert out["modalities"] is None
    assert out["total_slots"] is None
    assert out["is_sleeping"] is None


def test_props_build_info_surfaces(ctx, monkeypatch):
    monkeypatch.setattr(llama, "_llama_build_last", "")
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({
        "total_slots": 2, "build_info": "b10809-5266f24da",
    })))
    out = llama.collect_llama_for_metrics()
    assert out["build"] == "b10809-5266f24da"
    assert llama._llama_build_last == "b10809-5266f24da"


def test_props_build_cached_when_probe_skipped(ctx, monkeypatch):
    monkeypatch.setattr(llama, "_llama_build_last", "b10809-5266f24da")
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({
        "total_slots": 2,
    })))
    out = llama.collect_llama_for_metrics()
    assert out["build"] == "b10809-5266f24da"


def test_props_build_info_truncated_and_non_string_ignored(ctx, monkeypatch):
    monkeypatch.setattr(llama, "_llama_build_last", "")
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({
        "total_slots": 2, "build_info": "x" * 200,
    })))
    assert len(llama.collect_llama_for_metrics()["build"]) == 64
    monkeypatch.setattr(llama, "_llama_build_last", "")
    monkeypatch.setattr(llama, "_llama_info_last_poll", 0.0)
    monkeypatch.setattr(llama, "_llama_info_cache", {})
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({
        "total_slots": 2, "build_info": 12345,
    })))
    assert "build" not in llama.collect_llama_for_metrics()


# --- #1039: the llama block carries the server command line for the manager's --jinja check ---

class _Proc:
    def __init__(self, cmd, name="llama-server"):
        self._cmd, self.info = cmd, {"name": name, "cmdline": cmd}

    def cmdline(self):
        return list(self._cmd)


def _fake_psutil(monkeypatch, by_pid=None, running=()):
    calls = []
    def _process(pid):
        calls.append(pid)
        return _Proc((by_pid or {})[pid])
    mod = types.ModuleType("psutil")
    mod.Process = _process
    mod.process_iter = lambda attrs=None: list(running)
    monkeypatch.setitem(sys.modules, "psutil", mod)
    return calls


def test_server_cmdline_reads_the_unit_pid_once_per_pid(monkeypatch):
    monkeypatch.setattr(llama, "_llama_server_args", {"pid": None, "args": None})
    calls = _fake_psutil(monkeypatch, by_pid={4242: ["llama-server", "--jinja", "-m", "x.gguf"]})
    assert llama._llama_server_cmdline(4242) == "llama-server --jinja -m x.gguf"
    assert llama._llama_server_cmdline(4242) == "llama-server --jinja -m x.gguf"
    assert calls == [4242]


def test_server_cmdline_falls_back_to_a_named_process_without_a_unit_pid(monkeypatch):
    monkeypatch.setattr(llama, "_llama_server_args", {"pid": None, "args": None})
    _fake_psutil(monkeypatch, running=[_Proc(["python3", "agent.py"], name="python3"),
                                       _Proc(["/opt/llama/llama-server", "--port", "8080"])])
    assert llama._llama_server_cmdline(None) == "/opt/llama/llama-server --port 8080"


def test_server_cmdline_is_none_without_psutil_or_process(monkeypatch):
    monkeypatch.setattr(llama, "_llama_server_args", {"pid": None, "args": None})
    _fake_psutil(monkeypatch, running=[])
    assert llama._llama_server_cmdline(None) is None
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(llama, "_llama_server_args", {"pid": None, "args": None})
    assert llama._llama_server_cmdline(7) is None


def test_metrics_block_carries_server_args(ctx, monkeypatch):
    monkeypatch.setattr(llama, "_llama_unit_main_pid", lambda: 4242)
    monkeypatch.setattr(llama, "_llama_server_args", {"pid": None, "args": None})
    _fake_psutil(monkeypatch, by_pid={4242: ["llama-server", "--jinja"]})
    monkeypatch.setattr(llama, "requests", SimpleNamespace(get=_fake_get_factory({"total_slots": 1})))
    out = llama.collect_llama_for_metrics()
    assert out["server_args"] == "llama-server --jinja"
