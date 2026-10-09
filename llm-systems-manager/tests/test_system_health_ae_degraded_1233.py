"""#1233: a degraded alarm engine (InfluxDB down) stays "up" on System Health with its state passed through."""
from __future__ import annotations

import pytest

import manager_mod as M


class _AeResp:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _payload(status, influx):
    return {"status": status, "version": "v2026.10.09-5", "uptime_s": 120.0,
            "ingest_points_per_s": 40.0, "influx_writes_per_s": 0.0,
            "components": {"influxdb": influx, "influxdb_version": None, "influxdb_ping_ms": None,
                           "tls": {"enabled": False, "active": False}}}


@pytest.fixture
def admin(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    monkeypatch.setattr(M, "_alarm_engine_url", "http://ae.test:8081")
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: {"agents": {}, "global": {}})
    monkeypatch.setattr(M.proxies, "resolve_proxy_target", lambda name: None)
    monkeypatch.setattr(M, "_custom_manager_tls_files", lambda quiet=True: None)
    monkeypatch.setattr(M, "DATA_DIR", tmp_path)
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c


def _health(c, monkeypatch, payload):
    monkeypatch.setattr(M._ae_session, "get", lambda *a, **k: _AeResp(payload))
    r = c.get("/api/admin/system-health")
    assert r.status_code == 200
    return r.get_json()


def _svc(d, name):
    return next(s for s in d["services"] if s["name"] == name)


def test_degraded_engine_is_up_with_state_and_warning(admin, monkeypatch):
    d = _health(admin, monkeypatch, _payload("degraded", "unreachable: ConnectionError"))
    ae = _svc(d, "alarm_engine")
    assert ae["ok"] is True
    assert ae["state"] == "degraded"
    assert ae["version"] == "v2026.10.09-5"
    influx = _svc(d, "influxdb")
    assert influx["ok"] is False
    assert influx["state"] == "unreachable: ConnectionError"
    assert any("not being stored" in w for w in d["warnings"])


def test_ok_engine_has_ok_state_and_no_outage_warning(admin, monkeypatch):
    d = _health(admin, monkeypatch, _payload("ok", "connected"))
    ae = _svc(d, "alarm_engine")
    assert ae["ok"] is True and ae["state"] == "ok"
    assert _svc(d, "influxdb")["ok"] is True
    assert not any("not being stored" in w for w in d["warnings"])


def test_unknown_status_is_not_up(admin, monkeypatch):
    d = _health(admin, monkeypatch, _payload("starting", "connected"))
    assert _svc(d, "alarm_engine")["ok"] is False
