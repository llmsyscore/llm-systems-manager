"""The role adapter accepts only the expected role; the lock file cannot be downgraded (#1161)."""
from __future__ import annotations

import json
import logging

import pytest
import requests

import _pki
import tls_roles
from tests.tls_servers import Pki, connect_proxy, serve

A = "11111111-2222-3333-4444-555555555555"
B = "99999999-8888-7777-6666-555555555555"
ROLES = ("alarm_engine",)


@pytest.fixture(scope="module")
def net(tmp_path_factory):
    pki = Pki(tmp_path_factory.mktemp("pki"))
    stops, urls = [], {}
    for label, role, aid in (("mgr", "manager", "llm-systems-manager"),
                             ("ae", "alarm_engine", "llm-systems-alarm-engine"),
                             ("a", "agent", A), ("b", "agent", B)):
        urls[label], stop = serve(*pki.leaf(label, role, aid))
        stops.append(stop)
    yield pki, urls
    for s in stops:
        s()


def _get(net, label, name):
    pki, urls = net
    return tls_roles.role_session(name).get(urls[label] + "/health", verify=pki.ca_file, timeout=5)


def test_plain_client_still_accepts_a_role_cert(net):
    pki, urls = net
    assert requests.get(urls["mgr"] + "/health", verify=pki.ca_file, timeout=5).ok


def test_role_session_accepts_the_expected_role(net):
    assert _get(net, "mgr", _pki.role_name("manager")).ok
    assert _get(net, "ae", _pki.role_name("alarm_engine")).ok
    assert _get(net, "a", _pki.role_name("agent", A)).ok


def test_locked_session_refuses_cert_without_role(net):
    with pytest.raises(requests.exceptions.SSLError) as e:
        _get(net, "a", _pki.role_name("manager"))
    assert tls_roles.is_name_mismatch(e.value)


def test_alarm_engine_cert_is_not_accepted_as_the_manager(net):
    with pytest.raises(requests.exceptions.SSLError):
        _get(net, "ae", _pki.role_name("manager"))


def test_agent_cert_is_not_accepted_for_another_agent(net):
    with pytest.raises(requests.exceptions.SSLError) as e:
        _get(net, "a", _pki.role_name("agent", B))
    assert tls_roles.is_name_mismatch(e.value)


def test_connection_error_is_not_a_name_mismatch():
    with pytest.raises(requests.exceptions.ConnectionError) as e:
        tls_roles.role_session("manager.role.llmsys.internal").get("https://127.0.0.1:9/health", timeout=2)
    assert not tls_roles.is_name_mismatch(e.value)


def test_role_session_refuses_plain_http():
    import http.server
    import threading
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/health"
        assert requests.get(url, timeout=5).ok
        with pytest.raises(requests.exceptions.ConnectionError, match="plain HTTP"):
            tls_roles.role_session("manager.role.llmsys.internal").get(url, timeout=5)
        assert hits == ["/health"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_wrong_ca_is_not_a_name_mismatch(net, tmp_path):
    pki, urls = net
    other = Pki(tmp_path)
    with pytest.raises(requests.exceptions.SSLError) as e:
        tls_roles.role_session(_pki.role_name("manager")).get(urls["mgr"] + "/health", verify=other.ca_file, timeout=5)
    assert not tls_roles.is_name_mismatch(e.value)


def test_streaming_works_through_a_role_session(net):
    pki, urls = net
    r = tls_roles.role_session(_pki.role_name("manager")).get(urls["mgr"] + "/x", verify=pki.ca_file, stream=True, timeout=5)
    assert b"".join(r.iter_content(1)) == b"ok"


def test_locks_absent_file_is_unlocked(tmp_path):
    assert tls_roles.load_locks(tmp_path / "tls-roles.json", ROLES) == {}


def test_lock_role_persists_and_is_idempotent(tmp_path):
    p = tmp_path / "tls-roles.json"
    tls_roles.lock_role(p, "alarm_engine", ROLES)
    first = tls_roles.load_locks(p, ROLES)
    assert set(first) == {"alarm_engine"} and first["alarm_engine"]
    tls_roles.lock_role(p, "alarm_engine", ROLES)
    assert tls_roles.load_locks(p, ROLES) == first
    assert not list(tmp_path.glob("*.tmp"))


def test_unknown_roles_in_the_file_are_ignored(tmp_path):
    p = tmp_path / "tls-roles.json"
    p.write_text(json.dumps({"alarm_engine": "2026-10-01T00:00:00+00:00", "other": "x", "manager": ""}))
    assert set(tls_roles.load_locks(p, ROLES)) == {"alarm_engine"}


@pytest.mark.parametrize("content", ["{not json", "[]", "", "\x00\x01"])
def test_unreadable_lock_file_means_locked(tmp_path, content):
    p = tmp_path / "tls-roles.json"
    p.write_text(content)
    assert set(tls_roles.load_locks(p, ROLES)) == set(ROLES)


def test_lock_role_rejects_an_unknown_role(tmp_path):
    with pytest.raises(ValueError):
        tls_roles.lock_role(tmp_path / "tls-roles.json", "nope", ROLES)


@pytest.fixture
def proxy():
    url, stop = connect_proxy()
    yield url
    stop()


def test_role_is_required_through_an_explicit_proxy(net, proxy):
    pki, urls = net
    name = _pki.role_name("manager")
    via = {"https": proxy}
    assert tls_roles.role_session(name).get(urls["mgr"] + "/health", verify=pki.ca_file, proxies=via, timeout=5).ok
    with pytest.raises(requests.exceptions.RequestException) as e:
        tls_roles.role_session(name).get(urls["a"] + "/health", verify=pki.ca_file, proxies=via, timeout=5)
    assert tls_roles.is_name_mismatch(e.value)


def test_role_is_required_through_an_environment_proxy(net, proxy, monkeypatch):
    pki, urls = net
    for var in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    name = _pki.role_name("manager")
    assert tls_roles.role_session(name).get(urls["mgr"] + "/health", verify=pki.ca_file, timeout=5).ok
    with pytest.raises(requests.exceptions.RequestException):
        tls_roles.role_session(name).get(urls["a"] + "/health", verify=pki.ca_file, timeout=5)


def test_a_refusal_is_logged_with_the_missing_role(net, caplog):
    name = _pki.role_name("manager")
    with caplog.at_level(logging.ERROR, logger="llm-systems-manager.tls_roles"):
        with pytest.raises(requests.exceptions.SSLError):
            _get(net, "a", name)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and name in errors[0] and "127.0.0.1" in errors[0]


def test_a_probe_session_does_not_log_refusals(net, caplog):
    pki, urls = net
    with caplog.at_level(logging.ERROR, logger="llm-systems-manager.tls_roles"):
        with pytest.raises(requests.exceptions.SSLError):
            tls_roles.role_session(_pki.role_name("manager"), quiet=True).get(urls["a"] + "/health", verify=pki.ca_file, timeout=5)
    assert not caplog.records


def test_an_unreachable_peer_is_not_logged_as_a_refusal(caplog):
    with caplog.at_level(logging.ERROR, logger="llm-systems-manager.tls_roles"):
        with pytest.raises(requests.exceptions.ConnectionError):
            tls_roles.role_session("manager.role.llmsys.internal").get("https://127.0.0.1:9/health", timeout=2)
    assert not caplog.records


def test_a_wildcard_outside_the_zone_does_not_satisfy_a_role(tmp_path):
    pki = Pki(tmp_path)
    pem, key = _pki.sign_agent_cert(pki.ca_cert, pki.ca_key, agent_id=A, hostname="hostx", ip_san="127.0.0.1",
                                    extra_dns_sans=["*.llmsys.internal", "*.internal"])
    (tmp_path / "w.crt").write_text(pem)
    (tmp_path / "w.key").write_text(key)
    url, stop = serve(tmp_path / "w.crt", tmp_path / "w.key")
    try:
        for role in (_pki.role_name("manager"), _pki.role_name("alarm_engine"), _pki.role_name("agent", B)):
            with pytest.raises(requests.exceptions.SSLError):
                tls_roles.role_session(role).get(url + "/health", verify=pki.ca_file, timeout=5)
        assert tls_roles.role_session(_pki.role_name("agent", A)).get(url + "/health", verify=pki.ca_file, timeout=5).ok
    finally:
        stop()


def test_helper_servers_refuse_old_tls_versions(net):
    import socket
    import ssl
    from urllib.parse import urlsplit
    pki, urls = net
    u = urlsplit(urls["mgr"])
    ctx = ssl.create_default_context(cafile=pki.ca_file)
    ctx.check_hostname = False
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with ctx.wrap_socket(socket.create_connection((u.hostname, u.port), timeout=5)) as s:
        assert s.version() in ("TLSv1.2", "TLSv1.3")
    import inspect
    assert "minimum_version = ssl.TLSVersion.TLSv1_2" in inspect.getsource(serve)
