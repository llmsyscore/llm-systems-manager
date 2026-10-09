"""#1216: the manager monitor reads the anonymous-request counters with its bearer
and reports them as self-monitor values, None when the read fails."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "llm-systems-agent.py").read_text()


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}()"
    return m.group(0)


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        return self._body


def _load(calls: list, code=200, body=None):
    import logging
    ns = {
        "Optional": __import__("typing").Optional, "Any": object, "logging": logging,
        "logger": logging.getLogger("t"),
        "CONFIG": type("C", (), {"META_PERF_TIMEOUT_S": 3.0})(),
        "_probe_http_latency": lambda *a, **k: 12.5,
        "_post_session": type("S", (), {
            "get": lambda self, url, timeout=None, headers=None: calls.append((url, headers)) or _Resp(code, body),
        })(),
    }
    exec(_extract("_probe_manager_request_stats"), ns)
    exec(_extract("_probe_manager_perf"), ns)
    return ns["_probe_manager_perf"]


def test_counters_are_read_with_the_bearer():
    calls: list = []
    fn = _load(calls, body={"ok": True, "anon_requests_per_min": 7, "anon_refused_per_min": 2})
    out = fn("http://mgr:5000", "tok")
    assert out["manager_anon_requests_per_min"] == 7.0
    assert out["manager_anon_refused_per_min"] == 2.0
    assert calls == [("http://mgr:5000/api/manager/request-stats", {"Authorization": "Bearer tok"})]


def test_no_token_means_no_read():
    calls: list = []
    out = _load(calls)("http://mgr:5000", "")
    assert out["manager_anon_requests_per_min"] is None
    assert out["manager_anon_refused_per_min"] is None
    assert calls == []


class _BadResp:
    status_code = 200

    def json(self):
        raise ValueError("not json")


def test_a_body_that_is_not_json_reports_none():
    import logging
    ns = {"Optional": __import__("typing").Optional, "logging": logging, "logger": logging.getLogger("t"),
          "CONFIG": type("C", (), {"META_PERF_TIMEOUT_S": 3.0})(),
          "_post_session": type("S", (), {"get": lambda self, url, timeout=None, headers=None: _BadResp()})()}
    exec(_extract("_probe_manager_request_stats"), ns)
    out = ns["_probe_manager_request_stats"]("http://mgr:5000", {})
    assert out == {"manager_anon_requests_per_min": None, "manager_anon_refused_per_min": None}


def test_an_old_manager_without_the_route_reports_none():
    out = _load([], code=404, body={})("http://mgr:5000", "tok")
    assert out["manager_anon_requests_per_min"] is None
    assert out["manager_anon_refused_per_min"] is None
