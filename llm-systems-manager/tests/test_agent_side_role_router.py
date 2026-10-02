"""The agent's own adapter classes, exercised against real TLS servers (#1161)."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from tests.tls_servers import Pki, connect_proxy, serve

AGENT_PY = Path(__file__).resolve().parents[2] / "agent" / "llm-systems-agent.py"
SRC = AGENT_PY.read_text()


def _block(kind: str, name: str) -> str:
    m = re.search(rf"^{kind} {name}\b.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {kind} {name}"
    return m.group(0)


@pytest.fixture
def agent_ns(tmp_path):
    pki = Pki(tmp_path)
    mgr, stop_m = serve(*pki.leaf("mgr", "manager", "llm-systems-manager"))
    fake, stop_f = serve(*pki.leaf("agent", "agent", "11111111-2222-3333-4444-555555555555"))
    locked: set = set()
    ns = {"requests": requests, "logger": logging.getLogger("agent-test"), "urlsplit": urlsplit,
          "_ROLE_NAMES": {"manager": "manager.role.llmsys.internal",
                          "alarm_engine": "alarm-engine.role.llmsys.internal"},
          "_tls_role_locked": lambda role: role in locked, "_peer_role_for": lambda url: "manager"}
    for kind, name in (("class", "_RoleAdapter"), ("class", "_RoleRouter"), ("def", "_is_name_mismatch")):
        exec(_block(kind, name), ns)
    sess = requests.Session()
    sess.verify = pki.ca_file
    sess.mount("https://", ns["_RoleRouter"]())
    yield ns, sess, locked, mgr, fake
    stop_m()
    stop_f()


def test_router_is_plain_until_locked(agent_ns):
    ns, sess, locked, mgr, fake = agent_ns
    assert sess.get(fake + "/health", timeout=5).ok


def test_router_refuses_after_lock(agent_ns):
    ns, sess, locked, mgr, fake = agent_ns
    locked.add("manager")
    assert sess.get(mgr + "/health", timeout=5).ok
    with pytest.raises(requests.exceptions.SSLError) as e:
        sess.get(fake + "/health", timeout=5)
    assert ns["_is_name_mismatch"](e.value)


def test_router_leaves_other_hosts_alone(agent_ns):
    ns, sess, locked, mgr, fake = agent_ns
    locked.add("manager")
    ns["_peer_role_for"] = lambda url: None
    sess.mount("https://", ns["_RoleRouter"]())
    assert sess.get(fake + "/health", timeout=5).ok


@pytest.fixture
def probe_ns(tmp_path):
    from types import SimpleNamespace
    pki = Pki(tmp_path)
    mgr, stop_m = serve(*pki.leaf("mgr", "manager", "llm-systems-manager"))
    ae, stop_a = serve(*pki.leaf("ae", "alarm_engine", "llm-systems-alarm-engine"))
    fake, stop_f = serve(*pki.leaf("agent", "agent", "11111111-2222-3333-4444-555555555555"))
    locked, warned = set(), []
    ns = {"requests": requests, "logging": logging, "logger": logging.getLogger("agent-test"), "urlsplit": urlsplit,
          "_ROLE_NAMES": {"manager": "manager.role.llmsys.internal",
                          "alarm_engine": "alarm-engine.role.llmsys.internal"},
          "CONFIG": SimpleNamespace(MANAGER_URL=mgr, ALARM_ENGINE_URL=ae),
          "_ca_bundle_path": lambda: Path(pki.ca_file),
          "_tls_role_locked": lambda role: role in locked, "_tls_role_lock": locked.add,
          "remembered": {}, "_tls_role_remember_url": lambda role, url: ns["remembered"].__setitem__(role, url),
          "_diag_throttle": lambda key, msg, *a, **k: warned.append(key)}
    for kind, name in (("class", "_RoleAdapter"), ("def", "_is_name_mismatch"), ("def", "_role_get"),
                       ("def", "_maybe_lock_peer_roles")):
        exec(_block(kind, name), ns)
    yield ns, locked, warned, pki, fake
    stop_m()
    stop_a()
    stop_f()


def test_probe_locks_both_peers_that_carry_their_role(probe_ns):
    ns, locked, warned, pki, fake = probe_ns
    ns["_maybe_lock_peer_roles"]()
    assert locked == {"manager", "alarm_engine"} and warned == []
    assert ns["remembered"] == {"manager": ns["CONFIG"].MANAGER_URL, "alarm_engine": ns["CONFIG"].ALARM_ENGINE_URL}


def test_probe_leaves_a_manager_without_role_unlocked_and_warns(probe_ns):
    ns, locked, warned, pki, fake = probe_ns
    ns["CONFIG"].MANAGER_URL = fake
    ns["_maybe_lock_peer_roles"]()
    assert locked == {"alarm_engine"}
    assert warned == ["tls_role_missing_manager"]


def test_probe_ignores_an_unreachable_or_plain_http_peer(probe_ns):
    ns, locked, warned, pki, fake = probe_ns
    ns["CONFIG"].MANAGER_URL = "https://127.0.0.1:9"
    ns["CONFIG"].ALARM_ENGINE_URL = "http://127.0.0.1:9"
    ns["_maybe_lock_peer_roles"]()
    assert locked == set() and warned == []


def test_role_get_requires_the_role_only_once_locked(probe_ns):
    ns, locked, warned, pki, fake = probe_ns
    assert ns["_role_get"]("manager", fake + "/health", timeout=5, verify=pki.ca_file).ok
    locked.add("manager")
    with pytest.raises(requests.exceptions.SSLError):
        ns["_role_get"]("manager", fake + "/health", timeout=5, verify=pki.ca_file)
    assert ns["_role_get"]("manager", ns["CONFIG"].MANAGER_URL + "/health", timeout=5, verify=pki.ca_file).ok


def test_router_requires_the_role_through_a_proxy(agent_ns):
    ns, sess, locked, mgr, fake = agent_ns
    proxy, stop = connect_proxy()
    try:
        locked.add("manager")
        assert sess.get(mgr + "/health", proxies={"https": proxy}, timeout=5).ok
        with pytest.raises(requests.exceptions.RequestException) as e:
            sess.get(fake + "/health", proxies={"https": proxy}, timeout=5)
        assert ns["_is_name_mismatch"](e.value)
    finally:
        stop()


def test_router_logs_a_refusal_with_the_missing_role(agent_ns, caplog):
    ns, sess, locked, mgr, fake = agent_ns
    locked.add("manager")
    with caplog.at_level(logging.ERROR, logger="agent-test"):
        with pytest.raises(requests.exceptions.SSLError):
            sess.get(fake + "/health", timeout=5)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "manager.role.llmsys.internal" in errors[0]


def test_probe_does_not_log_a_refusal_as_an_error(probe_ns, caplog):
    ns, locked, warned, pki, fake = probe_ns
    ns["CONFIG"].MANAGER_URL = fake
    with caplog.at_level(logging.ERROR, logger="agent-test"):
        ns["_maybe_lock_peer_roles"]()
    assert warned == ["tls_role_missing_manager"]
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]

