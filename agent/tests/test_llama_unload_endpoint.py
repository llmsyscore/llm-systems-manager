"""/llama/unload reports a llama-server refusal as ok:false (#730)."""
from __future__ import annotations

from types import SimpleNamespace

import test_llama_props

llama = test_llama_props.llama


class _Resp:
    def __init__(self, payload, ok=True, status_code=200, text=""):
        self._payload = payload
        self.ok, self.status_code, self.text = ok, status_code, text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _models(*rows):
    return _Resp({"data": [{"id": i, "status": {"value": s}} for i, s in rows]})


def _setup(monkeypatch, resp, models=None):
    monkeypatch.setattr(llama, "requests", SimpleNamespace(
        post=lambda url, **kw: resp,
        get=lambda url, **kw: models if models is not None else _models()))
    monkeypatch.setattr(llama, "_llama_fetch_model_entries", lambda: {
        e["id"]: e for e in (models or _models()).json()["data"]})
    monkeypatch.setattr(llama, "_llama_wait_unloaded", lambda api, **kw: True)
    monkeypatch.setattr(llama, "_llama_check_enabled", lambda: None)
    monkeypatch.setattr(llama, "_require_ctx", lambda: SimpleNamespace(
        check_bearer=lambda a: None,
        config=SimpleNamespace(LLAMA_API_URL="http://x")))


def test_http_error_from_llama_server_is_ok_false(monkeypatch):
    body = {"error": {"code": 400, "message": "model is not found"}}
    _setup(monkeypatch, _Resp(body, ok=False, status_code=400))
    out = llama.llama_unload_endpoint({"model": "nope"})
    assert out["ok"] is False
    assert out["error"] == "llama-server returned HTTP 400"
    assert out["response"] == body


def test_non_json_error_body_is_kept_raw(monkeypatch):
    _setup(monkeypatch, _Resp(ValueError("no json"), ok=False, status_code=502, text="bad gateway"))
    out = llama.llama_unload_endpoint({"model": "m1"})
    assert out["ok"] is False and out["response"] == {"raw": "bad gateway"}


def test_accepted_unload_is_ok_true(monkeypatch):
    _setup(monkeypatch, _Resp({"success": True}))
    out = llama.llama_unload_endpoint({"model": "m1"})
    assert out == {"ok": True, "response": {"success": True}}


def test_accepted_unload_with_non_json_body_still_settles(monkeypatch):
    _setup(monkeypatch, _Resp(ValueError("no json"), text=""))
    out = llama.llama_unload_endpoint({"model": "m1"})
    assert out == {"ok": True, "response": {"raw": ""}}


def test_not_running_refusal_for_an_idle_model_is_ok(monkeypatch):
    body = {"error": {"code": 400, "message": "model is not running"}}
    _setup(monkeypatch, _Resp(body, ok=False, status_code=400),
           models=_models(("m1", "unloaded"), ("m2", "loaded")))
    out = llama.llama_unload_endpoint({"model": "m1"})
    assert out == {"ok": True, "already_unloaded": True, "response": body}


def test_refusal_for_a_busy_or_unknown_model_stays_ok_false(monkeypatch):
    body = {"error": {"code": 400, "message": "model is not found"}}
    _setup(monkeypatch, _Resp(body, ok=False, status_code=400),
           models=_models(("m1", "loaded")))
    assert llama.llama_unload_endpoint({"model": "m1"})["ok"] is False
    assert llama.llama_unload_endpoint({"model": "nope"})["ok"] is False


def test_refusal_for_a_model_without_a_status_stays_ok_false(monkeypatch):
    body = {"error": {"code": 404, "message": "not found"}}
    _setup(monkeypatch, _Resp(body, ok=False, status_code=404),
           models=_Resp({"data": [{"id": "m1"}]}))    # single-model server, no status
    assert llama.llama_unload_endpoint({"model": "m1"})["ok"] is False
