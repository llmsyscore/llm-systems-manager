"""#519 manager side: the WS bridge presents the AE read bearer upstream, and
no direct-dial URL is advertised to browsers once that bearer is configured."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import manager_mod as M
import proxies


def _wire(monkeypatch, *, ws_proxy_port=0, ingest="", management=""):
    monkeypatch.setattr(proxies, "_deps", SimpleNamespace(
        ctx=SimpleNamespace(alarm_engine_url=lambda: "http://127.0.0.1:8081"),
        request_host_no_port=lambda: "mgr.example.com",
        rewrite_loopback_host=lambda url, host: url,
        request_is_https=lambda: False,
        wss_bridge_port=lambda: 0,
    ))
    monkeypatch.setattr(proxies.settings.manager, "ws_proxy_port", ws_proxy_port,
                        raising=False)
    monkeypatch.setattr(proxies.settings.alarm_engine, "ingest_token", ingest,
                        raising=False)
    monkeypatch.setattr(proxies.settings.alarm_engine, "management_token",
                        management, raising=False)


def test_no_token_keeps_direct_dial(monkeypatch):
    _wire(monkeypatch)
    assert proxies.ae_ws_url_for_browser() == "ws://127.0.0.1:8081/ws"


def test_placeholder_token_keeps_direct_dial(monkeypatch):
    _wire(monkeypatch, ingest="REPLACE_ME")
    assert proxies.ae_ws_url_for_browser() == "ws://127.0.0.1:8081/ws"


def test_ingest_token_suppresses_direct_dial(monkeypatch):
    # A browser can't attach Authorization to a WS handshake, so a direct
    # URL would just 1008-loop against the gated AE.
    _wire(monkeypatch, ingest="sekrit")
    assert proxies.ae_ws_url_for_browser() == ""


def test_management_token_suppresses_direct_dial(monkeypatch):
    _wire(monkeypatch, management="read-tok")
    assert proxies.ae_ws_url_for_browser() == ""


def test_bridge_url_unaffected_by_token(monkeypatch):
    _wire(monkeypatch, ws_proxy_port=5444, ingest="sekrit")
    assert proxies.ae_ws_url_for_browser() == "ws://mgr.example.com:5444/ws/alarm"


def test_bridge_presents_bearer_upstream():
    src = inspect.getsource(M._maybe_start_alarm_ws_proxy)
    assert "additional_headers" in src, \
        "bridge dials the AE without the read bearer — gated /ws would 1008 it"
    assert "_AE_BEARER" in src
