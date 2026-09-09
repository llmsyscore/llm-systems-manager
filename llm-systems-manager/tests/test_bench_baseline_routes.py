"""#882: baseline routes over a fake watcher."""
from __future__ import annotations

import bench_baseline as bb


class FakeWatcher:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"baselines": [{"run_id": "p1"}], "schedule": {"enabled": False}}

    def recheck(self, run_id=None):
        self.calls.append(run_id)
        return {"ok": True, "queued": [run_id or "all"], "skipped": []}

    def history(self, run_id, limit=20):
        self.calls.append(("h", run_id, limit))
        return [{"run_id": "r1"}]


def _client():
    from flask import Flask
    app = Flask(__name__)
    w = FakeWatcher()
    bb.register_routes(app, w)
    return app.test_client(), w


def test_list_recheck_history():
    c, w = _client()
    r = c.get("/api/benchmark/live/baselines").get_json()
    assert r["ok"] and r["baselines"][0]["run_id"] == "p1" and r["schedule"]["enabled"] is False
    assert c.post("/api/benchmark/live/baselines/recheck", json={"run_id": "p1"}).get_json()["queued"] == ["p1"]
    assert c.post("/api/benchmark/live/baselines/recheck", json={}).get_json()["queued"] == ["all"]
    assert c.post("/api/benchmark/live/baselines/recheck", json={"run_id": 5}).status_code == 400
    assert w.calls[:2] == ["p1", None]
    assert c.get("/api/benchmark/live/baselines/checks").status_code == 400
    r = c.get("/api/benchmark/live/baselines/checks?run_id=p1&limit=5").get_json()
    assert r["checks"] == [{"run_id": "r1"}] and w.calls[-1] == ("h", "p1", 5)


def test_start_thread_is_noop_under_pytest():
    assert bb.start_thread(FakeWatcher(), lambda: False) is None
