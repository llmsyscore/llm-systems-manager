"""#1244: the agent's long-range read probe hits a dedicated route gated by
the ingest token, which runs the history read path and returns only a point
count and the server-side read time."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.unified_config import settings
from backend.api.routes import metrics
from backend.models.metrics import MetricPoint

INGEST = "ingest-secret"
MGMT = "mgmt-secret"


class _Repo:
    def __init__(self):
        self.calls = []

    def get_history_rows(self, source, metric_name, since=None, limit=None,
                         hostname=None, agg="mean", max_points=0):
        self.calls.append(dict(source=source, metric_name=metric_name, since=since,
                               limit=limit, hostname=hostname, agg=agg, max_points=max_points))
        return [MetricPoint(source=source, metric_name=metric_name, value=float(i),
                            hostname="h").to_dict() for i in range(3)]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", INGEST, raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", MGMT, raising=False)
    repo = _Repo()
    metrics.set_repository(repo)
    app = FastAPI()
    app.include_router(metrics.router)
    c = TestClient(app, raise_server_exceptions=False)
    c.repo = repo
    return c


def test_anonymous_and_management_bearer_are_denied(client):
    assert client.get("/api/alarm/metrics/probe").status_code == 401
    r = client.get("/api/alarm/metrics/probe", headers={"Authorization": f"Bearer {MGMT}"})
    assert r.status_code == 401


def test_ingest_bearer_runs_the_24h_read_and_returns_only_counts(client):
    before = datetime.now(timezone.utc)
    r = client.get("/api/alarm/metrics/probe", headers={"Authorization": f"Bearer {INGEST}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"points", "read_ms"}
    assert body["points"] == 3
    assert isinstance(body["read_ms"], (int, float)) and body["read_ms"] >= 0
    call = client.repo.calls[0]
    assert (call["source"], call["metric_name"]) == ("system", "cpu_total")
    assert call["hostname"] is None
    assert call["max_points"] == metrics._HIST_MAX_POINTS_DEFAULT
    assert abs((before - call["since"]) - timedelta(minutes=1440)) < timedelta(seconds=5)


def test_window_is_bounded_by_the_history_limit(client):
    hdr = {"Authorization": f"Bearer {INGEST}"}
    assert client.get("/api/alarm/metrics/probe?since_minutes=60", headers=hdr).status_code == 200
    assert client.repo.calls[-1]["since"] > datetime.now(timezone.utc) - timedelta(minutes=61)
    too_long = metrics._HIST_SINCE_MAX + 1
    assert client.get(f"/api/alarm/metrics/probe?since_minutes={too_long}", headers=hdr).status_code == 422
