# agent/tests/test_lms_reliability.py
"""#785/#786: lms ps/status read failures are distinguishable; /lms/load is
idempotent, verified, and honours the configured timeouts."""
from __future__ import annotations

import importlib.util
import json
import subprocess
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


class _Timeout(Exception):
    pass


def _load_lms():
    req = _stub_if_absent("requests", Session=lambda: None)
    if not hasattr(req, "exceptions"):
        req.exceptions = types.SimpleNamespace(Timeout=_Timeout, RequestException=Exception)
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


def _set_ctx(mod, session=None, **cfg_extra):
    cfg = types.SimpleNamespace(LMS_ENABLED=True, LMS_CMD="/usr/bin/lms",
                                AGENT_USER=None, LMS_API_URL="http://x:1235",
                                **cfg_extra)
    mod.set_context(types.SimpleNamespace(config=cfg, check_bearer=lambda *_a: None))
    mod._lms_session = session
    return session


@pytest.fixture
def lms(monkeypatch):
    mod = _load_lms()
    _set_ctx(mod)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)
    return mod


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


# ── #785: lms_get_ps ───────────────────────────────────────────────────

def test_ps_success_empty_is_an_empty_list(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output", lambda *a, **k: "[]")
    assert lms.lms_get_ps() == []


def test_ps_timeout_is_none_not_empty(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        _raise(subprocess.TimeoutExpired(["lms"], 15)))
    assert lms.lms_get_ps() is None


def test_ps_parse_error_is_none(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output", lambda *a, **k: "not json")
    assert lms.lms_get_ps() is None


def test_ps_missing_binary_is_none(lms, monkeypatch):
    monkeypatch.setattr(lms.os.path, "exists", lambda p: False)
    assert lms.lms_get_ps() is None


def test_ps_other_failure_is_none(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        _raise(subprocess.CalledProcessError(1, ["lms"])))
    assert lms.lms_get_ps() is None


def test_ps_model_prefers_the_catalog_key_over_the_instance_identifier(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output", lambda *a, **k: json.dumps(
        [{"identifier": "qwen3-30b:2", "modelKey": "qwen3-30b", "status": "IDLE"}]))
    row = lms.lms_get_ps()[0]
    assert row["model"] == "qwen3-30b" and row["identifier"] == "qwen3-30b:2"


def test_ps_endpoint_still_returns_a_list_on_failure(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        _raise(subprocess.TimeoutExpired(["lms"], 15)))
    assert lms.lms_ps_endpoint(authorization=None) == []


def test_delete_refuses_when_residency_cannot_be_verified(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        _raise(subprocess.TimeoutExpired(["lms"], 15)))
    out = lms.lms_delete_endpoint({"model": "qwen3-30b"}, authorization=None)
    assert out["ok"] is False and "cannot verify" in out["error"]


# ── #785: lms_get_status ───────────────────────────────────────────────

def test_status_success_reports_on(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        lambda *a, **k: json.dumps({"running": True, "port": 1235}))
    s = lms.lms_get_status()
    assert s["on"] is True and s["port"] == 1235 and not s.get("error")


def test_status_success_reports_off(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        lambda *a, **k: json.dumps({"running": False}))
    assert lms.lms_get_status()["on"] is False


def test_status_timeout_is_unknown_with_error(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        _raise(subprocess.TimeoutExpired(["lms"], 15)))
    s = lms.lms_get_status()
    assert s["on"] is None and "timed out" in s["error"]


def test_status_parse_error_is_unknown_with_error(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output", lambda *a, **k: "garbage")
    s = lms.lms_get_status()
    assert s["on"] is None and s["error"]


def test_status_missing_binary_is_unknown_with_error(lms, monkeypatch):
    monkeypatch.setattr(lms.os.path, "exists", lambda p: False)
    s = lms.lms_get_status()
    assert s["on"] is None and "LMS_CMD" in s["error"]


def test_status_endpoint_keeps_legacy_running_bool(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        _raise(subprocess.TimeoutExpired(["lms"], 15)))
    out = lms.lms_status_endpoint(authorization=None)
    assert out["data"]["running"] is False and out["on"] is None and out["error"]


# ── #785: lms_sample_block — what the metric sample / dashboard payload carry ──

def _cli(ps_out, status_out=json.dumps({"running": True, "port": 1235})):
    def _co(cmd, **k):
        if cmd[1] == "ps":
            if isinstance(ps_out, BaseException):
                raise ps_out
            return ps_out
        return status_out
    return _co


def test_sample_block_marks_a_good_read(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        _cli(json.dumps([{"identifier": "m", "status": "IDLE"}])))
    monkeypatch.setattr(lms, "lms_get_models", lambda: [])
    b = lms.lms_sample_block()
    assert b["ps_ok"] is True and b["ps_error"] is None
    assert [p["model"] for p in b["ps"]] == ["m"]
    assert b["server"]["on"] is True


def test_sample_block_marks_a_failed_ps_read(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output",
                        _cli(subprocess.TimeoutExpired(["lms", "ps"], 15)))
    monkeypatch.setattr(lms, "lms_get_models", lambda: [])
    b = lms.lms_sample_block()
    assert b["ps"] == []                   # consumers keep their list shape
    assert b["ps_ok"] is False and "timed out" in b["ps_error"]


def test_sample_block_names_a_parse_failure(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output", _cli("garbage"))
    monkeypatch.setattr(lms, "lms_get_models", lambda: [])
    b = lms.lms_sample_block()
    assert b["ps_ok"] is False and "parse" in b["ps_error"]


def test_sample_block_empty_ps_read_is_trustworthy(lms, monkeypatch):
    monkeypatch.setattr(lms.subprocess, "check_output", _cli("[]"))
    monkeypatch.setattr(lms, "lms_get_models", lambda: [])
    b = lms.lms_sample_block()
    assert b["ps"] == [] and b["ps_ok"] is True and b["ps_error"] is None


# ── #786: /lms/load + /lms/unload ──────────────────────────────────────

class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Session:
    """Fake requests.Session: `loaded` backs the /api/v1/models answer and is
    mutated by a successful load; `posts` records (url, json, timeout)."""

    def __init__(self, loaded=(), catalog_error=None, post_resp=None,
                 post_exc=None, load_takes_effect=True):
        self.loaded = set(loaded)
        self.catalog_error = catalog_error
        self.post_resp = post_resp or _Resp(200, {"instance_id": "m", "status": "loaded"})
        self.post_exc = post_exc
        self.load_takes_effect = load_takes_effect
        self.posts = []
        self.gets = []

    def get(self, url, timeout=None, **k):
        self.gets.append((url, timeout))
        if self.catalog_error:
            raise self.catalog_error
        models = [{"key": m, "type": "llm", "loaded_instances": []} for m in ("m", "other")]
        for m in models:
            if m["key"] in self.loaded:
                m["loaded_instances"] = [{"id": m["key"], "config": {}}]
        return _Resp(200, {"models": models})

    def post(self, url, json=None, timeout=None, **k):
        self.posts.append((url, json, timeout))
        if self.post_exc:
            raise self.post_exc
        if url.endswith("/load") and self.load_takes_effect and self.post_resp.ok:
            self.loaded.add(json["model"])
        if url.endswith("/unload"):
            self.loaded.discard(json["instance_id"])
        return self.post_resp


def test_already_loaded_model_is_not_loaded_again(lms):
    s = _set_ctx(lms, _Session(loaded={"m"}))
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is True and out["already_loaded"] is True
    assert s.posts == []


def test_load_posts_then_verifies_residency(lms):
    s = _set_ctx(lms, _Session())
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is True and out["already_loaded"] is False and out["verified"] is True
    assert [p[0].endswith("/api/v1/models/load") for p in s.posts] == [True]
    assert s.posts[0][1] == {"model": "m"}
    assert len(s.gets) == 2                      # pre-check + post-load verify


def test_load_ok_status_without_a_resident_instance_is_a_failure(lms):
    s = _set_ctx(lms, _Session(load_takes_effect=False))
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is False and "not resident" in out["error"]
    assert len(s.posts) == 1


def test_load_uses_configured_timeout(lms):
    s = _set_ctx(lms, _Session(), LMS_LOAD_TIMEOUT_S=240)
    lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert s.posts[0][2] == 240


def test_load_timeout_defaults_to_180s_without_config_field(lms):
    s = _set_ctx(lms, _Session())
    lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert s.posts[0][2] == 180


@pytest.mark.parametrize("bad", [None, "3m", 0, -5])
def test_load_timeout_falls_back_on_a_bad_config_value(lms, bad):
    s = _set_ctx(lms, _Session(), LMS_LOAD_TIMEOUT_S=bad)
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is True and s.posts[0][2] == 180


def test_load_read_timeout_is_reported_as_a_timeout(lms):
    import requests  # the real module or the test stub
    _set_ctx(lms, _Session(post_exc=requests.exceptions.Timeout("boom")))
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is False and out["timeout"] is True and "timed out" in out["error"]


def test_load_http_error_is_a_failure_with_the_body(lms):
    _set_ctx(lms, _Session(post_resp=_Resp(400, {"error": "bad"})))
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is False and out["response"] == {"error": "bad"}


def test_unreadable_catalog_falls_back_to_trusting_the_http_status(lms):
    s = _set_ctx(lms, _Session(catalog_error=RuntimeError("down")))
    out = lms.lms_load_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is True and out["verified"] is False
    assert len(s.posts) == 1


def test_unload_uses_configured_timeout(lms):
    s = _set_ctx(lms, _Session(loaded={"m"}), LMS_UNLOAD_TIMEOUT_S=45)
    out = lms.lms_unload_endpoint({"model": "m"}, authorization=None)
    assert out["ok"] is True
    assert s.posts[0][1] == {"instance_id": "m"} and s.posts[0][2] == 45


def test_unload_timeout_defaults_to_60s(lms):
    s = _set_ctx(lms, _Session(loaded={"m"}))
    lms.lms_unload_endpoint({"model": "m"}, authorization=None)
    assert s.posts[0][2] == 60
