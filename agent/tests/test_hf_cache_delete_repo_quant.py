# agent/tests/test_hf_cache_delete_repo_quant.py
"""#1061: a card saved as `hf-repo = owner/repo:QUANT` (no hf-file) must still
resolve to that quant's .gguf, so the cache delete unlinks it."""
from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hf_cache_delete_traversal import _load_llama  # noqa: E402


@pytest.fixture(scope="module")
def llama():
    return _load_llama()


def _cache(tmp_path: Path, repo: str, files: "list[str]") -> Path:
    root = tmp_path / "hub"
    snap = root / f"models--{repo.replace('/', '--')}" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    blobs = root / "blobs"
    blobs.mkdir()
    for i, name in enumerate(files):
        blob = blobs / f"blob{i}"
        blob.write_bytes(b"x")
        (snap / name).symlink_to(blob)
    return root


def _ini(section: str, values: dict):
    cp = configparser.ConfigParser()
    cp.add_section(section)
    for k, v in values.items():
        cp[section][k] = v
    return cp


def test_repo_quant_card_resolves_the_quant_file(llama, tmp_path, monkeypatch):
    mid = "bartowski/Qwen3.8-27B-GGUF:Q4_K_M"
    root = _cache(tmp_path, "bartowski/Qwen3.8-27B-GGUF", ["Qwen3.8-27B-Q4_K_M.gguf", "Qwen3.8-27B-Q5_K_M.gguf"])
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: _ini(mid, {"hf-repo": mid, "ctx-size": "8192"}))
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: root)
    files, err = llama._locate_quant_files(mid)
    assert err is None and [p.name for p in files] == ["Qwen3.8-27B-Q4_K_M.gguf"]
    deleted, err = llama._delete_quant_from_hf_cache(mid)
    assert err is None and len(deleted) == 2
    snap = root / "models--bartowski--Qwen3.8-27B-GGUF" / "snapshots" / "abc"
    assert sorted(p.name for p in snap.iterdir()) == ["Qwen3.8-27B-Q5_K_M.gguf"]


def test_hf_file_still_wins_over_the_repo_suffix(llama, tmp_path, monkeypatch):
    mid = "org/repo:Q4_K_M"
    root = _cache(tmp_path, "org/repo", ["model-q4_k_m.gguf", "model-q8_0.gguf"])
    monkeypatch.setattr(llama, "_llama_read_ini", lambda: _ini(mid, {"hf-repo": "org/repo:Q4_K_M", "hf-file": "model-q8_0.gguf"}))
    monkeypatch.setattr(llama, "_hf_cache_root", lambda: root)
    files, err = llama._locate_quant_files(mid)
    assert err is None and [p.name for p in files] == ["model-q8_0.gguf"]
