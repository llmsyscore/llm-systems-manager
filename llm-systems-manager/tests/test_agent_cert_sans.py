"""Agent leaf certificates name the agent's own address and hostname only."""
from __future__ import annotations

import pytest

import agent_registry as ar

AID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


def _agent(**over) -> dict:
    a = {"agent_id": AID, "hostname": "box", "os": "linux", "status": "approved",
         "bind_url": "https://10.0.0.5:8082", "registered_from": "10.0.0.5"}
    a.update(over)
    return a


@pytest.fixture
def install(monkeypatch):
    """Manager host mgr (10.0.0.1), alarm engine at 10.0.0.2, a second agent seen from 10.0.0.9."""
    store = {"agents": {OTHER: {"agent_id": OTHER, "hostname": "other", "registered_from": "10.0.0.9",
                                "bind_url": "https://10.0.0.9:8082"}}, "global": {}}
    monkeypatch.setattr(ar, "load_agents", lambda: store)
    monkeypatch.setattr(ar, "_manager_host_ips", lambda: frozenset({"10.0.0.1"}))
    monkeypatch.setattr(ar._deps, "hostname", "Mgr.lan")
    monkeypatch.setattr(ar._deps, "alarm_engine_url", lambda: "https://10.0.0.2:8081")
    return store


def _sans(install, agent):
    install["agents"][agent["agent_id"]] = agent
    return ar._agent_cert_sans(agent)


def test_own_address_and_hostname(install):
    assert _sans(install, _agent()) == ("box", ["10.0.0.5"])


def test_hostname_bind_url_uses_the_observed_address(install):
    assert _sans(install, _agent(bind_url="https://box:8082")) == ("box", ["10.0.0.5"])


def test_second_address_of_a_multi_homed_agent_is_kept(install):
    assert _sans(install, _agent(bind_url="https://10.1.0.5:8082")) == ("box", ["10.1.0.5", "10.0.0.5"])


@pytest.mark.parametrize("taken", ["10.0.0.1", "10.0.0.2", "10.0.0.9", "127.0.0.1", "::1"])
def test_another_members_address_is_left_out(install, taken):
    host = f"[{taken}]" if ":" in taken else taken
    assert _sans(install, _agent(bind_url=f"https://{host}:8082")) == ("box", ["10.0.0.5"])


@pytest.mark.parametrize("name", ["mgr", "MGR.LAN", "mgr.lan.", "localhost"])
def test_managers_hostname_is_replaced_by_the_agent_id(install, name):
    assert _sans(install, _agent(hostname=name)) == (AID, ["10.0.0.5"])


@pytest.mark.parametrize("name", ["mgr.other.example", "localhost.localdomain", "mgr.agents.local",
                                  "*.lan", "a b", "llm.example.com", "LLM.example.com."])
def test_wildcard_and_other_reserved_names_are_replaced(install, monkeypatch, name):
    monkeypatch.setattr(ar._deps, "manager_public_hosts", lambda: ["llm.example.com", "203.0.113.7"])
    assert _sans(install, _agent(hostname=name)) == (AID, ["10.0.0.5"])


@pytest.mark.parametrize("taken", ["203.0.113.7", "0.0.0.0", "::"])
def test_public_host_and_unspecified_addresses_are_left_out(install, monkeypatch, taken):
    monkeypatch.setattr(ar._deps, "manager_public_hosts", lambda: ["llm.example.com", "203.0.113.7"])
    host = f"[{taken}]" if ":" in taken else taken
    assert _sans(install, _agent(bind_url=f"https://{host}:8082")) == ("box", ["10.0.0.5"])


def test_alarm_engine_name_is_kept_only_from_the_alarm_engine_host(install, monkeypatch):
    import socket
    monkeypatch.setattr(ar._deps, "alarm_engine_url", lambda: "https://ae.lan:8081")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("10.0.0.2", 0))])
    assert _sans(install, _agent(hostname="ae.lan")) == (AID, ["10.0.0.5"])
    on_ae = _agent(hostname="AE.lan", bind_url="https://10.0.0.2:8082", registered_from="10.0.0.2")
    assert _sans(install, on_ae) == ("AE.lan", ["10.0.0.2"])
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: (_ for _ in ()).throw(OSError("no dns")))
    assert _sans(install, on_ae) == (AID, ["10.0.0.2"])


def test_manager_host_ips_lists_interface_addresses(monkeypatch):
    monkeypatch.setattr(ar, "_host_ips_cache", (0.0, frozenset()))
    assert "127.0.0.1" in ar._manager_host_ips()


@pytest.mark.parametrize("src", ["127.0.0.1", "10.0.0.1"])
def test_agent_on_the_manager_host_keeps_its_names(install, src):
    a = _agent(hostname="mgr", bind_url="https://10.0.0.1:8082", registered_from=src)
    assert _sans(install, a) == ("mgr", list(dict.fromkeys(["10.0.0.1", src])))


def test_designated_host_agent_keeps_its_bind_address(install):
    install["global"]["host_agent_id"] = AID
    a = _agent(bind_url="https://10.0.0.1:8082", registered_from="172.18.0.1")
    assert _sans(install, a) == ("box", ["10.0.0.1", "172.18.0.1"])


def test_two_agents_behind_one_address_each_keep_it(install):
    assert _sans(install, _agent(bind_url="https://10.0.0.9:8082", registered_from="10.0.0.9")) == ("box", ["10.0.0.9"])


@pytest.mark.parametrize("bind_url", [123, ["x"], "https://[::1", "", None])
def test_unusable_bind_url_leaves_the_observed_address(install, bind_url):
    assert _sans(install, _agent(bind_url=bind_url)) == ("box", ["10.0.0.5"])


def test_ipv4_mapped_forms_are_compared_as_ipv4(install):
    assert _sans(install, _agent(bind_url="https://[::ffff:10.0.0.1]:8082")) == ("box", ["10.0.0.5"])
    a = _agent(hostname="mgr", bind_url="https://10.0.0.1:8082", registered_from="::ffff:10.0.0.1")
    assert _sans(install, a) == ("mgr", ["10.0.0.1"])


def test_reissue_check_ignores_an_address_left_out_of_the_san(install, monkeypatch):
    a = _agent(bind_url="https://10.0.0.2:8082")
    install["agents"][AID] = a
    monkeypatch.setattr(ar._deps, "settings", type("S", (), {"manager": type("M", (), {
        "security": type("Sec", (), {"tls_rotation_warn_days": 30})})}), raising=False)
    monkeypatch.setattr(ar, "_build_and_sign_agent_cert", lambda agent: pytest.fail("reissued"))
    body = {"has_tls_cert": True, "tls_san_ips": ["10.0.0.5"]}
    assert ar._maybe_issue_tls_bundle(a, body) is None


def test_reissue_check_still_fires_for_a_missing_own_address(install, monkeypatch):
    a = _agent()
    install["agents"][AID] = a
    monkeypatch.setattr(ar._deps, "settings", type("S", (), {"manager": type("M", (), {
        "security": type("Sec", (), {"tls_rotation_warn_days": 30})})}), raising=False)
    monkeypatch.setattr(ar, "save_agents", lambda d: None)
    monkeypatch.setattr(ar, "_build_and_sign_agent_cert", lambda agent: {"expires_at": "x"})
    out = ar._maybe_issue_tls_bundle(a, {"has_tls_cert": True, "tls_san_ips": ["10.9.9.9"]})
    assert out == {"expires_at": "x", "reason": "san-mismatch"}


def test_reissue_check_reads_ipv6_sans_in_any_spelling(install, monkeypatch):
    a = _agent(bind_url="https://[2001:db8::5]:8082", registered_from="2001:db8::5")
    install["agents"][AID] = a
    monkeypatch.setattr(ar._deps, "settings", type("S", (), {"manager": type("M", (), {
        "security": type("Sec", (), {"tls_rotation_warn_days": 30})})}), raising=False)
    monkeypatch.setattr(ar, "_build_and_sign_agent_cert", lambda agent: pytest.fail("reissued"))
    body = {"has_tls_cert": True, "tls_san_ips": ["2001:DB8:0:0:0:0:0:5"]}
    assert ar._maybe_issue_tls_bundle(a, body) is None
