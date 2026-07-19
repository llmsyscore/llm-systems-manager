"""#412: alarm-engine self-restart endpoint.

The manager triggers this on containerized installs (it can't systemctl a
sibling container). It must be guarded by the management token, and the real
SIGTERM scheduler is stubbed so the test never terminates the runner.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config.unified_config import settings
from backend import alarm_engine as ae

PATH = "/api/alarm/admin/self-restart"

# Real scheduler, captured before the autouse fixture stubs it per-test.
_REAL_SCHEDULE = ae._schedule_ae_self_restart


@pytest.fixture(autouse=True)
def _stub_restart(monkeypatch):
    # Never fire a real SIGTERM during tests.
    monkeypatch.setattr(ae, "_schedule_ae_self_restart", lambda *a, **k: None)


def _set_tokens(monkeypatch, ingest="", management=""):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", ingest, raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", management, raising=False)


def _client():
    return TestClient(ae.app, raise_server_exceptions=False)


def test_denies_without_token(monkeypatch):
    _set_tokens(monkeypatch, management="mgmt-secret")
    assert _client().post(PATH).status_code == 401


def test_denies_wrong_token(monkeypatch):
    _set_tokens(monkeypatch, management="mgmt-secret")
    r = _client().post(PATH, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_accepts_management_token_and_schedules(monkeypatch):
    _set_tokens(monkeypatch, management="mgmt-secret")
    called = {}
    monkeypatch.setattr(ae, "_schedule_ae_self_restart",
                        lambda *a, **k: called.setdefault("x", True))
    r = _client().post(PATH, headers={"Authorization": "Bearer mgmt-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["restarting"] is True
    assert called.get("x") is True


def test_open_when_no_token_configured(monkeypatch):
    _set_tokens(monkeypatch, ingest="", management="")
    assert _client().post(PATH).status_code == 200


# #440: self-restart must HARD-EXIT 1, not SIGTERM. brew-services units are
# Restart=on-failure, and systemd excludes SIGTERM from on-failure restarts,
# so a self-SIGTERM'd engine stays dead. os._exit(1) is a genuine failure exit.
def test_schedule_hard_exits_nonzero(monkeypatch):
    import time
    codes = []
    monkeypatch.setattr(ae.os, "_exit", lambda c: codes.append(c))
    _REAL_SCHEDULE(delay=0)  # os._exit stubbed — thread records the code
    for _ in range(50):
        if codes:
            break
        time.sleep(0.02)
    assert codes == [1]


def test_self_restart_does_not_signal(monkeypatch):
    # Regression: the scheduler must exit, never SIGTERM itself — systemd
    # on-failure ignores SIGTERM, so a self-signal leaves the engine dead.
    import time
    exits, kills = [], []
    monkeypatch.setattr(ae.os, "_exit", lambda c: exits.append(c))
    monkeypatch.setattr(ae.os, "kill", lambda *a: kills.append(a))
    _REAL_SCHEDULE(delay=0)
    for _ in range(50):
        if exits:
            break
        time.sleep(0.02)
    assert exits == [1]
    assert kills == []
