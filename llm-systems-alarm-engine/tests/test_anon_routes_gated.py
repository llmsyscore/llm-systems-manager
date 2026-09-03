"""Every alarm-engine data/admin route denies anonymous callers once a
management token is configured (#826)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config.unified_config import settings
from backend import alarm_engine as ae

TOKEN = "mgmt-secret"
HDR = {"Authorization": f"Bearer {TOKEN}"}

GATED = [
    ("POST", "/api/alarm/admin/export"),
    ("POST", "/api/alarm/admin/import/preview"),
    ("POST", "/api/alarm/admin/import/apply"),
    ("GET", "/api/alarm/dbstats/sqlite"),
    ("GET", "/api/alarm/metrics"),
    ("GET", "/api/alarm/metrics/export?source=system&metric_name=cpu_total"),
    ("GET", "/api/alarm/metrics/system/cpu_total"),
    ("GET", "/api/alarm/metrics/system/cpu_total/summary"),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", "", raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", TOKEN, raising=False)
    return TestClient(ae.app, raise_server_exceptions=False)


@pytest.mark.parametrize("method,path", GATED)
def test_anonymous_request_is_denied_before_body_parsing(client, method, path):
    r = client.request(method, path)
    assert r.status_code == 401, (method, path, r.status_code, r.text[:120])


@pytest.mark.parametrize("method,path", GATED)
def test_wrong_bearer_is_denied(client, method, path):
    r = client.request(method, path, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401, (method, path, r.status_code)


def test_management_bearer_passes_the_gate(client):
    # dbstats needs no body; a 200 proves the dependency accepts the token.
    r = client.get("/api/alarm/dbstats/sqlite", headers=HDR)
    assert r.status_code == 200, r.text[:120]
    # import/preview reaches body validation (422 = missing file), not the gate.
    r = client.post("/api/alarm/admin/import/preview", headers=HDR)
    assert r.status_code == 422
