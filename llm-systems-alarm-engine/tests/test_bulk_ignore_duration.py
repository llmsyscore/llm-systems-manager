"""#334: the bulk /alerts/bulk route forwards a duration_hours to ignore."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.unified_config import settings
from backend.api.routes import alerts
from backend.api.routes.alerts import get_alert_mgr

BULK = "/api/alarm/alerts/bulk"


class FakeMgr:
    def __init__(self):
        self.ignore_calls = []
        self.ack_calls = []

    def ignore_alert(self, alert_id, duration_hours=24):
        self.ignore_calls.append((str(alert_id), duration_hours))
        return True

    def mark_as_read(self, alert_id):
        self.ack_calls.append(str(alert_id))
        return True

    def close_alert(self, alert_id):
        return True


@pytest.fixture
def fake_mgr():
    return FakeMgr()


@pytest.fixture
def client(monkeypatch, fake_mgr):
    # Open the management surface so the router-level auth doesn't 401.
    monkeypatch.setattr(settings.alarm_engine, "management_token", "", raising=False)
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", "", raising=False)
    app = FastAPI()
    app.include_router(alerts.router)
    app.dependency_overrides[get_alert_mgr] = lambda: fake_mgr
    return TestClient(app, raise_server_exceptions=False)


def test_bulk_ignore_forwards_duration(client, fake_mgr):
    r = client.post(BULK, json={"alert_ids": ["a", "b"], "action": "ignore",
                                "duration_hours": 72})
    assert r.status_code == 200, r.text
    assert len(fake_mgr.ignore_calls) == 2
    assert all(dh == 72 for _, dh in fake_mgr.ignore_calls)


def test_bulk_ignore_defaults_to_24(client, fake_mgr):
    r = client.post(BULK, json={"alert_ids": ["a"], "action": "ignore"})
    assert r.status_code == 200
    assert fake_mgr.ignore_calls == [("a", 24)]


def test_bulk_ignore_clamps_out_of_range(client, fake_mgr):
    client.post(BULK, json={"alert_ids": ["a"], "action": "ignore",
                            "duration_hours": 99999})
    client.post(BULK, json={"alert_ids": ["b"], "action": "ignore",
                            "duration_hours": 0})
    client.post(BULK, json={"alert_ids": ["c"], "action": "ignore",
                            "duration_hours": "nonsense"})
    assert fake_mgr.ignore_calls == [("a", 720), ("b", 1), ("c", 24)]


def test_bulk_acknowledge_ignores_duration(client, fake_mgr):
    r = client.post(BULK, json={"alert_ids": ["a"], "action": "acknowledge",
                                "duration_hours": 99})
    assert r.status_code == 200
    assert fake_mgr.ack_calls == ["a"]
    assert fake_mgr.ignore_calls == []
