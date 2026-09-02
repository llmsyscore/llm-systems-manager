"""#797: /health rate counters — ingest points/s, influx writes/s, active alerts."""
from __future__ import annotations

import asyncio

import pytest

from backend import alarm_engine as ae
from backend.rate_counter import RateCounter


@pytest.fixture(autouse=True)
def _reset_counters():
    ae.INGEST_POINTS.reset()
    ae.INFLUX_WRITES.reset()
    yield
    ae.INGEST_POINTS.reset()
    ae.INFLUX_WRITES.reset()


def test_rate_counter_windows_and_prunes():
    rc = RateCounter(60.0)
    rc.add(30, now=1000.0)
    rc.add(30, now=1001.0)
    assert rc.total(now=1002.0) == 60
    assert rc.per_s(now=1002.0) == pytest.approx(1.0)
    # Everything older than the window falls out.
    assert rc.total(now=1200.0) == 0
    assert rc.per_s(now=1200.0) == 0.0


def test_rate_counter_ignores_non_positive():
    rc = RateCounter(60.0)
    rc.add(0)
    rc.add(-5)
    assert rc.total() == 0


def test_health_reports_counters(monkeypatch):
    monkeypatch.setattr(ae.settings.influxdb, "host", "", raising=False)
    ae.INGEST_POINTS.add(120)
    ae.INFLUX_WRITES.add(60)
    body = asyncio.run(ae.health_check())
    assert body["ingest_points_per_s"] == pytest.approx(2.0)
    assert body["influx_writes_per_s"] == pytest.approx(1.0)
    assert body["active_alerts"] == 0
    assert body["version"] == ae.__version__


def test_health_active_alerts_from_alert_manager(monkeypatch):
    monkeypatch.setattr(ae.settings.influxdb, "host", "", raising=False)

    class _AM:
        def get_alert_stats(self):
            return {"active": 4, "total": 9}

    monkeypatch.setattr(ae, "alert_manager", _AM())
    body = asyncio.run(ae.health_check())
    assert body["active_alerts"] == 4


def test_active_alert_count_survives_a_broken_manager(monkeypatch):
    class _AM:
        def get_alert_stats(self):
            raise RuntimeError("db gone")

    monkeypatch.setattr(ae, "alert_manager", _AM())
    assert ae._active_alert_count() == 0
    monkeypatch.setattr(ae, "alert_manager", None)
    assert ae._active_alert_count() == 0


class _FakeRepo:
    def __init__(self):
        self.created = []

    def create(self, point):
        self.created.append(point)
        return point

    def create_batch(self, points):
        self.created.extend(points)
        return len(points)


def test_batch_route_counts_accepted_points():
    from backend.api.routes import metrics as m
    from backend.models.metrics import MetricBatchCreate, MetricPoint

    pts = [MetricPoint(source="cpu", metric_name="usage_percent", value=float(i))
           for i in range(3)]
    batch = MetricBatchCreate(metrics=pts)
    asyncio.run(m.ingest_metric_batch(batch=batch, metric_repo=_FakeRepo(),
                                      _auth=None))
    assert ae.INGEST_POINTS.total() == 3


def test_single_and_raw_routes_count_points():
    from backend.api.routes import metrics as m
    from backend.models.metrics import MetricPoint

    asyncio.run(m.ingest_metric(
        point=MetricPoint(source="cpu", metric_name="usage_percent", value=1.0),
        metric_repo=_FakeRepo(), _auth=None))
    assert ae.INGEST_POINTS.total() == 1
    out = asyncio.run(m.ingest_raw_batch(
        payload={"host": "h1", "samples": [{"cpu": {"usage_percent": 5.0}}]},
        metric_repo=_FakeRepo(), _auth=None))
    assert out["points_written"] >= 1
    assert ae.INGEST_POINTS.total() == 1 + out["points_written"]
