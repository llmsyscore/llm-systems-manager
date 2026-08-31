"""llama_hf_trending (#767): limit/min_b/max_b/sort validate and map onto the
hf-cli argv; garbage falls back to the legacy defaults."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from test_bench_parse_row import _load_llama  # noqa: E402


@pytest.fixture(scope="module")
def llama():
    return _load_llama()


class _Ctx:
    def check_bearer(self, _):
        return None


def _run(llama, monkeypatch, **kwargs):
    argv = {}
    monkeypatch.setattr(llama, "_require_ctx", _Ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_hf_cli_path", lambda: "hf")

    import json as _json
    rows = kwargs.pop("_rows", [])

    def fake_check_output(cmd, **kw):
        argv["cmd"] = cmd
        return _json.dumps(rows)

    monkeypatch.setattr(llama.subprocess, "check_output", fake_check_output)
    r = llama.llama_hf_trending(authorization="Bearer x", **kwargs)
    assert r["ok"] is True
    argv["data"] = r["data"]
    return argv["cmd"], argv["data"]


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_defaults_match_legacy_query(llama, monkeypatch):
    cmd, _ = _run(llama, monkeypatch)
    assert _flag(cmd, "--sort") == "trending_score"
    assert _flag(cmd, "--limit") == "10"
    assert _flag(cmd, "--num-parameters") == "min:27B,max:35B"


def test_valid_params_flow_through(llama, monkeypatch):
    cmd, _ = _run(llama, monkeypatch, limit=5, min_b="9B", max_b="27B", sort="downloads")
    assert _flag(cmd, "--limit") == "5"
    assert _flag(cmd, "--num-parameters") == "min:9B,max:27B"
    assert _flag(cmd, "--sort") == "downloads"


def test_downloads_reranks_by_all_time(llama, monkeypatch):
    rows = [{"id": "a", "downloads_all_time": 10},
            {"id": "b", "downloads_all_time": 300},
            {"id": "c", "downloads_all_time": 200}]
    _, data = _run(llama, monkeypatch, sort="downloads", _rows=rows)
    assert [m["id"] for m in data] == ["b", "c", "a"]


def test_newest_sorts_trending_pool_by_created_and_slices(llama, monkeypatch):
    rows = [{"id": "old", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "new", "created_at": "2026-08-01T00:00:00Z"},
            {"id": "mid", "created_at": "2025-06-01T00:00:00Z"}]
    cmd, data = _run(llama, monkeypatch, limit=2, sort="newest", _rows=rows)
    assert _flag(cmd, "--sort") == "trending_score"
    assert _flag(cmd, "--limit") == "100"
    assert [m["id"] for m in data] == ["new", "mid"]


def test_garbage_falls_back(llama, monkeypatch):
    cmd, _ = _run(llama, monkeypatch, limit=9999, min_b="'; rm -rf", max_b="27b or 1=1", sort="bogus")
    assert _flag(cmd, "--limit") == "50"
    assert _flag(cmd, "--num-parameters") == "min:27B,max:35B"
    assert _flag(cmd, "--sort") == "trending_score"


def test_limit_floor(llama, monkeypatch):
    cmd, _ = _run(llama, monkeypatch, limit=0)
    assert _flag(cmd, "--limit") == "1"
