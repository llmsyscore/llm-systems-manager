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
        _entry("org/cached-GGUF:Q4_K_M"),                # section deleted, file still cached: hidden
        _entry("org/ghost:Q4_K_M"),                      # section and file gone: hidden
        _entry("org/busy:Q4_K_M", status="loaded"),      # no section, but serving: stays
        _entry("org/other:Q4_K_M", source="cache"),      # not a preset: untouched
    ]}
    out = llama._drop_ghost_presets(listing)
    assert [m["id"] for m in out["data"]] == ["org/kept:Q4_K_M", "org/busy:Q4_K_M", "org/other:Q4_K_M"]


def test_models_endpoint_applies_the_filter(llama, monkeypatch):
    monkeypatch.setattr(llama, "_require_ctx", lambda: types.SimpleNamespace(
        config=types.SimpleNamespace(LLAMA_API_URL="http://x:8080"), check_bearer=lambda *a: None))
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_llama_read_ini", _ini)
    monkeypatch.setattr(llama, "_locate_quant_files", lambda mid: ([], "none"))
    monkeypatch.setattr(llama, "cached_models", lambda: [])
    monkeypatch.setattr(llama, "llama_mode_block", lambda: {})
    monkeypatch.setattr(llama, "requests", types.SimpleNamespace(
        get=lambda url, timeout: types.SimpleNamespace(json=lambda: {"data": [_entry("org/ghost:Q4_K_M")]})))
    assert llama.llama_models_endpoint(authorization=None)["data"] == []


def _heal_env(llama, tmp_path, monkeypatch, model):
    ini = tmp_path / "models.ini"
    ini.write_text(f"[org/m:Q4_K_M]\nmodel = {model}\nctx-size = 4096\n")
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: tmp_path / "hub")
    monkeypatch.setattr(llama, "_llama_effective_presets_path", lambda: str(ini))
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: llama._read_presets(str(ini)))
    monkeypatch.setattr(llama, "_locate_quant_files", lambda mid: ([], "none"))
    return ini


def test_moved_snapshot_is_repointed_and_kept(llama, tmp_path, monkeypatch):
    repo = tmp_path / "hub" / "models--org--m-GGUF" / "snapshots"
    (repo / "new").mkdir(parents=True)
    (repo / "new" / "m-Q4_K_M.gguf").write_bytes(b"x")
    ini = _heal_env(llama, tmp_path, monkeypatch, str(repo / "old" / "m-Q4_K_M.gguf"))
    out = llama._drop_ghost_presets({"data": [_entry("org/m:Q4_K_M")]})
    assert [m["id"] for m in out["data"]] == ["org/m:Q4_K_M"]
    cp = llama._read_presets(str(ini))
    assert cp.get("org/m:Q4_K_M", "model") == str(repo / "new" / "m-Q4_K_M.gguf")
    assert cp.get("org/m:Q4_K_M", "ctx-size") == "4096"


def test_moved_snapshot_picks_newest_candidate(llama, tmp_path, monkeypatch):
    import os
    repo = tmp_path / "hub" / "models--org--m-GGUF" / "snapshots"
    for name, t in (("a", 100), ("b", 200)):
        (repo / name).mkdir(parents=True)
        f = repo / name / "m-Q4_K_M.gguf"
        f.write_bytes(b"x")
        os.utime(f, (t, t))
    ini = _heal_env(llama, tmp_path, monkeypatch, str(repo / "old" / "m-Q4_K_M.gguf"))
    llama._drop_ghost_presets({"data": []})
    assert llama._read_presets(str(ini)).get("org/m:Q4_K_M", "model") == str(repo / "b" / "m-Q4_K_M.gguf")


def test_moved_snapshot_without_candidate_stays_unchanged(llama, tmp_path, monkeypatch):
    repo = tmp_path / "hub" / "models--org--m-GGUF" / "snapshots"
    (repo / "new").mkdir(parents=True)
    (repo / "new" / "other-Q8_0.gguf").write_bytes(b"x")
    ini = _heal_env(llama, tmp_path, monkeypatch, str(repo / "old" / "m-Q4_K_M.gguf"))
    before = ini.read_text()
    out = llama._drop_ghost_presets({"data": [_entry("org/m:Q4_K_M")]})
    assert ini.read_text() == before and [m["id"] for m in out["data"]] == ["org/m:Q4_K_M"]


def test_missing_model_outside_hub_is_untouched(llama, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere" / "models--org--m-GGUF" / "snapshots"
    (other / "new").mkdir(parents=True)
    (other / "new" / "m-Q4_K_M.gguf").write_bytes(b"x")
    (tmp_path / "hub").mkdir()
    ini = _heal_env(llama, tmp_path, monkeypatch, str(other / "old" / "m-Q4_K_M.gguf"))
    before = ini.read_text()
    llama._drop_ghost_presets({"data": []})
    assert ini.read_text() == before
