"""#1071: OTLP receiver advisories on the System Health card."""
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


def _ae_payload(otlp, uptime_s=3600.0):
    comps = {"influxdb": "connected", "tls": {"enabled": False, "active": False}}
    if otlp is not None:
        comps["otlp"] = otlp
    return {"status": "ok", "version": "v2026.09.22-2", "uptime_s": uptime_s, "components": comps}


def _otlp(batches=0, last_batch_age_s=None, parse_errors=0, last_error_age_s=None):
    return {"metric_batches": batches, "trace_batches": 0, "log_batches": 0,
            "parse_errors": parse_errors, "write_errors": 0,
            "last_batch_age_s": last_batch_age_s, "last_error_age_s": last_error_age_s}


_OPENCLAW_AGENTS = {"agents": {"a1": {"status": "approved", "hostname": "claw",
                                      "capabilities": {"openclaw": True}}}, "global": {}}


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


def test_no_openclaw_stays_silent(admin, monkeypatch):
    h = _health(admin, monkeypatch, _ae_payload(_otlp()))
    assert h["advisories"] == []


def test_openclaw_agent_and_zero_batches_past_grace_advises(admin, monkeypatch):
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: _OPENCLAW_AGENTS)
    h = _health(admin, monkeypatch, _ae_payload(_otlp(), uptime_s=1200))
    (a,) = h["advisories"]
    assert "no OTLP data in 20 min" in a and "TLS certificate" in a
    assert not any("OTLP" in w for w in h["warnings"])
    assert _svc(h)["otlp"]["metric_batches"] == 0


def test_openclaw_proxy_url_also_gates(admin, monkeypatch):
    monkeypatch.setattr(M.proxies, "resolve_proxy_target",
                        lambda name: "http://claw:18789" if name == "openclaw" else None)
    assert len(_health(admin, monkeypatch, _ae_payload(_otlp()))["advisories"]) == 1


def test_zero_batches_inside_grace_is_quiet(admin, monkeypatch):
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: _OPENCLAW_AGENTS)
    assert _health(admin, monkeypatch, _ae_payload(_otlp(), uptime_s=300))["advisories"] == []


def test_recent_batches_are_quiet(admin, monkeypatch):
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: _OPENCLAW_AGENTS)
    h = _health(admin, monkeypatch, _ae_payload(_otlp(5, last_batch_age_s=60)))
    assert h["advisories"] == []


def test_stale_batches_advise(admin, monkeypatch):
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: _OPENCLAW_AGENTS)
    h = _health(admin, monkeypatch, _ae_payload(_otlp(5, last_batch_age_s=3600)))
    (a,) = h["advisories"]
    assert "for 60 min" in a and "stopped sending" in a


def test_recent_errors_advise(admin, monkeypatch):
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: _OPENCLAW_AGENTS)
    h = _health(admin, monkeypatch, _ae_payload(
        _otlp(5, last_batch_age_s=30, parse_errors=2, last_error_age_s=60)))
    (a,) = h["advisories"]
    assert "rejected OTLP data" in a and "2 errors" in a


def test_old_errors_are_quiet(admin, monkeypatch):
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: _OPENCLAW_AGENTS)
    h = _health(admin, monkeypatch, _ae_payload(
        _otlp(5, last_batch_age_s=30, parse_errors=1, last_error_age_s=7200)))
    assert h["advisories"] == []


def test_older_engine_without_otlp_block_is_quiet(admin, monkeypatch):
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: _OPENCLAW_AGENTS)
    h = _health(admin, monkeypatch, _ae_payload(None))
    assert h["advisories"] == []
    assert _svc(h)["otlp"] is None


def test_malformed_otlp_block_never_breaks_the_endpoint(admin, monkeypatch):
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: _OPENCLAW_AGENTS)
    h = _health(admin, monkeypatch, _ae_payload({"metric_batches": "lots"}, uptime_s="soon"))
    assert h["advisories"] == []


def _svc(h):
    return next(s for s in h["services"] if s["name"] == "alarm_engine")
