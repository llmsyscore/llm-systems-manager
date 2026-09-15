"""#966: the vLLM and LMS lifecycle endpoints ask the llama collector for an early reconcile."""
from __future__ import annotations

import types

import pytest

from tests._vllm_load import load_vllm
from tests.test_lms_reliability import _Resp, _Session, _load_lms, _set_ctx


class _Proc:
    returncode, stdout, stderr = 0, "", ""


@pytest.fixture
def vllm(monkeypatch):
    mod = load_vllm()
    mod.set_context(types.SimpleNamespace(
        config=types.SimpleNamespace(VLLM_ENABLED=True, VLLM_SYSTEMD_UNIT="vllm.service",
                                     VLLM_API_URL="http://x:8000", VLLM_LORA_ENABLED=True),
        check_bearer=lambda *a, **k: None))
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Proc())
    return mod


@pytest.fixture
def lms(monkeypatch):
    mod = _load_lms()
    _set_ctx(mod, _Session(loaded={"m"}))
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mod, "_lms_run_cli", lambda args, timeout=20: (0, "ok"))
    return mod


def _count(mod, monkeypatch):
    hits = []
    monkeypatch.setattr(mod, "reconcile_now", lambda: hits.append(1))
    return hits


def test_vllm_server_stop_reconciles(vllm, monkeypatch):
    hits = _count(vllm, monkeypatch)
    assert vllm.vllm_server_stop_endpoint(None)["ok"] is True
    assert hits == [1]


def test_vllm_lora_load_reconciles(vllm, monkeypatch):
    hits = _count(vllm, monkeypatch)
    monkeypatch.setattr(vllm, "_get_session",
                        lambda: types.SimpleNamespace(post=lambda *a, **k: _Resp(200, {"ok": True})))
    out = vllm.vllm_lora_load({"lora_name": "a", "lora_path": "/tmp/a"}, None)
    assert out["ok"] is True and hits == [1]


def test_vllm_lora_unload_reconciles_on_a_transport_error(vllm, monkeypatch):
    hits = _count(vllm, monkeypatch)

    def _boom():
        raise RuntimeError("no session")
    monkeypatch.setattr(vllm, "_get_session", _boom)
    out = vllm.vllm_lora_unload({"lora_name": "a"}, None)
    assert out["ok"] is False and hits == [1]


def test_vllm_lora_guard_rejects_before_any_reconcile(vllm, monkeypatch):
    hits = _count(vllm, monkeypatch)
    vllm._require_ctx().config.VLLM_LORA_ENABLED = False
    with pytest.raises(Exception):
        vllm.vllm_lora_load({"lora_name": "a", "lora_path": "/tmp/a"}, None)
    assert hits == []


def test_lms_server_start_reconciles(lms, monkeypatch):
    hits = _count(lms, monkeypatch)
    assert lms.lms_server_start_endpoint(None)["ok"] is True
    assert hits == [1]


def test_lms_server_restart_reconciles(lms, monkeypatch):
    hits = _count(lms, monkeypatch)
    monkeypatch.setattr(lms.time, "sleep", lambda s: None)
    assert lms.lms_server_restart_endpoint(None)["ok"] is True
    assert hits == [1]


def test_lms_unload_reconciles(lms, monkeypatch):
    hits = _count(lms, monkeypatch)
    out = lms.lms_unload_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is True and hits == [1]


def test_lms_load_reconciles(lms, monkeypatch):
    _set_ctx(lms, _Session())
    hits = _count(lms, monkeypatch)
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is True and hits == [1]


def test_lms_load_reconciles_on_a_timeout(lms, monkeypatch):
    import requests
    _set_ctx(lms, _Session(post_exc=requests.exceptions.Timeout("boom")))
    hits = _count(lms, monkeypatch)
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is False and out["timeout"] is True and hits == [1]


def test_lms_load_already_resident_does_not_reconcile(lms, monkeypatch):
    hits = _count(lms, monkeypatch)
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["already_loaded"] is True and hits == []


def test_the_wrappers_delegate_to_the_llama_hook(vllm, lms):
    """Both wrappers resolve providers.llama lazily so the provider import cycle stays out."""
    import inspect
    for mod in (vllm, lms):
        src = inspect.getsource(mod.reconcile_now)
        assert "from . import llama" in src and "_llama.reconcile_now()" in src
