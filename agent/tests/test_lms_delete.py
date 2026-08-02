# agent/tests/test_lms_delete.py
"""#492: POST /lms/delete — file removal with traversal + loaded guards."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


def _stub_if_absent(name: str, **attrs) -> types.ModuleType:
    # setdefault only — never clobber a real (or earlier-stubbed) module.
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _load_lms():
    _stub_if_absent("requests", Session=lambda: None)
    _stub_if_absent("fastapi", Header=lambda **k: None,
                    HTTPException=_HTTPException,
                    Query=lambda *a, **k: None, Request=object)
    # Synthetic parent package so lms.py's `from . import _shared` resolves
    # without executing the real _shared (fastapi/starlette heavy).
    pkg = _stub_if_absent("lms_pkg")
    pkg.__path__ = []
    pkg._shared = _stub_if_absent("lms_pkg._shared", openai_forward=None)
    spec = importlib.util.spec_from_file_location(
        "lms_pkg.lms", _AGENT_ROOT / "providers" / "lms.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lms(monkeypatch, tmp_path):
    mod = _load_lms()
    cfg = types.SimpleNamespace(LMS_ENABLED=True, LMS_CMD="/usr/bin/lms",
                                AGENT_USER=None, LMS_API_URL="http://x")
    mod.set_context(types.SimpleNamespace(config=cfg,
                                          check_bearer=lambda *_a: None))
    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setattr(mod, "_lms_models_root", lambda: root)
    monkeypatch.setattr(mod, "lms_get_ps", lambda: [])
    mod._test_root = root
    return mod


def _catalog(monkeypatch, mod, entries):
    out = json.dumps(entries)
    monkeypatch.setattr(mod.subprocess, "check_output",
                        lambda *a, **k: out)


def test_delete_removes_the_file_and_prunes_empty_dirs(lms, monkeypatch):
    f = lms._test_root / "Qwen" / "Qwen2.5-1.5B-Instruct-GGUF"
    f.mkdir(parents=True)
    gguf = f / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    gguf.write_bytes(b"x" * 10)
    _catalog(monkeypatch, lms, [{
        "modelKey": "qwen2.5-1.5b-instruct",
        "path": "Qwen/Qwen2.5-1.5B-Instruct-GGUF/qwen2.5-1.5b-instruct-q4_k_m.gguf"}])
    out = lms.lms_delete_endpoint({"model": "qwen2.5-1.5b-instruct"})
    assert out["ok"] is True
    assert out["deleted_files"] == ["qwen2.5-1.5b-instruct-q4_k_m.gguf"]
    assert out["freed_bytes"] == 10
    assert not gguf.exists()
    assert not (lms._test_root / "Qwen").exists()      # pruned
    assert lms._test_root.exists()                     # root survives


def test_delete_takes_every_shard_of_a_sharded_model(lms, monkeypatch):
    d = lms._test_root / "Qwen" / "Qwen2.5-7B-Instruct-GGUF"
    d.mkdir(parents=True)
    s1 = d / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"
    s2 = d / "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf"
    other = d / "qwen2.5-7b-instruct-q8_0.gguf"
    for p in (s1, s2, other):
        p.write_bytes(b"y")
    _catalog(monkeypatch, lms, [{
        "modelKey": "qwen2.5-7b-instruct",
        "path": "Qwen/Qwen2.5-7B-Instruct-GGUF/"
                "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"}])
    out = lms.lms_delete_endpoint({"model": "qwen2.5-7b-instruct"})
    assert out["ok"] is True
    assert sorted(out["deleted_files"]) == [s1.name, s2.name]
    assert not s1.exists() and not s2.exists()
    assert other.exists()                              # other quant kept
    assert d.exists()                                  # dir not empty -> kept


def test_delete_rejects_a_path_escaping_the_models_root(lms, monkeypatch):
    outside = lms._test_root.parent / "victim.gguf"
    outside.write_bytes(b"z")
    _catalog(monkeypatch, lms, [{"modelKey": "evil",
                                 "path": "../victim.gguf"}])
    out = lms.lms_delete_endpoint({"model": "evil"})
    assert out["ok"] is False and "escapes" in out["error"]
    assert outside.exists()


def test_delete_refuses_a_loaded_model(lms, monkeypatch):
    monkeypatch.setattr(lms, "lms_get_ps",
                        lambda: [{"identifier": "qwen2.5-1.5b-instruct",
                                  "model": "qwen2.5-1.5b-instruct",
                                  "status": "IDLE"}])
    out = lms.lms_delete_endpoint({"model": "qwen2.5-1.5b-instruct"})
    assert out["ok"] is False and "loaded" in out["error"]


def test_delete_matches_a_quantless_catalog_key_when_unambiguous(lms, monkeypatch):
    d = lms._test_root / "bartowski" / "m"
    d.mkdir(parents=True)
    (d / "m-q4_k_m.gguf").write_bytes(b"q")
    _catalog(monkeypatch, lms, [{"modelKey": "bartowski_m",
                                 "path": "bartowski/m/m-q4_k_m.gguf"}])
    out = lms.lms_delete_endpoint({"model": "bartowski_m@q4_k_m"})
    assert out["ok"] is True


def test_delete_rejects_an_ambiguous_base_key(lms, monkeypatch):
    d = lms._test_root / "b" / "m"
    d.mkdir(parents=True)
    (d / "a.gguf").write_bytes(b"1")
    (d / "b.gguf").write_bytes(b"2")
    _catalog(monkeypatch, lms, [
        {"modelKey": "m@q4_k_m", "path": "b/m/a.gguf"},
        {"modelKey": "m@q8_0", "path": "b/m/b.gguf"}])
    out = lms.lms_delete_endpoint({"model": "m"})
    assert out["ok"] is False and "catalog" in out["error"]


def test_delete_refuses_when_a_quant_sibling_is_loaded(lms, monkeypatch):
    monkeypatch.setattr(lms, "lms_get_ps",
                        lambda: [{"identifier": "m@q4_k_m", "model": "m@q4_k_m",
                                  "status": "IDLE"}])
    out = lms.lms_delete_endpoint({"model": "m"})
    assert out["ok"] is False and "loaded" in out["error"]


def test_delete_errors_when_model_not_in_catalog(lms, monkeypatch):
    _catalog(monkeypatch, lms, [{"modelKey": "other", "path": "o/o.gguf"}])
    out = lms.lms_delete_endpoint({"model": "qwen2.5-1.5b-instruct"})
    assert out["ok"] is False and "catalog" in out["error"]


def test_delete_requires_a_valid_model_id(lms):
    with pytest.raises(Exception):
        lms.lms_delete_endpoint({"model": ""})
    with pytest.raises(Exception):
        lms.lms_delete_endpoint({"model": "bad id with spaces!"})
