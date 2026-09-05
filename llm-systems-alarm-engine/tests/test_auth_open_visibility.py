"""#828: auth posture — auth_state(), /health's auth fields, startup WARNING."""
from __future__ import annotations

import asyncio
import logging

import pytest

from backend import alarm_engine as ae
from backend.api import auth


@pytest.fixture
def tokens(monkeypatch):
    def _set(mgmt="", ingest="", host="0.0.0.0"):
        monkeypatch.setattr(auth.settings.alarm_engine, "management_token", mgmt, raising=False)
        monkeypatch.setattr(auth.settings.alarm_engine, "ingest_token", ingest, raising=False)
        monkeypatch.setattr(auth.settings.alarm_engine, "host", host, raising=False)
    return _set


class _Stop(Exception):
    pass


@pytest.fixture
def banner_only(monkeypatch, caplog):
    """Runs _on_startup up to the cache init and returns the captured records."""
    def _boom():
        raise _Stop()
    monkeypatch.setattr(ae, "Cache", _boom)

    def _run():
        with caplog.at_level(logging.INFO, logger=ae.logger.name):
            with pytest.raises(_Stop):
                asyncio.run(ae._on_startup())
        return caplog.records
    return _run


def test_open_on_network_when_no_tokens_and_routable_bind(tokens):
    tokens("", "", "0.0.0.0")
    assert auth.auth_state() == {"management": "open", "ingest": "open",
                                 "loopback_only": False, "open_on_network": True,
                                 "bearer_ok": None}


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.53",
                                  "::ffff:127.0.0.1", "LOCALHOST"])
def test_loopback_bind_is_not_open_on_network(tokens, host):
    tokens("", "", host)
    st = auth.auth_state()
    assert st["management"] == "open"
    assert st["loopback_only"] is True
    assert st["open_on_network"] is False


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "ae.internal", "127.999.1.1", ""])
def test_non_loopback_binds(host):
    assert auth.bind_is_loopback(host) is False


def test_management_token_enforces(tokens):
    tokens("m" * 64, "", "0.0.0.0")
    st = auth.auth_state()
    assert st["management"] == "management_token"
    assert st["ingest"] == "open"
    assert st["open_on_network"] is False


def test_ingest_token_alone_is_the_management_fallback(tokens):
    tokens("", "i" * 64, "0.0.0.0")
    st = auth.auth_state()
    assert st["management"] == "ingest_token" and st["ingest"] == "enforced"
    assert st["open_on_network"] is False


def test_replace_me_counts_as_unset(tokens):
    tokens("REPLACE_ME", "REPLACE_ME", "0.0.0.0")
    assert auth.auth_state()["open_on_network"] is True


def test_bearer_ok_reports_whether_the_presented_token_matches(tokens):
    tokens("m" * 64, "i" * 64, "0.0.0.0")
    assert auth.auth_state("Bearer " + "m" * 64)["bearer_ok"] is True
    assert auth.auth_state("Bearer " + "i" * 64)["bearer_ok"] is False
    assert auth.auth_state("Bearer wrong")["bearer_ok"] is False
    tokens("", "i" * 64, "0.0.0.0")
    assert auth.auth_state("Bearer " + "i" * 64)["bearer_ok"] is True
    assert auth.auth_state(None)["bearer_ok"] is None
    assert auth.auth_state("Basic abc")["bearer_ok"] is None
    tokens("", "", "0.0.0.0")
    assert auth.auth_state("Bearer anything")["bearer_ok"] is None


def test_health_reports_auth(tokens, monkeypatch):
    monkeypatch.setattr(ae.settings.influxdb, "host", "", raising=False)
    tokens("", "", "0.0.0.0")
    body = asyncio.run(ae.health_check())
    assert body["auth"] == "open"
    assert body["components"]["auth"]["open_on_network"] is True
    tokens("m" * 64, "", "0.0.0.0")
    body = asyncio.run(ae.health_check(authorization="Bearer nope"))
    assert body["auth"] == "enforced"
    assert body["components"]["auth"]["management"] == "management_token"
    assert body["components"]["auth"]["bearer_ok"] is False
    body = asyncio.run(ae.health_check(authorization="Bearer " + "m" * 64))
    assert body["components"]["auth"]["bearer_ok"] is True


def test_startup_banner_warns_when_open(tokens, banner_only):
    tokens("", "", "0.0.0.0")
    records = banner_only()
    warns = [r for r in records if r.levelno == logging.WARNING and "ALARM ENGINE AUTH" in r.getMessage()]
    assert len(warns) == 1
    assert "management_token" in warns[0].getMessage()
    assert "0.0.0.0:" in warns[0].getMessage()
    assert any("Auth:" in r.getMessage() and "management=open" in r.getMessage() for r in records)


def test_startup_banner_quiet_when_enforced(tokens, banner_only):
    tokens("m" * 64, "", "0.0.0.0")
    assert not [r for r in banner_only() if "ALARM ENGINE AUTH" in r.getMessage()]


def test_startup_banner_quiet_on_loopback(tokens, banner_only):
    tokens("", "", "127.0.0.1")
    assert not [r for r in banner_only() if "ALARM ENGINE AUTH" in r.getMessage()]
