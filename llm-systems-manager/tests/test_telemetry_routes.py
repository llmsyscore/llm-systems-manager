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
    for k in ("install_report", "crash_reports", "checkin", "heartbeat", "do_not_track", "last", "pending"):
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


def test_send_busy_is_409(client, monkeypatch):
    monkeypatch.setattr(telemetry, "send_now", lambda: {"ok": False, "error": "busy"})
    r = client.post("/api/admin/telemetry/send")
    assert r.status_code == 409 and r.get_json()["error"] == "busy"


def test_operator_reads_switches_and_last_event_without_the_payload(client, monkeypatch):
    monkeypatch.setattr(auth, "effective_role", lambda: "operator")
    monkeypatch.setattr(telemetry, "status", lambda: {
        "install_report": True, "crash_reports": True, "checkin": False, "heartbeat": False, "pending": False,
        "do_not_track": False,
        "last": {"event": "install", "sent_at": "t", "payload": {"os": "debian", "cpu_model": "secret"}}})
    r = client.get("/api/admin/telemetry")
    d = r.get_json()
    assert r.status_code == 200 and d["read_only"] is True and d["heartbeat"] is False and d["do_not_track"] is False
    assert d["last"] == {"event": "install", "sent_at": "t"} and "secret" not in r.get_data(as_text=True)
    assert client.put("/api/admin/telemetry", json={"heartbeat": True}).status_code == 403


def test_operator_gate_lets_only_the_telemetry_get_through():
    with M.app.test_request_context("/api/admin/telemetry", method="GET"):
        assert auth._operator_denied("/api/admin/telemetry") is False
    with M.app.test_request_context("/api/admin/telemetry", method="PUT"):
        assert auth._operator_denied("/api/admin/telemetry") is True
    with M.app.test_request_context("/api/admin/telemetry/send", method="POST"):
        assert auth._operator_denied("/api/admin/telemetry/send") is True


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


def test_put_checkin_writes_its_key(client, monkeypatch):
    import settings_toml_io
    written = {}
    monkeypatch.setattr(settings_toml_io, "apply_patches",
                        lambda changes, removals=None: written.update(changes))
    assert client.put("/api/admin/telemetry", json={"checkin": True}).status_code == 200
    assert written == {"manager.telemetry.checkin": True}


def test_send_under_do_not_track_is_403(client, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    r = client.post("/api/admin/telemetry/send")
    assert r.status_code == 403 and r.get_json()["error"] == "do_not_track"
