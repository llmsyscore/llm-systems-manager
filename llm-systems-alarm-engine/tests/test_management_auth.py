"""Auth coverage for the management surface (rules/alerts/notifications).

Builds a minimal FastAPI app from the real routers so the router-level
`require_management_token` dependency is exercised exactly as deployed.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.unified_config import settings
from backend.api import auth as ae_auth
from backend.api.routes import alerts, notifications, rules


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(rules.router)
    app.include_router(alerts.router)
    app.include_router(notifications.router)
    return TestClient(app, raise_server_exceptions=False)


def _set_tokens(monkeypatch, ingest="", management=""):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", ingest, raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", management, raising=False)


PROBES = [
    ("GET", "/api/alarm/rules"),
    ("DELETE", "/api/alarm/rules"),
    ("GET", "/api/alarm/alerts/"),
    ("POST", "/api/alarm/alerts/close-all"),
    ("GET", "/api/alarm/notifications/channels"),
]


@pytest.mark.parametrize("method,path", PROBES)
def test_denies_without_token(monkeypatch, client, method, path):
    _set_tokens(monkeypatch, ingest="", management="mgmt-secret")
    r = client.request(method, path)
    assert r.status_code == 401


@pytest.mark.parametrize("method,path", PROBES)
def test_denies_wrong_token(monkeypatch, client, method, path):
    _set_tokens(monkeypatch, ingest="ingest-secret", management="mgmt-secret")
    r = client.request(method, path, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_management_token_accepted(monkeypatch, client):
    _set_tokens(monkeypatch, ingest="ingest-secret", management="mgmt-secret")
    r = client.get("/api/alarm/rules", headers={"Authorization": "Bearer mgmt-secret"})
    assert r.status_code != 401


def test_ingest_token_rejected_when_management_set(monkeypatch, client):
    _set_tokens(monkeypatch, ingest="ingest-secret", management="mgmt-secret")
    r = client.get("/api/alarm/rules", headers={"Authorization": "Bearer ingest-secret"})
    assert r.status_code == 401


def test_ingest_fallback_when_management_unset(monkeypatch, client):
    _set_tokens(monkeypatch, ingest="ingest-secret", management="")
    r = client.get("/api/alarm/rules", headers={"Authorization": "Bearer ingest-secret"})
    assert r.status_code != 401


def test_open_when_both_unset(monkeypatch, client):
    _set_tokens(monkeypatch, ingest="", management="")
    r = client.get("/api/alarm/rules")
    assert r.status_code != 401


def test_replace_me_treated_as_unset(monkeypatch, client):
    _set_tokens(monkeypatch, ingest="ingest-secret", management="REPLACE_ME")
    r = client.get("/api/alarm/rules", headers={"Authorization": "Bearer ingest-secret"})
    assert r.status_code != 401


def test_ingest_route_rejects_management_token(monkeypatch):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", "ingest-secret", raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", "mgmt-secret", raising=False)
    with pytest.raises(Exception) as exc:
        ae_auth.require_ingest_token("Bearer mgmt-secret")
    assert getattr(exc.value, "status_code", None) == 401
    assert ae_auth.require_ingest_token("Bearer ingest-secret") is None
