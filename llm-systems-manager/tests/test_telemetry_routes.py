"""/api/admin/telemetry GET/PUT and the manager's crash hook wiring."""
from __future__ import annotations
import pytest

import auth
import telemetry
import manager_mod as M


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
    monkeypatch.setattr(telemetry, "_data_dir", tmp_path)
    with M.app.test_client() as c:
        yield c


def test_get_status_shape(client):
    body = client.get("/api/admin/telemetry").get_json()
    assert body["ok"] is True
    for k in ("install_report", "crash_reports", "heartbeat", "last", "pending"):
        assert k in body
    assert "endpoint" not in body


def test_put_writes_keys_through_toml_io(client, monkeypatch):
    import settings_toml_io
    written = {}
    monkeypatch.setattr(settings_toml_io, "apply_patches",
                        lambda changes, removals=None: written.update(changes))
    r = client.put("/api/admin/telemetry", json={"heartbeat": True, "crash_reports": False})
    assert r.status_code == 200
    assert written == {"manager.telemetry.heartbeat": True, "manager.telemetry.crash_reports": False}


def test_put_rejects_unknown_keys(client):
    r = client.put("/api/admin/telemetry", json={"endpoint": "https://evil"})
    assert r.status_code == 400


def test_audit_route_registered():
    assert M._audit_match("PUT", "/api/admin/telemetry")[:2] == ("config.telemetry", None)


def test_send_ok(client, monkeypatch):
    monkeypatch.setattr(telemetry, "send_now", lambda: {"ok": True, "event": "heartbeat", "sent_at": "t"})
    r = client.post("/api/admin/telemetry/send")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True and "heartbeat" in r.get_json()


def test_send_failure_is_502(client, monkeypatch):
    monkeypatch.setattr(telemetry, "send_now", lambda: {"ok": False, "event": "heartbeat", "sent_at": "t"})
    r = client.post("/api/admin/telemetry/send")
    assert r.status_code == 502 and r.get_json()["ok"] is False


def test_send_denied_without_admin(client, monkeypatch):
    monkeypatch.setattr(M, "_require_admin", lambda: ("no", 403))
    called = []
    monkeypatch.setattr(telemetry, "send_now", lambda: called.append(1))
    assert client.post("/api/admin/telemetry/send").status_code == 403
    assert not called


def test_send_audit_route_registered():
    assert M._audit_match("POST", "/api/admin/telemetry/send")[0] == "config.telemetry-send"
    assert M._AUDIT_LABELS["config.telemetry-send"] == "Sent a diagnostic report now"


def test_hot_reloader_registered():
    assert "manager.telemetry." in M._HOT_RELOADERS


def test_unhandled_route_error_reports_crash(monkeypatch):
    seen = []
    monkeypatch.setattr(telemetry, "report_exception",
                        lambda exc, tb, component: seen.append((type(exc).__name__, component)))
    with M.app.test_request_context("/x"):
        _body, status = M._log_unhandled(KeyError("x"))
    assert status == 500
    assert seen == [("KeyError", "manager")]
