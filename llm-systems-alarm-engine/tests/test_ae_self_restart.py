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


# #437: brew-services units are Restart=on-failure — main() must exit
# non-zero after a scheduled self-restart or the engine stays dead.
def test_schedule_marks_restart_pending(monkeypatch):
    monkeypatch.setattr(ae, "_restart_pending", False)
    _REAL_SCHEDULE(delay=600)  # SIGTERM thread never fires in-test
    assert ae._restart_pending is True
    assert ae._restart_exit_code() == 1


def test_exit_code_zero_without_pending_restart(monkeypatch):
    monkeypatch.setattr(ae, "_restart_pending", False)
    assert ae._restart_exit_code() == 0


def test_main_exits_nonzero_on_restart_pending():
    import inspect
    src = inspect.getsource(ae.main)
    assert "_restart_exit_code" in src and "SystemExit" in src
