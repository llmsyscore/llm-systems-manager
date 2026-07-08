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


# The agent test venv has no requests/fastapi (see conftest.py) — stub the
# third-party imports so the full llama provider module can load.
if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        _stub_module("requests", get=None, Session=object)
try:
    import fastapi  # noqa: F401
except ImportError:
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

try:
    import psutil  # noqa: F401
except ImportError:
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
            return _Resp(props_payload)
        raise ConnectionError(f"unexpected fetch: {url}")
    return _fake_get


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    cfg = SimpleNamespace(
        LLAMA_ENABLED=True,
        POLL_INTERVAL_S=2.0,
        LLAMA_API_URL="http://127.0.0.1:9999",
        LLAMA_STATE_FILE=str(tmp_path / "llama-state"),
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
    monkeypatch.setattr(llama, "_llama_info_last_loaded_model", None)
    monkeypatch.setattr(llama, "_llama_api_probe_cache", {"ts": 0, "result": "unknown"})
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
