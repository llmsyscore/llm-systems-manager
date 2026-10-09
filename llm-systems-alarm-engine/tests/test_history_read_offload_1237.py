"""#1237: metric history, summary and export reads run on a one-thread read
pool, so a slow long-range read no longer stalls every other alarm engine
request, and point ids no longer read system entropy per point."""
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.auth import require_management_token
from backend.api.routes import metrics
from backend.models.metrics import MetricPoint, MetricSummary

_SLEEP_S = 0.4


class _SlowRepo:
    """Stand-in repository whose reads block for _SLEEP_S."""

    def __init__(self):
        self.cache = None
        self.started = threading.Event()

    def _slow(self):
        self.started.set()
        time.sleep(_SLEEP_S)

    def get_points(self, *a, **k):
        self._slow()
        return [MetricPoint(source="system", metric_name="cpu_total", value=1.0, hostname="h")]

    def get_summary(self, *a, **k):
        self._slow()
        return MetricSummary(source="system", metric_name="cpu_total", unit="%",
                             current_value=1.0, min_value=1.0, max_value=1.0,
                             avg_value=1.0, std_dev=0.0, p90=1.0, p95=1.0, p99=1.0,
                             data_points=1, last_updated=datetime.now(timezone.utc))


def _client(repo):
    metrics.set_repository(repo)
    app = FastAPI()
    app.include_router(metrics.router)
    app.dependency_overrides[require_management_token] = lambda: None

    @app.get("/ping")
    async def _ping():
        return {"ok": True}

    return TestClient(app)


@pytest.mark.parametrize("path", [
    "/api/alarm/metrics/system/cpu_total?since_minutes=1440",
    "/api/alarm/metrics/system/cpu_total/summary?window_minutes=60",
    "/api/alarm/metrics/export?source=system&metric_name=cpu_total",
])
def test_other_requests_answer_while_a_slow_read_runs(path):
    repo = _SlowRepo()
    # The context manager keeps one event loop for every request.
    with _client(repo) as client:
        result = {}
        t = threading.Thread(target=lambda: result.setdefault("code", client.get(path).status_code))
        t.start()
        assert repo.started.wait(2.0), "slow read never started"
        t0 = time.perf_counter()
        r = client.get("/ping")
        ping_ms = (time.perf_counter() - t0) * 1000
        t.join()
    assert r.status_code == 200
    assert result["code"] == 200
    assert ping_ms < _SLEEP_S * 1000 * 0.5, f"/ping waited {ping_ms:.0f} ms behind the read"


def test_point_ids_need_no_system_entropy(monkeypatch):
    import os

    def _boom(n):
        raise AssertionError("os.urandom called per point")
    monkeypatch.setattr(os, "urandom", _boom)
    ids = {MetricPoint(source="s", metric_name="m", value=1.0).metric_id for _ in range(500)}
    assert len(ids) == 500
    assert all(i.version == 4 for i in ids)
