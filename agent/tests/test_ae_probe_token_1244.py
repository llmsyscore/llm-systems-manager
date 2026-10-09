"""#1244: the alarm engine 24 h read probe sends the ingest bearer to the
dedicated probe route, and an auth-rejected probe logs a throttled warning."""
from __future__ import annotations

import logging
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "llm-systems-agent.py").read_text()


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^(?:def |class |@))", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}()"
    return m.group(0)


def test_loop_probes_the_dedicated_route_with_the_ingest_bearer():
    loop = _extract("_meta_perf_loop")
    assert "/api/alarm/metrics/probe?since_minutes=1440" in loop
    assert "/api/alarm/metrics/system/cpu_total" not in loop
    i = loop.index("/api/alarm/metrics/probe")
    tail = loop[i:i + 400]
    assert "_ingest_token_provider()" in loop[:i] or "_ingest_token_provider()" in tail
    assert "headers=" in tail


class _Resp:
    def __init__(self, code):
        self.status_code = code


def _load(code, logged):
    import time
    ns = {
        "Any": object, "Optional": object, "time": time, "logging": logging,
        "_post_session": type("S", (), {
            "get": lambda self, url, timeout=None, headers=None: _Resp(code),
            "post": lambda self, url, json=None, timeout=None, headers=None: _Resp(code),
        })(),
        "logger": type("L", (), {"debug": lambda *a, **k: None})(),
        "_diag_throttle": lambda key, msg, *a, **k: logged.append((key, msg % a if a else msg, k.get("level"))),
    }
    exec(_extract("_probe_auth_warn"), ns)
    exec(_extract("_probe_http_latency"), ns)
    return ns["_probe_http_latency"]


def test_auth_rejection_logs_a_throttled_warning_and_returns_no_sample():
    logged: list = []
    fn = _load(401, logged)
    assert fn("GET", "https://ae:8081/api/alarm/metrics/probe", 3.0) is None
    assert len(logged) == 1
    key, msg, level = logged[0]
    assert key.startswith("probe_auth:")
    assert "401" in msg and "/api/alarm/metrics/probe" in msg
    assert level == logging.WARNING


def test_other_failures_stay_quiet():
    logged: list = []
    assert _load(500, logged)("GET", "https://ae:8081/x", 3.0) is None
    assert logged == []
    logged2: list = []
    assert _load(200, logged2)("GET", "https://ae:8081/x", 3.0) is not None
    assert logged2 == []


def test_warning_is_suppressed_before_the_ingest_token_arrives():
    logged: list = []
    fn = _load(401, logged)
    assert fn("GET", "https://ae:8081/api/alarm/metrics/probe", 3.0, warn_auth=False) is None
    assert logged == []
    loop = _extract("_meta_perf_loop")
    assert "warn_auth=_have_ingest_token()" in loop


def test_ingest_probe_warns_only_with_the_ingest_token_held():
    body = _extract("_probe_ae_ingest")
    assert "if _have_ingest_token():" in body
    assert body.index("if _have_ingest_token():") < body.index("_probe_auth_warn(")
