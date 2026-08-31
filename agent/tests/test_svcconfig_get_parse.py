"""llama_svcconfig_get (#767): ExecStart args round-trip — a flag outside the
value allowlist followed by a non-flag token keeps that token as its value
instead of collapsing to flag-only and dropping it."""
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


def _parse(llama, monkeypatch, tmp_path, exec_line):
    unit = tmp_path / "llama.service"
    unit.write_text(f"[Service]\nExecStart={exec_line}\n")
    monkeypatch.setattr(llama, "_require_ctx", _Ctx)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_llama_svc_file_path", lambda: str(unit))
    r = llama.llama_svcconfig_get(authorization="Bearer x")
    assert r["ok"] is True, r
    return {a["flag"]: a for a in r["args"]}


def test_unknown_value_flags_keep_their_values(llama, monkeypatch, tmp_path):
    args = _parse(llama, monkeypatch, tmp_path,
                  "/usr/bin/llama-server --cors-origins * --tools all --metrics")
    assert args["--cors-origins"] == {"flag": "--cors-origins", "value": "*", "bool": False}
    assert args["--tools"] == {"flag": "--tools", "value": "all", "bool": False}
    assert args["--metrics"]["bool"] is True


def test_boolean_flags_stay_boolean_before_other_flags(llama, monkeypatch, tmp_path):
    args = _parse(llama, monkeypatch, tmp_path,
                  "/usr/bin/llama-server --metrics --perf --port 8080")
    assert args["--metrics"]["bool"] is True
    assert args["--perf"]["bool"] is True
    assert args["--port"] == {"flag": "--port", "value": "8080", "bool": False}


def test_trailing_flag_is_boolean(llama, monkeypatch, tmp_path):
    args = _parse(llama, monkeypatch, tmp_path, "/usr/bin/llama-server --kv-unified")
    assert args["--kv-unified"]["bool"] is True
