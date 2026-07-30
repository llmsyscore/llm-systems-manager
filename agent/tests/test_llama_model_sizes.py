# agent/tests/test_llama_model_sizes.py
"""#472/#474: GET /llama/models/sizes — gguf size lookup for autopilot."""
from __future__ import annotations

import contextlib
import importlib.util
import os
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


@pytest.fixture
def llama():
    mod = _load_llama()
    mod._model_sizes_cache["mtime"] = None
    mod._model_sizes_cache["sizes"] = {}
    return mod


class _FakeCP:
    """Minimal configparser.ConfigParser stand-in for section lookups."""
    def __init__(self, sections):
        self._sections = sections

    def has_section(self, name):
        return name in self._sections

    def __getitem__(self, name):
        return self._sections[name]

    def sections(self):
        return list(self._sections.keys())


class _Ctx:
    def __init__(self, ini_path):
        self.config = types.SimpleNamespace(LLAMA_CONFIG_INI=str(ini_path))


def _make_snapshot(root: Path, repo: str, filename: str, size: int) -> Path:
    snap = root / f"models--{repo.replace('/', '--')}" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    p = snap / filename
    p.write_bytes(b"x" * size)
    return p


# ── _model_gguf_size_bytes: stat the resolved quant file(s) ────────────

def test_model_gguf_size_bytes_sums_matched_files(llama, monkeypatch, tmp_path):
    cache_root = tmp_path / "hub"
    _make_snapshot(cache_root, "org/repo", "model-Q4_K_M.gguf", 4096)
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: cache_root)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: _FakeCP({}))
    assert llama._model_gguf_size_bytes("org/repo:model-Q4_K_M.gguf") == 4096


def test_model_gguf_size_bytes_none_when_validation_rejects(llama, monkeypatch):
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: _FakeCP({}))

    def _boom():
        raise AssertionError("_hf_cache_root reached — traversal not rejected")
    monkeypatch.setattr(llama, "_hf_cache_root", _boom)
    assert llama._model_gguf_size_bytes("org/repo:../../etc/passwd.gguf") is None


def test_model_gguf_size_bytes_none_when_no_snapshot(llama, monkeypatch, tmp_path):
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: _FakeCP({}))
    assert llama._model_gguf_size_bytes("org/repo:missing-Q4_K_M.gguf") is None


def test_model_gguf_size_bytes_uses_ini_hf_repo_file(llama, monkeypatch, tmp_path):
    """A section with an explicit hf-repo/hf-file overrides deriving from model_id."""
    cache_root = tmp_path / "hub"
    _make_snapshot(cache_root, "actual/repo", "actual-file.gguf", 2048)
    cp = _FakeCP({"my-alias": {"hf-repo": "actual/repo", "hf-file": "actual-file.gguf"}})
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: cache_root)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: cp)
    assert llama._model_gguf_size_bytes("my-alias") == 2048


# ── delete behavior unchanged after the _locate_quant_files refactor ───

def test_delete_quant_still_rejects_traversal(llama, monkeypatch):
    monkeypatch.setattr(llama, "_require_ctx",
                        lambda: (_ for _ in ()).throw(RuntimeError("no ctx")))

    def _boom(*a, **k):
        raise AssertionError("_hf_cache_root reached — traversal not rejected")
    monkeypatch.setattr(llama, "_hf_cache_root", _boom)
    deleted, err = llama._delete_quant_from_hf_cache("org/repo:../secret.gguf")
    assert deleted == []
    assert err and "traversal" in err


def test_delete_quant_still_unlinks_matched_file(llama, monkeypatch, tmp_path):
    cache_root = tmp_path / "hub"
    p = _make_snapshot(cache_root, "org/repo", "model-Q4_K_M.gguf", 128)
    monkeypatch.setattr(llama, "_require_ctx",
                        lambda: (_ for _ in ()).throw(RuntimeError("no ctx")))
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: cache_root)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: _FakeCP({}))
    deleted, err = llama._delete_quant_from_hf_cache("org/repo:model-Q4_K_M.gguf")
    assert err is None
    assert str(p) in deleted
    assert not p.exists()


# ── _llama_all_model_sizes: catalog sweep + mtime cache ────────────────

def test_all_model_sizes_skips_star_and_defaults_sections(llama, monkeypatch, tmp_path):
    cache_root = tmp_path / "hub"
    _make_snapshot(cache_root, "org/repo", "m-Q4_K_M.gguf", 2048)
    cp = _FakeCP({
        "*": {},
        "__DEFAULTS__": {},
        "org/repo:m-Q4_K_M.gguf": {"hf-repo": "org/repo", "hf-file": "m-Q4_K_M.gguf"},
    })
    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[*]\n")
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: cache_root)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: cp)
    monkeypatch.setattr(llama, "_require_ctx", lambda: _Ctx(ini_path))

    sizes = llama._llama_all_model_sizes()
    assert sizes == {"org/repo:m-Q4_K_M.gguf": 2048}


def test_all_model_sizes_cached_until_ini_mtime_changes(llama, monkeypatch, tmp_path):
    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[*]\n")
    orig_mtime = os.path.getmtime(ini_path)

    cp = _FakeCP({"m1": {"hf-repo": "org/repo", "hf-file": "m-Q4_K_M.gguf"}})
    calls = {"n": 0}

    def _read_ini():
        calls["n"] += 1
        return cp
    monkeypatch.setattr(llama, "_llama_read_ini", _read_ini)
    monkeypatch.setattr(llama, "_model_gguf_size_bytes", lambda mid: 1234)
    monkeypatch.setattr(llama, "_require_ctx", lambda: _Ctx(ini_path))

    first = llama._llama_all_model_sizes()
    second = llama._llama_all_model_sizes()
    assert first == {"m1": 1234} and second == first
    assert calls["n"] == 1        # second call hit the mtime cache, no ini re-read

    os.utime(ini_path, (orig_mtime + 100, orig_mtime + 100))
    third = llama._llama_all_model_sizes()
    assert third == {"m1": 1234}
    assert calls["n"] == 2         # mtime bump forced a fresh sweep


# ── route handler: auth + enabled gates, response shape ────────────────

def test_endpoint_returns_ok_and_sizes_dict(llama, monkeypatch, tmp_path):
    ini_path = tmp_path / "config.ini"
    ini_path.write_text("[*]\n")

    class _FakeCfg:
        LLAMA_CONFIG_INI = str(ini_path)
        LLAMA_ENABLED = True

    class _FakeFullCtx:
        config = _FakeCfg()
        def check_bearer(self, authorization):
            return None

    monkeypatch.setattr(llama, "_require_ctx", lambda: _FakeFullCtx())
    monkeypatch.setattr(llama, "_llama_all_model_sizes", lambda: {"m1": 999})
    out = llama.llama_model_sizes_endpoint(authorization="Bearer x")
    assert out == {"ok": True, "sizes": {"m1": 999}}
