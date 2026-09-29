"""Hermetic loader for providers/vllm_tools.py: stubs third-party imports, loads llama + vllm + the shim + vllm_tools."""
from __future__ import annotations
import contextlib, importlib.util, sys, types
from pathlib import Path
_AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_AGENT_ROOT))

class _Timeout(Exception):
    pass
class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code, self.detail = status_code, detail

def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    sys.modules[name] = m
    return m

def _file_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

def load():
    """Returns (llama, vllm, vllm_tools, restore)."""
    snapshot = dict(sys.modules)
    _stub("requests", Session=object, RequestException=Exception,
          exceptions=types.SimpleNamespace(Timeout=_Timeout, RequestException=Exception))
    _stub("fastapi", Header=lambda **k: None, HTTPException=_HTTPException, Query=lambda *a, **k: None, Request=object)
    _stub("fastapi.responses", Response=object, StreamingResponse=object)
    _stub("starlette.concurrency", run_in_threadpool=None); _stub("starlette"); _stub("stream_pool")
    @contextlib.contextmanager
    def _be(*a, **k): yield
    _stub("_best_effort", best_effort=_be)
    _file_module("_bench_replay", _AGENT_ROOT / "_bench_replay.py")
    _stub("collectors"); _stub("collectors.gpu", collect_gpu=lambda *a, **k: {})
    pkg = types.ModuleType("providers"); pkg.__path__ = [str(_AGENT_ROOT / "providers")]; sys.modules["providers"] = pkg
    for sub in ("llama_install", "llama_sse", "llama_upgrade"):
        sys.modules[f"providers.{sub}"] = types.ModuleType(f"providers.{sub}")
    llama = _file_module("providers.llama", _AGENT_ROOT / "providers" / "llama.py")
    for name in ("residency", "vllm", "lms_timings_shim"):
        _file_module(f"providers.{name}", _AGENT_ROOT / "providers" / f"{name}.py")
    tools = _file_module("providers.vllm_tools", _AGENT_ROOT / "providers" / "vllm_tools.py")
    def restore():
        for k in list(sys.modules):
            if k not in snapshot: sys.modules.pop(k, None)
        sys.modules.update(snapshot)
    return llama, sys.modules["providers.vllm"], tools, restore
