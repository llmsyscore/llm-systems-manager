# agent/tests/test_llama_ghost_presets.py
"""#1062: /llama/models hides a preset llama-server still lists after its config.ini section
and cache file were deleted; presets with a section, a cache file, or a loaded slot stay."""
from __future__ import annotations

import configparser
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hf_cache_delete_traversal import _load_llama  # noqa: E402


@pytest.fixture(scope="module")
def llama():
    return _load_llama()


def _ini(*sections):
    cp = configparser.ConfigParser()
    for sec in sections:
        cp.add_section(sec)
    return cp


def _entry(mid, status="unloaded", source="preset"):
    return {"id": mid, "source": source, "status": {"value": status}}


def test_ghost_presets_are_hidden_and_everything_else_stays(llama, tmp_path, monkeypatch):
    root = tmp_path / "hub"
    snap = root / "models--org--cached-GGUF" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "cached-Q4_K_M.gguf").write_bytes(b"x")
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: root)
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: _ini("org/kept:Q4_K_M"))
    listing = {"object": "list", "data": [
        _entry("org/kept:Q4_K_M"),                       # section present
        _entry("org/cached-GGUF:Q4_K_M"),                # no section, file in the cache
        _entry("org/ghost:Q4_K_M"),                      # neither: hidden
        _entry("org/busy:Q4_K_M", status="loaded"),      # no section, no file, but serving: stays
        _entry("org/other:Q4_K_M", source="cache"),      # not a preset: untouched
    ]}
    out = llama._drop_ghost_presets(listing)
    assert [m["id"] for m in out["data"]] == ["org/kept:Q4_K_M", "org/cached-GGUF:Q4_K_M", "org/busy:Q4_K_M", "org/other:Q4_K_M"]


def test_models_endpoint_applies_the_filter(llama, monkeypatch):
    monkeypatch.setattr(llama, "_require_ctx", lambda: types.SimpleNamespace(
        config=types.SimpleNamespace(LLAMA_API_URL="http://x:8080"), check_bearer=lambda *a: None))
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_llama_read_ini", _ini)
    monkeypatch.setattr(llama, "_locate_quant_files", lambda mid: ([], "none"))
    monkeypatch.setattr(llama, "requests", types.SimpleNamespace(
        get=lambda url, timeout: types.SimpleNamespace(json=lambda: {"data": [_entry("org/ghost:Q4_K_M")]})))
    assert llama.llama_models_endpoint(authorization=None) == {"data": []}
