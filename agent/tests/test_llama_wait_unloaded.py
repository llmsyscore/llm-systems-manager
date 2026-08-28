"""#727: /llama/load waits for the displaced instance to leave before loading."""
from __future__ import annotations

import test_llama_props

llama = test_llama_props.llama


class _Resp:
    def __init__(self, statuses):
        self._statuses = statuses

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


def test_unreachable_server_counts_as_settled(monkeypatch):
    _clock(monkeypatch)
    def _boom(url, **kw):
        raise ConnectionError("down")
    monkeypatch.setattr(llama.requests, "get", _boom)
    assert llama._llama_wait_unloaded("http://x", timeout_s=3.0) is True
