# agent/tests/_vllm_load.py
"""Stub heavy deps when absent and load providers/vllm.py standalone
(same idiom as test_llama_svcconfig_unit_mismatch.py)."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

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

_pkg = types.ModuleType("providers")
_pkg.__path__ = [str(_AGENT_ROOT / "providers")]
sys.modules.setdefault("providers", _pkg)


def load_vllm():
    if "providers.vllm" in sys.modules:
        return sys.modules["providers.vllm"]
    spec = importlib.util.spec_from_file_location(
        "providers.vllm", _AGENT_ROOT / "providers" / "vllm.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["providers.vllm"] = mod
    spec.loader.exec_module(mod)
    return mod
