"""Manager and agent certificates are issued with their role and re-issued when it is missing (#1161)."""
from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta, timezone

import pytest

import _pki
import agent_registry as ar
import manager_mod as M

AID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def data(monkeypatch, tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(M, "DATA_DIR", d)
    # A CA of its own: never sign test certificates with the live one.
    monkeypatch.setattr(M, "_pki_ca", None)
    monkeypatch.setattr(M.settings.manager, "tls_cert_file", "", raising=False)
    monkeypatch.setattr(M.settings.manager, "tls_key_file", "", raising=False)
    return d


def test_manager_cert_carries_the_manager_role(data):
    M._ensure_manager_server_cert()
    assert M._cert_has_role(data / "manager-tls.crt", "manager")
    assert not M._cert_has_role(data / "manager-tls.crt", "alarm_engine")


def test_manager_cert_without_role_is_reissued(data, caplog):
    M._ensure_manager_server_cert()
    ca_cert, ca_key, pki = M._pki_ensure_ca()
    pem, key = pki.sign_agent_cert(ca_cert, ca_key, agent_id="llm-systems-manager",
                                   hostname=socket.gethostname(), ip_san="127.0.0.1",
                                   extra_dns_sans=["localhost"], extra_ip_sans=["127.0.0.1"])
    (data / "manager-tls.crt").write_text(pem)
    (data / "manager-tls.key").write_text(key)
    assert not M._cert_has_role(data / "manager-tls.crt", "manager")
    with caplog.at_level(logging.INFO):
        M._ensure_manager_server_cert()
    assert any("no role name" in r.message for r in caplog.records)
    assert M._cert_has_role(data / "manager-tls.crt", "manager")


def test_cert_has_role_is_false_for_missing_or_garbage(tmp_path):
    assert M._cert_has_role(tmp_path / "absent.crt", "manager") is False
    p = tmp_path / "junk.crt"
    p.write_text("not a cert")
    assert M._cert_has_role(p, "manager") is False


def test_alarm_engine_issuer_requests_its_role():
    import inspect
    src = inspect.getsource(M._ensure_ae_server_cert)
    assert 'role="alarm_engine"' in src
    assert '_cert_has_role(crt_path, "alarm_engine")' in src


def test_registry_zone_matches_the_signer():
    assert ar._ROLE_ZONE == _pki.ROLE_ZONE


@pytest.fixture
def registry(monkeypatch):
    store = {"agents": {AID: {"agent_id": AID, "hostname": "box", "status": "approved",
                              "bind_url": "https://10.0.0.5:8082", "registered_from": "10.0.0.5",
                              "last_cert_issued_at": "2026-09-01T00:00:00+00:00"}}, "global": {}}
    monkeypatch.setattr(ar, "load_agents", lambda: store)
    monkeypatch.setattr(ar, "save_agents", lambda d: None)
    monkeypatch.setattr(ar, "_manager_host_ips", lambda: frozenset({"10.0.0.1"}))
    monkeypatch.setattr(ar._deps, "hostname", "mgr.lan")
    monkeypatch.setattr(ar._deps, "alarm_engine_url", lambda: "https://10.0.0.2:8081")
    return store


BODY = {"has_tls_cert": True, "tls_expires_at": "2099-01-01T00:00:00+00:00", "tls_san_ips": ["10.0.0.5"]}


def test_agent_without_a_role_cert_gets_one_once(registry, monkeypatch):
    calls = []
    monkeypatch.setattr(ar, "_build_and_sign_agent_cert",
                        lambda agent: calls.append(1) or {"cert_pem": "c", "key_pem": "k", "ca_pem": "a",
                                                          "expires_at": "2099-01-01T00:00:00+00:00"})
    agent = registry["agents"][AID]
    out = ar._maybe_issue_tls_bundle(agent, BODY)
    assert out and out["reason"] == "role-upgrade"
    assert registry["agents"][AID]["cert_role_sent_at"]
    assert ar._maybe_issue_tls_bundle(registry["agents"][AID], BODY) is None
    assert calls == [1]


def test_agent_sign_call_asks_for_the_agent_role(registry, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(ar._deps, "data_dir", tmp_path)

    class FakePki:
        ROLE_AGENT = "agent"

        @staticmethod
        def sign_agent_cert(ca_cert, ca_key, **kw):
            seen.update(kw)
            real = _pki.sign_agent_cert(*_pki.load_or_create_ca(ar._deps.data_dir), **kw)
            return real

        ca_bundle_pem = staticmethod(_pki.ca_bundle_pem)

    monkeypatch.setattr(ar._deps, "pki_ensure_ca", lambda: (None, None, FakePki))
    ar._build_and_sign_agent_cert(registry["agents"][AID])
    assert seen["role"] == "agent" and seen["agent_id"] == AID


def test_fixture_certificates_are_not_signed_by_the_live_ca(data):
    M._ensure_manager_server_cert()
    assert (data / "internal-ca.crt").is_file()


def _signer(monkeypatch, calls):
    monkeypatch.setattr(ar, "_build_and_sign_agent_cert",
                        lambda agent: calls.append(1) or {"cert_pem": "c", "key_pem": "k", "ca_pem": "a",
                                                          "expires_at": "2099-01-01T00:00:00+00:00"})


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def test_agent_that_reports_no_role_cert_is_sent_one_again(registry, monkeypatch):
    calls = []
    _signer(monkeypatch, calls)
    agent = registry["agents"][AID]
    agent["cert_role_sent_at"] = sent = _ago(minutes=30)
    out = ar._maybe_issue_tls_bundle(agent, {**BODY, "tls_cert_has_role": False})
    assert out and out["reason"] == "role-resend"
    assert registry["agents"][AID]["cert_role_sent_at"] != sent
    assert calls == [1]


def test_resend_waits_between_attempts(registry, monkeypatch):
    monkeypatch.setattr(ar, "_build_and_sign_agent_cert", lambda agent: pytest.fail("reissued"))
    agent = registry["agents"][AID]
    agent["cert_role_sent_at"] = _ago(minutes=2)
    assert ar._maybe_issue_tls_bundle(agent, {**BODY, "tls_cert_has_role": False}) is None


@pytest.mark.parametrize("extra", [{"tls_cert_has_role": True}, {}, {"tls_cert_has_role": None}, {"tls_cert_has_role": 0}])
def test_no_resend_unless_the_agent_says_it_lacks_the_certificate(registry, monkeypatch, extra):
    monkeypatch.setattr(ar, "_build_and_sign_agent_cert", lambda agent: pytest.fail("reissued"))
    agent = registry["agents"][AID]
    agent["cert_role_sent_at"] = _ago(minutes=30)
    assert ar._maybe_issue_tls_bundle(agent, {**BODY, **extra}) is None


@pytest.mark.parametrize("body,want", [
    ({}, {"tls_cert_has_role": None, "tls_serves_role": None}),
    ({"tls_cert_has_role": True, "tls_serves_role": False}, {"tls_cert_has_role": True, "tls_serves_role": False}),
    ({"tls_cert_has_role": "yes", "tls_serves_role": 1}, {"tls_cert_has_role": None, "tls_serves_role": None}),
])
def test_heartbeat_role_fields_are_kept_only_as_booleans(body, want):
    assert ar._hb_role_fields(body) == want


def _live(aid, host, **over):
    a = {"agent_id": aid, "hostname": host, "status": "approved", "bind_url": "https://10.0.0.5:8082",
         "cert_role_sent_at": _ago(minutes=30), "last_heartbeat_data": {}}
    a.update(over)
    return a


def test_role_warnings(monkeypatch):
    monkeypatch.setattr(ar, "agent_liveness", lambda a: a.get("_live", "live"))
    agents = [
        _live("1", "checked", tls_role_checked_at=_ago(minutes=1), last_heartbeat_data={"tls_serves_role": True}),
        _live("2", "stuck", last_heartbeat_data={"tls_cert_has_role": True, "tls_serves_role": True}),
        _live("3", "nocert", last_heartbeat_data={"tls_cert_has_role": False}),
        _live("4", "needs-restart", last_heartbeat_data={"tls_cert_has_role": True, "tls_serves_role": False}),
        _live("5", "old-agent"),
        _live("6", "just-sent", cert_role_sent_at=_ago(minutes=1), last_heartbeat_data={"tls_serves_role": True}),
        _live("7", "down", _live="down", last_heartbeat_data={"tls_serves_role": True}),
        _live("8", "pending", status="pending", last_heartbeat_data={"tls_cert_has_role": False}),
        _live("9", "plain", tls_role_checked_at=_ago(minutes=1), bind_url="http://10.0.0.5:8082"),
        _live("10", "plain-down", _live="down", tls_role_checked_at=_ago(minutes=1), bind_url="http://10.0.0.5:8082"),
    ]
    out = ar.role_warnings(agents)
    assert len(out) == 3
    assert any(w.startswith("agent plain:") and "plain-HTTP" in w for w in out)
    assert any(w.startswith("agent stuck:") and "not checked" in w for w in out)
    assert any(w.startswith("agent nocert:") and "Push CA" in w for w in out)
