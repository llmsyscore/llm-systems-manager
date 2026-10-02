"""The manager locks an agent's role on first sight and requires it afterwards (#1161)."""
from __future__ import annotations

import logging

import pytest
import requests

import agent_registry as ar
from tests.tls_servers import Pki, serve

A = "11111111-2222-3333-4444-555555555555"
B = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def net(tmp_path, monkeypatch):
    pki = Pki(tmp_path)
    url_a, stop_a = serve(*pki.leaf("a", "agent", A))
    url_plain, stop_p = serve(*pki.leaf("plain", "manager", "llm-systems-manager"))
    monkeypatch.setattr(ar._deps, "data_dir", tmp_path / "ca")
    store = {"agents": {}, "global": {}}
    monkeypatch.setattr(ar, "load_agents", lambda: store)
    monkeypatch.setattr(ar, "save_agents", lambda d: None)
    monkeypatch.setattr(ar, "agent_liveness", lambda a: "live")
    ar._role_warned.clear()
    yield store, url_a, url_plain
    stop_a()
    stop_p()


def _agent(store, aid, url, **over):
    a = {"agent_id": aid, "hostname": "h-" + aid[:4], "status": "approved", "bind_url": url,
         "registered_from": "127.0.0.1", "cert_role_sent_at": "2026-10-01T00:00:00+00:00", "token": "t"}
    a.update(over)
    store["agents"][aid] = a
    return a


def test_probe_locks_an_agent_that_serves_its_role(net):
    store, url_a, _ = net
    a = _agent(store, A, url_a)
    assert not ar.agent_role_locked(a)
    ar._role_probe_tick(store)
    assert store["agents"][A]["tls_role_checked_at"]
    assert ar.agent_role_locked(store["agents"][A])


def test_probe_leaves_an_agent_without_role_unlocked_and_warns_once(net, caplog):
    store, _, url_plain = net
    _agent(store, A, url_plain)
    with caplog.at_level(logging.WARNING):
        ar._role_probe_tick(store)
        ar._role_probe_tick(store)
    assert "tls_role_checked_at" not in store["agents"][A]
    assert sum("has no role yet" in r.message for r in caplog.records) == 1


def test_probe_ignores_unreachable_agent(net, caplog):
    store, _, _ = net
    _agent(store, A, "https://127.0.0.1:9")
    with caplog.at_level(logging.WARNING):
        ar._role_probe_tick(store)
    assert "tls_role_checked_at" not in store["agents"][A]
    assert not any("has no role yet" in r.message for r in caplog.records)


@pytest.mark.parametrize("over", [{"status": "pending"}, {"bind_url": "http://127.0.0.1:1"},
                                  {"cert_role_sent_at": None}, {"tls_role_checked_at": "2026-10-01T00:00:00+00:00"}])
def test_probe_skips_agents_that_are_not_candidates(net, monkeypatch, over):
    store, url_a, _ = net
    _agent(store, A, url_a, **over)
    monkeypatch.setattr(ar.tls_roles, "role_session", lambda name: pytest.fail("probed"))
    ar._role_probe_tick(store)


def test_agent_http_is_plain_requests_until_locked(net):
    store, url_a, _ = net
    a = _agent(store, A, url_a)
    assert ar.agent_http(a) is requests
    assert ar.agent_http(None) is requests
    a["tls_role_checked_at"] = "2026-10-01T00:00:00+00:00"
    assert isinstance(ar.agent_http(a), requests.Session)


def test_locked_agent_refuses_another_agents_certificate(net):
    store, url_a, _ = net
    b = _agent(store, B, url_a, tls_role_checked_at="2026-10-01T00:00:00+00:00")
    resp, tried, err = ar.agent_request("GET", b, "/health", timeout=5)
    assert resp is None and err


def test_locked_agent_request_succeeds_with_its_own_certificate(net):
    store, url_a, _ = net
    a = _agent(store, A, url_a, tls_role_checked_at="2026-10-01T00:00:00+00:00")
    resp, tried, err = ar.agent_request("GET", a, "/health", timeout=5)
    assert err is None and resp.ok


def test_locked_agent_is_not_dialed_over_plain_http(net, caplog):
    store, url_a, _ = net
    plain = url_a.replace("https://", "http://")
    a = _agent(store, A, plain, registered_from="127.0.0.1", tls_role_checked_at="2026-10-01T00:00:00+00:00")
    with caplog.at_level(logging.WARNING):
        resp, tried, err = ar.agent_request("GET", a, "/health", timeout=5)
    assert resp is None and err and tried == [plain + "/health"]
    assert any("plain HTTP" in r.getMessage() for r in caplog.records)


def test_registry_role_name_matches_the_signer():
    import _pki
    assert ar._agent_role_name({"agent_id": A.upper()}) == _pki.role_name("agent", A.upper())

