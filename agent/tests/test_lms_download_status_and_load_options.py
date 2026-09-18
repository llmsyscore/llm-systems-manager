# agent/tests/test_lms_download_status_and_load_options.py
"""#1047: /lms/download/status/{job_id} proxies LM Studio's download job, and /lms/load
forwards the load-time options (context, batch, flash attention) it is given."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lms_reliability import _load_lms, _set_ctx  # noqa: E402


class _Resp:
    def __init__(self, status: int, body):
        self.status_code, self._body, self.ok, self.text = status, body, status < 400, "raw"

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.resp

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.resp


@pytest.fixture
def lms(monkeypatch):
    mod = _load_lms()
    _set_ctx(mod)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)
    return mod


def test_download_status_proxies_the_job_and_flags_a_forgotten_one(lms):
    s = _Session(_Resp(200, {"job_id": "job_1", "status": "downloading", "downloaded_bytes": 5, "total_size_bytes": 10}))
    lms._lms_session = s
    out = lms.lms_download_status_endpoint("job_1", authorization=None)
    assert out == {"ok": True, "http": 200, "response": {"job_id": "job_1", "status": "downloading", "downloaded_bytes": 5, "total_size_bytes": 10}}
    assert s.calls[0][1].endswith("/api/v1/models/download/status/job_1")
    lms._lms_session = _Session(_Resp(404, {"error": "unknown job"}))
    out = lms.lms_download_status_endpoint("job_1", authorization=None)
    assert out["ok"] is False and out["http"] == 404
    with pytest.raises(Exception):
        lms.lms_download_status_endpoint("../x", authorization=None)


def test_load_forwards_the_load_time_options_only(lms, monkeypatch):
    s = _Session(_Resp(200, {"status": "loaded", "instance_id": "i1"}))
    lms._lms_session = s
    monkeypatch.setattr(lms, "_lms_resident_instances", lambda mid: [])
    monkeypatch.setattr(lms, "_cfg_timeout", lambda *a, **k: 5)
    lms.lms_load_endpoint({"model": "qwen3.5-9b", "context_length": 32768, "eval_batch_size": 1024, "temperature": 0.2}, authorization=None)
    post = next(c for c in s.calls if c[0] == "POST")
    assert post[2]["json"] == {"model": "qwen3.5-9b", "context_length": 32768, "eval_batch_size": 1024}
