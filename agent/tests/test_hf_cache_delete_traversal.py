# agent/tests/test_hf_cache_delete_traversal.py
"""#277: DELETE /llama/config must not let an unknown model_id derive a
traversing quant pattern that escapes the HF snapshots dir."""
from __future__ import annotations

import contextlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _load_llama():
    # Stub the heavy third-party / sibling deps so llama.py imports without a venv.
    _stub("requests")
    _stub("fastapi", Header=lambda **k: None, HTTPException=Exception,
          Query=lambda *a, **k: None, Request=object)
    _stub("fastapi.responses", Response=object, StreamingResponse=object)
    _stub("starlette.concurrency", run_in_threadpool=None)
    _stub("starlette")
    _stub("stream_pool")

    @contextlib.contextmanager
    def _be(*a, **k):
        yield
    _stub("_best_effort", best_effort=_be)
    _stub("_bench_replay", BenchReplayBuffer=lambda *a, **k: object())
    _stub("collectors")
    _stub("collectors.gpu", collect_gpu=lambda *a, **k: {})

    pkg = types.ModuleType("providers")
    pkg.__path__ = [str(_AGENT_ROOT / "providers")]
    sys.modules["providers"] = pkg
    for sub in ("llama_install", "llama_sse", "llama_upgrade"):
        sys.modules[f"providers.{sub}"] = types.ModuleType(f"providers.{sub}")

    spec = importlib.util.spec_from_file_location(
        "providers.llama", _AGENT_ROOT / "providers" / "llama.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["providers.llama"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def llama():
    return _load_llama()


@pytest.mark.parametrize("model_id", [
    "org/repo:../../../../etc/passwd.gguf",
    "org/repo:../secret.gguf",
    "org/repo:sub/dir.gguf",
    "org/repo:..",
])
def test_traversing_quant_rejected(llama, model_id, monkeypatch):
    # No ini section -> repo/quant derived from model_id; must be rejected
    # before any glob/unlink touches the filesystem.
    monkeypatch.setattr(llama, "_require_ctx", lambda: (_ for _ in ()).throw(RuntimeError("no ctx")))

    def _boom(*a, **k):
        raise AssertionError("_hf_cache_root reached — traversal not rejected")
    monkeypatch.setattr(llama, "_hf_cache_root", _boom)

    deleted, err = llama._delete_quant_from_hf_cache(model_id)
    assert deleted == []
    assert err and ("traversal" in err or "malformed" in err)


def test_clean_quant_passes_validation(llama, monkeypatch, tmp_path):
    # A well-formed quant must clear validation and proceed to the cache lookup.
    monkeypatch.setattr(llama, "_require_ctx", lambda: (_ for _ in ()).throw(RuntimeError("no ctx")))
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path)
    deleted, err = llama._delete_quant_from_hf_cache("org/repo:model-Q4_K_M.gguf")
    # Reached the cache: snapshots dir absent, not a validation rejection.
    assert deleted == []
    assert err and "Snapshots dir not found" in err
