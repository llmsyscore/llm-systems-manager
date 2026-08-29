"""/llama/load waits for the displaced instance to leave before loading."""
from __future__ import annotations

import test_llama_props

llama = test_llama_props.llama


class _Resp:
    def __init__(self, statuses, ok=True, status_code=200):
        self._statuses = statuses
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return {"data": [{"id": f"m{i}", "status": {"value": s}}
                         for i, s in enumerate(self._statuses)]}


def _clock(monkeypatch):
    now = {"t": 0.0}
    monkeypatch.setattr(llama.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(llama.time, "sleep", lambda s: now.__setitem__("t", now["t"] + s))
    return now


def test_returns_once_no_instance_is_loaded_or_loading(monkeypatch):
    _clock(monkeypatch)
    polls = iter([["loading"], ["loading"], ["unloaded", "unloaded"]])
    calls = []
    monkeypatch.setattr(llama.requests, "get",
                        lambda url, **kw: (calls.append(url), _Resp(next(polls)))[1])
    assert llama._llama_wait_unloaded("http://x", timeout_s=30.0, poll_s=0.5) is True
    assert len(calls) == 3


def test_times_out_while_an_instance_still_holds_the_slot(monkeypatch):
    now = _clock(monkeypatch)
    monkeypatch.setattr(llama.requests, "get", lambda url, **kw: _Resp(["sleeping"]))
    assert llama._llama_wait_unloaded("http://x", timeout_s=3.0, poll_s=1.0) is False
    assert now["t"] >= 3.0


def test_failed_poll_counts_as_busy_until_the_deadline(monkeypatch):
    now = _clock(monkeypatch)
    def _boom(url, **kw):
        raise ConnectionError("down")
    monkeypatch.setattr(llama.requests, "get", _boom)
    assert llama._llama_wait_unloaded("http://x", timeout_s=3.0, poll_s=1.0) is False
    assert now["t"] >= 3.0


def test_transient_poll_failure_then_settled(monkeypatch):
    _clock(monkeypatch)
    polls = iter([ConnectionError("blip"), _Resp(["unloaded"])])
    def _get(url, **kw):
        r = next(polls)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(llama.requests, "get", _get)
    assert llama._llama_wait_unloaded("http://x", timeout_s=3.0, poll_s=0.5) is True


def test_error_status_counts_as_busy(monkeypatch):
    now = _clock(monkeypatch)
    monkeypatch.setattr(llama.requests, "get",
                        lambda url, **kw: _Resp([], ok=False, status_code=500))
    assert llama._llama_wait_unloaded("http://x", timeout_s=2.0, poll_s=1.0) is False
    assert now["t"] >= 2.0


def test_poll_timeout_never_exceeds_the_remaining_budget(monkeypatch):
    _clock(monkeypatch)
    seen = []
    monkeypatch.setattr(llama.requests, "get",
                        lambda url, **kw: (seen.append(kw.get("timeout")), _Resp(["loading"]))[1])
    llama._llama_wait_unloaded("http://x", timeout_s=2.0, poll_s=1.0)
    assert all(t <= 5.0 for t in seen) and seen[-1] <= 1.0
