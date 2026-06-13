"""#218: stream_health pushes the SSE-daemon gauges to the alarm engine."""
from __future__ import annotations

import stream_health


def _points_for(snap):
    return {(p["source"], p["metric_name"]): p["value"]
            for p in stream_health._to_points(snap)}


def test_to_points_includes_sse_daemon_gauges():
    snap = {
        "hostname": "mgr",
        "pool": {"active": 1, "peak": 2, "refusals": 0, "limit": 40},
        "sse_daemon_streams": 3,
        "sse_daemon_running": True,
        "agents": [],
    }
    pts = _points_for(snap)
    assert pts[("manager_streams", "sse_daemon_streams")] == 3.0
    assert pts[("manager_streams", "sse_daemon_running")] == 1.0


def test_to_points_running_false_pushes_zero():
    snap = {"hostname": "mgr", "pool": {}, "sse_daemon_streams": 0,
            "sse_daemon_running": False, "agents": []}
    pts = _points_for(snap)
    assert pts[("manager_streams", "sse_daemon_running")] == 0.0
    assert pts[("manager_streams", "sse_daemon_streams")] == 0.0


def test_to_points_omits_missing_sse_daemon_gauges():
    snap = {"hostname": "mgr", "pool": {}, "agents": []}
    pts = _points_for(snap)
    assert ("manager_streams", "sse_daemon_streams") not in pts
    assert ("manager_streams", "sse_daemon_running") not in pts
