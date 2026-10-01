"""Manager and agent certificates are issued with their role and re-issued when it is missing (#1161)."""
from __future__ import annotations

import logging
import socket

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
