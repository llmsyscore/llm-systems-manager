"""The manager locks the alarm engine's role on first sight (#1161)."""
from __future__ import annotations

import pytest
import requests

import manager_mod as M
import tls_roles
from tests.tls_servers import Pki, serve


@pytest.fixture
def ae(tmp_path, monkeypatch):
    pki = Pki(tmp_path)
    good, stop_g = serve(*pki.leaf("ae", "alarm_engine", "llm-systems-alarm-engine"))
    bad, stop_b = serve(*pki.leaf("agent", "agent", "11111111-2222-3333-4444-555555555555"))
    sess = requests.Session()
    sess.verify = pki.ca_file
    monkeypatch.setattr(M, "_ae_session", sess)
    monkeypatch.setattr(M, "_AE_CA_PATH", pki.ca_file)
    monkeypatch.setattr(M, "_TLS_ROLE_LOCKS", tmp_path / "tls-roles.json")
    monkeypatch.setattr(M, "_ae_role_state", {"locked": False})
    yield good, bad
    stop_g()
    stop_b()


def test_probe_locks_and_then_requires_the_role(ae, monkeypatch):
    good, bad = ae
    monkeypatch.setattr(M, "_alarm_engine_url", good)
    assert M._ae_role_probe() is True
    assert M._ae_role_locked()
    assert tls_roles.load_locks(M._TLS_ROLE_LOCKS, M._AE_ROLES)["alarm_engine"]
    assert M._ae_session.get(good + "/health", timeout=5).ok
    with pytest.raises(requests.exceptions.SSLError):
        M._ae_session.get(bad + "/health", timeout=5)


def test_probe_leaves_a_peer_without_role_unlocked(ae, monkeypatch):
    good, bad = ae
    monkeypatch.setattr(M, "_alarm_engine_url", bad)
    assert M._ae_role_probe() is False
    assert not M._ae_role_locked()
    assert M._ae_session.get(bad + "/health", timeout=5).ok


def test_probe_is_a_no_op_for_plain_http(ae, monkeypatch):
    monkeypatch.setattr(M, "_alarm_engine_url", "http://127.0.0.1:1")
    assert M._ae_role_probe() is False
    assert not M._ae_role_locked()


def test_a_stored_lock_is_applied_at_start(ae, monkeypatch):
    good, bad = ae
    tls_roles.lock_role(M._TLS_ROLE_LOCKS, "alarm_engine", M._AE_ROLES)
    M._ae_require_role_if_locked()
    assert M._ae_role_locked()
    with pytest.raises(requests.exceptions.SSLError):
        M._ae_session.get(bad + "/health", timeout=5)


def test_role_name_matches_the_signer():
    import _pki
    assert M._AE_ROLE_NAME == _pki.role_name("alarm_engine")



def test_a_failed_lock_write_still_requires_the_role(ae, monkeypatch, caplog):
    good, bad = ae
    monkeypatch.setattr(M, "_alarm_engine_url", good)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(tls_roles, "lock_role", boom)
    assert M._ae_role_probe() is True
    assert M._ae_role_locked()
    assert any("could not save" in r.getMessage() for r in caplog.records)
    with pytest.raises(requests.exceptions.SSLError):
        M._ae_session.get(bad + "/health", timeout=5)


def test_requiring_the_role_swaps_the_adapters_in_one_step(ae):
    before = M._ae_session.adapters
    plain_https = before["https://"]
    M._ae_require_role()
    assert M._ae_session.adapters is not before
    assert before["https://"] is plain_https
    assert isinstance(M._ae_session.adapters["https://"], tls_roles.RoleAdapter)
    assert list(M._ae_session.adapters) == list(before)


def test_ws_upstream_requires_the_role_once_locked(ae):
    import asyncio
    import ssl

    import websockets

    good, bad = ae
    ctx = ssl.create_default_context(cafile=M._AE_CA_PATH)
    assert M._ae_ws_tls_extra(ctx) == {}
    assert M._ae_ws_tls_extra(None) == {}
    M._ae_role_state["locked"] = True
    assert M._ae_ws_tls_extra(None) == {}
    extra = M._ae_ws_tls_extra(ctx)
    assert extra == {"server_hostname": M._AE_ROLE_NAME}

    async def dial(base):
        async with websockets.connect(base.replace("https://", "wss://") + "/ws", ssl=ctx, open_timeout=4, **extra):
            pass

    with pytest.raises(ssl.SSLCertVerificationError):
        asyncio.run(dial(bad))
    with pytest.raises(Exception) as passed_tls:
        asyncio.run(dial(good))
    assert not isinstance(passed_tls.value, ssl.SSLError)


def test_ws_bridge_uses_the_role_helper():
    import inspect
    src = inspect.getsource(M._maybe_start_alarm_ws_proxy)
    assert "ws_extra = _ae_ws_tls_extra(up_ssl)" in src and "**ws_extra) as up" in src
