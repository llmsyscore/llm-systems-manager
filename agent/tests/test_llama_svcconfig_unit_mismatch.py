# agent/tests/test_llama_svcconfig_unit_mismatch.py
"""#315: llama_svcconfig_post refuses to invoke the root wrapper when the
wrapper's baked UNIT_PATH differs from the configured llama unit."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_AGENT_ROOT = Path(__file__).resolve().parent.parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))


def _stub_module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules.setdefault(name, mod)
    return sys.modules[name]


def _module_available(name: str) -> bool:
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


if not _module_available("requests"):
    _stub_module("requests", get=None, Session=object)
if not _module_available("fastapi"):
    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail)
            self.status_code = status_code
    _stub_module(
        "fastapi",
        Header=lambda *a, **k: None, HTTPException=_HTTPException,
        Query=lambda *a, **k: None, Request=object,
    )
    _stub_module("fastapi.responses", Response=object, StreamingResponse=object)
    _stub_module("starlette")
    _stub_module("starlette.concurrency", run_in_threadpool=lambda f, *a, **k: f(*a, **k))

if not _module_available("psutil"):
    _collectors = _stub_module("collectors")
    _collectors.__path__ = []
    _stub_module("collectors.gpu", collect_gpu=lambda: {})

_pkg = types.ModuleType("providers")
_pkg.__path__ = [str(_AGENT_ROOT / "providers")]
sys.modules.setdefault("providers", _pkg)

_spec = importlib.util.spec_from_file_location(
    "providers.llama", _AGENT_ROOT / "providers" / "llama.py")
llama = importlib.util.module_from_spec(_spec)
sys.modules["providers.llama"] = llama
_spec.loader.exec_module(llama)


@pytest.fixture
def ctx():
    cfg = SimpleNamespace(
        LLAMA_ENABLED=True,
        LLAMA_SYSTEMD_UNIT="new-llama.service",
    )
    context = SimpleNamespace(config=cfg, check_bearer=lambda *_: None)
    llama.set_context(context)
    return context


def _wrapper_file(tmp_path, unit_path):
    wf = tmp_path / "llm-svcconfig-apply"
    wf.write_text(f"#!/usr/bin/env bash\n# SVCCONFIG_WRAPPER_VERSION=1\nUNIT_PATH='{unit_path}'\n")
    return wf


BODY = {"binary": "/usr/bin/llama-server", "args": [{"flag": "--port", "value": "8080", "bool": False}]}


def test_post_refuses_on_baked_unit_mismatch(ctx, tmp_path, monkeypatch):
    wf = _wrapper_file(tmp_path, "/etc/systemd/system/old-llama.service")
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", str(wf))
    monkeypatch.setattr(llama.subprocess, "run",
                        lambda *a, **k: pytest.fail("wrapper must not be invoked on mismatch"))
    r = llama.llama_svcconfig_post(dict(BODY))
    assert r["ok"] is False
    assert "baked for" in r["error"] and "new-llama.service" in r["error"]


def test_post_proceeds_when_baked_unit_matches(ctx, tmp_path, monkeypatch):
    wf = _wrapper_file(tmp_path, "/etc/systemd/system/new-llama.service")
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", str(wf))
    calls = []
    monkeypatch.setattr(llama.subprocess, "run",
                        lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stderr=b""))
    r = llama.llama_svcconfig_post(dict(BODY))
    assert r == {"ok": True}
    assert len(calls) == 1


def test_post_fails_safe_when_wrapper_missing(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", str(tmp_path / "nonexistent"))
    monkeypatch.setattr(llama.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stderr=b"sudo: a password is required"))
    r = llama.llama_svcconfig_post(dict(BODY))
    assert r["ok"] is False


def test_baked_path_reader_handles_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(llama, "_SVCCONFIG_WRAPPER", str(tmp_path / "nope"))
    assert llama._svcconfig_wrapper_baked_unit_path() == ""
