"""#1232: the manager monitor skips its auth-gated probes until the agent has a token."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "llm-systems-agent.py").read_text()


def _extract(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}()"
    return m.group(0)


def _load(calls: list):
    ns = {
        "Optional": __import__("typing").Optional,
        "CONFIG": type("C", (), {"META_PERF_TIMEOUT_S": 3.0})(),
        "_probe_http_latency": lambda *a, **k: calls.append((a[1], k.get("headers"))) or 12.5,
        "_probe_manager_request_stats": lambda base, hdrs: {
            "manager_anon_requests_per_min": 3.0, "manager_anon_refused_per_min": 0.0},
    }
    exec(_extract("_probe_manager_perf"), ns)
    return ns["_probe_manager_perf"]


def test_no_token_means_no_probe_and_both_values_unavailable():
    calls: list = []
    fn = _load(calls)
    assert fn("http://mgr:5000", "") == {"manager_api_latency_ms": None,
                                          "manager_history_latency_ms": None,
                                          "manager_anon_requests_per_min": None,
                                          "manager_anon_refused_per_min": None}
    assert calls == []


def test_with_token_both_probes_run_with_the_bearer():
    calls: list = []
    fn = _load(calls)
    out = fn("http://mgr:5000", "tok")
    assert out == {"manager_api_latency_ms": 12.5, "manager_history_latency_ms": 12.5,
                   "manager_anon_requests_per_min": 3.0, "manager_anon_refused_per_min": 0.0}
    assert [u for u, _ in calls] == ["http://mgr:5000/api/metrics",
                                     "http://mgr:5000/api/history?since_minutes=60"]
    assert all(h == {"Authorization": "Bearer tok"} for _, h in calls)


def test_loop_uses_the_helper():
    loop = _extract("_meta_perf_loop")
    assert "_probe_manager_perf(" in loop
    assert "/api/metrics" not in loop
