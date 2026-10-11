"""#1257: concurrent history reads run in parallel on the bounded read pool
instead of one at a time."""
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.auth import require_management_token
from backend.api.routes import metrics
from backend.models.metrics import MetricPoint

_SLEEP_S = 0.5
_N = 4


class _SlowRepo:
    """Stand-in repository that records how many reads overlap."""

    def __init__(self):
        self.cache = None
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def get_history_rows(self, *a, **k):
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            time.sleep(_SLEEP_S)
        finally:
            with self._lock:
                self._active -= 1
        return [MetricPoint(source="system", metric_name="cpu_total", value=1.0,
                            hostname="h").to_dict()]


@pytest.fixture
def slow_repo():
    previous = getattr(metrics, "_metric_repo", None)
    repo = _SlowRepo()
    metrics.set_repository(repo)
    yield repo
    metrics.set_repository(previous)


def test_read_pool_has_more_than_one_worker():
    assert metrics._READ_WORKERS >= _N
    assert metrics._READ_POOL._max_workers == metrics._READ_WORKERS


def test_concurrent_history_reads_do_not_queue(slow_repo):
    app = FastAPI()
    app.include_router(metrics.router)
    app.dependency_overrides[require_management_token] = lambda: None
    codes = []
    with TestClient(app) as client:
        def one():
            codes.append(client.get(
                "/api/alarm/metrics/system/cpu_total?since_minutes=60").status_code)
        threads = [threading.Thread(target=one) for _ in range(_N)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total_s = time.perf_counter() - t0
    assert codes == [200] * _N
    assert slow_repo.peak == _N, f"only {slow_repo.peak} of {_N} reads overlapped"
    assert total_s < _SLEEP_S * (_N - 1), f"{_N} reads took {total_s:.2f}s"
