# agent/tests/test_lms_ps_mapping.py
"""#502: lms ps --json mapping — sizeBytes / quantization / deviceIdentifier."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]


def _stub_if_absent(name: str, **attrs) -> types.ModuleType:
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
    pkg = _stub_if_absent("lms_pkg")
    pkg.__path__ = []
    pkg._shared = _stub_if_absent("lms_pkg._shared", openai_forward=None)
    spec = importlib.util.spec_from_file_location(
        "lms_pkg.lms", _AGENT_ROOT / "providers" / "lms.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lms(monkeypatch):
    mod = _load_lms()
    cfg = types.SimpleNamespace(LMS_ENABLED=True, LMS_CMD="/usr/bin/lms",
                                AGENT_USER=None, LMS_API_URL="http://x")
    mod.set_context(types.SimpleNamespace(config=cfg,
                                          check_bearer=lambda *_a: None))
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)
    return mod


def _mock_ps(mod, monkeypatch, rows):
    monkeypatch.setattr(mod.subprocess, "check_output",
                        lambda *a, **k: json.dumps(rows))


def test_ps_maps_modern_cli_fields(lms, monkeypatch):
    # Field shape captured from a real `lms ps --json` (LM Studio 2026 CLI).
    _mock_ps(lms, monkeypatch, [{
        "identifier": "nvidia/nemotron-3-nano-4b",
        "modelKey": "nvidia/nemotron-3-nano-4b",
        "sizeBytes": 4233778565,
        "deviceIdentifier": None,
        "paramsString": "4.0B",
        "quantization": {"name": "Q8_0", "bits": 8},
        "maxContextLength": 1048576,
        "contextLength": 67328,
        "status": "processingPrompt",
        "parallel": 2,
    }])
    row = lms.lms_get_ps()[0]
    assert row["size"] == "4.23 GB"
    assert row["quant"] == "Q8_0"
    assert row["params"] == "4.0B"
    assert row["device"] == ""
    assert row["status"] == "PROCESSINGPROMPT"
    assert row["context"] == 67328
    assert row["parallel"] == 2


def test_ps_keeps_legacy_string_fields(lms, monkeypatch):
    _mock_ps(lms, monkeypatch, [{
        "identifier": "m", "size": "19.20 GB", "device": "mps",
        "quantization": "Q4_K_M", "status": "IDLE", "context": 4096,
    }])
    row = lms.lms_get_ps()[0]
    assert row["size"] == "19.20 GB"
    assert row["device"] == "mps"
    assert row["quant"] == "Q4_K_M"


def test_fmt_size_bytes_boundaries(lms):
    assert lms._fmt_size_bytes(None) == ""
    assert lms._fmt_size_bytes(0) == ""
    assert lms._fmt_size_bytes(500_000_000) == "500 MB"
    assert lms._fmt_size_bytes(19_200_000_000) == "19.20 GB"


def test_ps_model_null_falls_back_to_identifier(lms, monkeypatch):
    _mock_ps(lms, monkeypatch, [{
        "identifier": "qwen3-30b", "model": None, "status": "IDLE",
    }])
    assert lms.lms_get_ps()[0]["model"] == "qwen3-30b"
