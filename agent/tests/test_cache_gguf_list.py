# agent/tests/test_cache_gguf_list.py
"""GET /llama/cache/gguf lists snapshot .gguf files without mmproj projectors."""
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


def _mk(root: Path, repo: str, snap: str, names: list[str]) -> None:
    d = root / f"models--{repo.replace('/', '--')}" / "snapshots" / snap
    d.mkdir(parents=True)
    for n in names:
        (d / n).write_bytes(b"x" * 3)


def test_lists_gguf_files_sorted_without_mmproj(llama, tmp_path):
    _mk(tmp_path, "unsloth/Qwen3-0.6B-GGUF", "aaa", ["Qwen3-0.6B-Q8_0.gguf", "mmproj-F16.gguf", "README.md"])
    _mk(tmp_path, "bartowski/gemma-3-27b-it-GGUF", "bbb", ["gemma-3-27b-it-Q5_K_M.gguf"])
    rows = llama._list_cache_ggufs(tmp_path)
    assert [(r["repo"], r["file"]) for r in rows] == [
        ("bartowski/gemma-3-27b-it-GGUF", "gemma-3-27b-it-Q5_K_M.gguf"),
        ("unsloth/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q8_0.gguf"),
    ]
    assert rows[0]["path"].endswith("/snapshots/bbb/gemma-3-27b-it-Q5_K_M.gguf")
    assert rows[0]["size"] == 3


def test_missing_root_is_empty(llama, tmp_path):
    assert llama._list_cache_ggufs(tmp_path / "nope") == []


def test_route_registered(llama):
    paths = {p for _, p, _ in llama._ROUTES}
    assert "/llama/cache/gguf" in paths
