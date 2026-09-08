"""_bench_parse_row returns a (gen, ppt, pg) triple per llama-bench JSONL row."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))


def _stub(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_llama():
    from tests._vllm_load import load_vllm
    load_vllm()
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


def test_llama_bench_ppt_row(llama):
    got = llama._bench_parse_row({"n_prompt": 2048, "n_gen": 0, "avg_ts": 769.87}, "llama-bench")
    assert got == (None, 769.87, None)


def test_llama_bench_gen_row(llama):
    got = llama._bench_parse_row({"n_prompt": 0, "n_gen": 512, "avg_ts": 40.02}, "llama-bench")
    assert got == (40.02, None, None)


def test_llama_bench_pg_row_uses_pg_slot(llama):
    got = llama._bench_parse_row({"n_prompt": 4096, "n_gen": 256, "avg_ts": 370.08}, "llama-bench")
    assert got == (None, None, 370.08)


def test_llama_bench_non_result_row(llama):
    assert llama._bench_parse_row({"build_commit": "abc"}, "llama-bench") == (None, None, None)
    assert llama._bench_parse_row("not a dict", "llama-bench") == (None, None, None)


def test_unknown_tool_returns_none(llama):
    row = {"n_prompt": 2048, "n_gen": 0, "avg_ts": 769.87}
    assert llama._bench_parse_row(row, "llama-batched-bench") == (None, None, None)
