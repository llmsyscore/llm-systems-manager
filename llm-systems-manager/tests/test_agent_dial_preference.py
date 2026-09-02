"""#791: the callback base that last connected is dialed first on every
later call to that agent."""
from __future__ import annotations

import pytest
import requests

import agent_registry


AGENT = {
    "agent_id": "11111111-0000-0000-0000-000000000000",
    "hostname": "agent-host",
    "bind_url": "https://agent-host:8082",
    "registered_from": "192.0.2.7",
    "token": "tok",
}
BIND = "https://agent-host:8082"
FALLBACK = "https://192.0.2.7:8082"


@pytest.fixture(autouse=True)
def _clear_pref():
    with agent_registry._dial_pref_lock:
        agent_registry._dial_pref.clear()
    yield
    with agent_registry._dial_pref_lock:
        agent_registry._dial_pref.clear()


def test_default_order_is_bind_then_registered_from():
    assert agent_registry.agent_callback_urls(AGENT) == [BIND, FALLBACK]


def test_connected_base_moves_to_front():
    agent_registry.note_dial_result(AGENT, FALLBACK, True)
    assert agent_registry.agent_callback_urls(AGENT) == [FALLBACK, BIND]


def test_preference_outside_current_list_is_ignored():
    agent_registry.note_dial_result(AGENT, "https://192.0.2.9:8082", True)
    assert agent_registry.agent_callback_urls(AGENT) == [BIND, FALLBACK]


def test_failed_preferred_base_loses_its_slot():
    agent_registry.note_dial_result(AGENT, FALLBACK, True)
    agent_registry.note_dial_result(AGENT, FALLBACK, False)
    assert agent_registry.agent_callback_urls(AGENT) == [BIND, FALLBACK]


def test_failure_of_non_preferred_base_keeps_preference():
    agent_registry.note_dial_result(AGENT, FALLBACK, True)
    agent_registry.note_dial_result(AGENT, BIND, False)
    assert agent_registry.agent_callback_urls(AGENT) == [FALLBACK, BIND]


def test_read_timeout_does_not_demote_a_connected_base():
    agent_registry.note_dial_result(AGENT, FALLBACK, True)
    agent_registry.note_dial_error(AGENT, FALLBACK, requests.ReadTimeout("slow"))
    assert agent_registry.agent_callback_urls(AGENT) == [FALLBACK, BIND]
    agent_registry.note_dial_error(AGENT, FALLBACK, requests.ConnectTimeout("dead"))
    assert agent_registry.agent_callback_urls(AGENT) == [BIND, FALLBACK]


def test_agent_without_id_is_a_no_op():
    agent_registry.note_dial_result({"bind_url": BIND}, BIND, True)
    assert agent_registry._dial_pref == {}


class _Resp:
    status_code = 200


def test_agent_request_learns_the_live_base(monkeypatch):
    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        if url.startswith(BIND):
            raise requests.ConnectionError("No route to host")
        return _Resp()

    monkeypatch.setattr(agent_registry.requests, "request", fake_request)

    r, tried, err = agent_registry.agent_request("GET", AGENT, "/status")
    assert r is not None and err is None
    assert tried == [f"{BIND}/status", f"{FALLBACK}/status"]

    r, tried, err = agent_registry.agent_request("GET", AGENT, "/status")
    assert r is not None and err is None
    assert tried == [f"{FALLBACK}/status"], "dead bind_url was dialed again"
    assert calls == [f"{BIND}/status", f"{FALLBACK}/status", f"{FALLBACK}/status"]


def test_agent_request_all_dead_records_nothing(monkeypatch):
    def fake_request(method, url, **kw):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(agent_registry.requests, "request", fake_request)
    r, tried, err = agent_registry.agent_request("GET", AGENT, "/status")
    assert r is None and err
    assert agent_registry._dial_pref == {}


def test_agent_request_keeps_preference_through_a_read_timeout(monkeypatch):
    agent_registry.note_dial_result(AGENT, FALLBACK, True)

    def fake_request(method, url, **kw):
        if url.startswith(FALLBACK):
            raise requests.ReadTimeout("slow")
        raise requests.ConnectionError("No route to host")

    monkeypatch.setattr(agent_registry.requests, "request", fake_request)
    r, tried, _err = agent_registry.agent_request("GET", AGENT, "/status")
    assert r is None
    assert tried == [f"{FALLBACK}/status", f"{BIND}/status"]
    assert agent_registry.agent_callback_urls(AGENT)[0] == FALLBACK
