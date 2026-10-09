"""#1234: a status poll that fails while a newly approved agent restarts for
HTTPS is logged at info, not warning; the window is recorded when the first
TLS bundle is issued."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests

import agent_registry as ar

AID = "11111111-0000-0000-0000-000000000000"


def _agent(**extra) -> dict:
    return {"agent_id": AID, "hostname": "agent-host", "bind_url": "http://agent-host:8082",
            "registered_from": "192.0.2.7", "token": "tok", **extra}


def _iso(delta_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


@pytest.fixture(autouse=True)
def _dead_agent(monkeypatch):
    def fake_request(method, url, **kw):
        raise requests.ConnectionError("Connection refused")
    monkeypatch.setattr(ar.requests, "request", fake_request)
    with ar._dial_pref_lock:
        ar._dial_pref.clear()


def _levels(caplog) -> set:
    return {r.levelno for r in caplog.records if "agent_request" in r.getMessage()}


def test_failure_inside_the_restart_window_logs_info(caplog):
    with caplog.at_level(logging.DEBUG, logger=ar.log.name):
        r, _tried, err = ar.agent_request("GET", _agent(tls_restart_until=_iso(60)), "/status", timeout=1)
    assert r is None and err
    assert _levels(caplog) == {logging.INFO}


def test_failure_after_the_window_warns(caplog):
    with caplog.at_level(logging.DEBUG, logger=ar.log.name):
        ar.agent_request("GET", _agent(tls_restart_until=_iso(-5)), "/status", timeout=1)
    assert logging.WARNING in _levels(caplog)


def test_failure_without_a_window_warns(caplog):
    with caplog.at_level(logging.DEBUG, logger=ar.log.name):
        ar.agent_request("GET", _agent(), "/status", timeout=1)
    assert logging.WARNING in _levels(caplog)


def test_unreadable_window_is_ignored():
    assert ar._in_planned_restart({"tls_restart_until": "soon"}) is False
    assert ar._in_planned_restart({}) is False
    assert ar._in_planned_restart({"tls_restart_until": _iso(30)}) is True


@pytest.fixture
def registry(monkeypatch):
    store = {"agents": {AID: _agent(status="approved")}, "global": {}}
    monkeypatch.setattr(ar, "load_agents", lambda: store)
    monkeypatch.setattr(ar, "save_agents", lambda d: None)
    monkeypatch.setattr(ar, "_build_and_sign_agent_cert",
                        lambda agent: {"cert_pem": "c", "key_pem": "k", "ca_pem": "a",
                                       "expires_at": "2099-01-01T00:00:00+00:00"})
    return store


def test_first_issue_records_the_restart_window(registry):
    out = ar._maybe_issue_tls_bundle(registry["agents"][AID], {"has_tls_cert": False})
    assert out and out["reason"] == "first-issue"
    assert ar._in_planned_restart(registry["agents"][AID])


def test_rotation_on_an_https_agent_records_no_window(registry):
    a = registry["agents"][AID]
    a["bind_url"] = "https://agent-host:8082"
    a["cert_role_sent_at"] = "2026-10-01T00:00:00+00:00"
    out = ar._maybe_issue_tls_bundle(a, {"has_tls_cert": True, "tls_expires_at": _iso(3600)})
    assert out and out["reason"] == "rotation"
    assert "tls_restart_until" not in a


def test_any_bundle_for_an_http_agent_records_the_window(registry):
    a = registry["agents"][AID]
    a["cert_role_sent_at"] = "2026-10-01T00:00:00+00:00"
    out = ar._maybe_issue_tls_bundle(a, {"has_tls_cert": True, "tls_expires_at": _iso(3600)})
    assert out and out["reason"] == "rotation"
    assert ar._in_planned_restart(a)
