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

    def fake_check_output(cmd, **kw):
        argv["cmd"] = cmd
        return "[]"

    monkeypatch.setattr(llama.subprocess, "check_output", fake_check_output)
    r = llama.llama_hf_trending(authorization="Bearer x", **kwargs)
    assert r["ok"] is True
    return argv["cmd"]


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_defaults_match_legacy_query(llama, monkeypatch):
    cmd = _run(llama, monkeypatch)
    assert _flag(cmd, "--sort") == "trending_score"
    assert _flag(cmd, "--limit") == "10"
    assert _flag(cmd, "--num-parameters") == "min:27B,max:35B"


def test_valid_params_flow_through(llama, monkeypatch):
    cmd = _run(llama, monkeypatch, limit=5, min_b="9B", max_b="27B", sort="downloads")
    assert _flag(cmd, "--limit") == "5"
    assert _flag(cmd, "--num-parameters") == "min:9B,max:27B"
    assert _flag(cmd, "--sort") == "downloads"


def test_newest_maps_to_created_at(llama, monkeypatch):
    cmd = _run(llama, monkeypatch, sort="newest")
    assert _flag(cmd, "--sort") == "created_at"


def test_garbage_falls_back(llama, monkeypatch):
    cmd = _run(llama, monkeypatch, limit=9999, min_b="'; rm -rf", max_b="27b or 1=1", sort="bogus")
    assert _flag(cmd, "--limit") == "50"
    assert _flag(cmd, "--num-parameters") == "min:27B,max:35B"
    assert _flag(cmd, "--sort") == "trending_score"


def test_limit_floor(llama, monkeypatch):
    cmd = _run(llama, monkeypatch, limit=0)
    assert _flag(cmd, "--limit") == "1"
