"""
wss alert bridge (#525): scheme-aware browser URL and a live TLS handshake
through the real bridge + ticket gate.
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import subprocess
import time
from types import SimpleNamespace

import pytest

import manager_mod as M
import proxies


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestBrowserWsUrlScheme:
    def _wire(self, monkeypatch, *, https, wss_port):
        monkeypatch.setattr(proxies, "_deps", SimpleNamespace(
            ctx=SimpleNamespace(alarm_engine_url=lambda: "http://127.0.0.1:8081"),
            request_host_no_port=lambda: "mgr.example.com",
            rewrite_loopback_host=lambda url, host: url,
            request_is_https=lambda: https,
            wss_bridge_port=lambda: wss_port,
        ))
        monkeypatch.setattr(proxies.settings.manager, "ws_proxy_port", 5444, raising=False)

    def test_https_page_gets_wss_when_bridge_has_tls(self, monkeypatch):
        # The #525 regression: an https dashboard handed ws:// is
        # mixed-content-blocked and the alert stream never connects.
        self._wire(monkeypatch, https=True, wss_port=5446)
        assert proxies.ae_ws_url_for_browser() == "wss://mgr.example.com:5446/ws/alarm"

    def test_http_page_keeps_plain_ws(self, monkeypatch):
        self._wire(monkeypatch, https=False, wss_port=5446)
        assert proxies.ae_ws_url_for_browser() == "ws://mgr.example.com:5444/ws/alarm"

    def test_https_without_tls_bridge_falls_back(self, monkeypatch):
        self._wire(monkeypatch, https=True, wss_port=0)
        assert proxies.ae_ws_url_for_browser() == "ws://mgr.example.com:5444/ws/alarm"


class TestWssBridgePort:
    def test_zero_without_custom_cert(self, monkeypatch):
        monkeypatch.setattr(M.settings.manager, "ws_proxy_port", 5444, raising=False)
        monkeypatch.setattr(M.settings.manager, "tls_cert_file", "", raising=False)
        monkeypatch.setattr(M.settings.manager, "tls_key_file", "", raising=False)
        assert M._wss_bridge_port() == 0

    def test_zero_when_bridge_disabled(self, monkeypatch, tmp_path):
        crt, key = tmp_path / "c.crt", tmp_path / "k.key"
        crt.write_text("x"); key.write_text("y"); key.chmod(0o600)
        monkeypatch.setattr(M.settings.manager, "ws_proxy_port", 0, raising=False)
        monkeypatch.setattr(M.settings.manager, "tls_cert_file", str(crt), raising=False)
        monkeypatch.setattr(M.settings.manager, "tls_key_file", str(key), raising=False)
        assert M._wss_bridge_port() == 0

    def test_port_when_cert_and_bridge_active(self, monkeypatch, tmp_path):
        crt, key = tmp_path / "c.crt", tmp_path / "k.key"
        crt.write_text("x"); key.write_text("y"); key.chmod(0o600)
        monkeypatch.setattr(M.settings.manager, "ws_proxy_port", 5444, raising=False)
        monkeypatch.setattr(M.settings.manager, "ws_proxy_tls_port", 5446, raising=False)
        monkeypatch.setattr(M.settings.manager, "tls_cert_file", str(crt), raising=False)
        monkeypatch.setattr(M.settings.manager, "tls_key_file", str(key), raising=False)
        assert M._wss_bridge_port() == 5446


@pytest.fixture(scope="module")
def test_cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("wsscert")
    crt, key = d / "t.crt", d / "t.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
         "ec_paramgen_curve:P-256", "-keyout", str(key), "-out", str(crt),
         "-days", "1", "-nodes", "-subj", "/CN=bridge-test",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True,
    )
    key.chmod(0o600)
    return crt, key


class TestLiveWssHandshake:
    def test_wss_served_and_ticket_gate_enforced(self, monkeypatch, test_cert):
        # Would have caught #525: proves the bridge actually terminates TLS
        # and runs the #514 ticket gate on the wss listener.
        crt, key = test_cert
        ws_port, wss_port = _free_port(), _free_port()
        monkeypatch.setattr(M.settings.manager, "ws_proxy_port", ws_port, raising=False)
        monkeypatch.setattr(M.settings.manager, "ws_proxy_tls_port", wss_port, raising=False)
        monkeypatch.setattr(M.settings.manager, "tls_cert_file", str(crt), raising=False)
        monkeypatch.setattr(M.settings.manager, "tls_key_file", str(key), raising=False)
        monkeypatch.setattr(M, "_alarm_engine_url", "http://127.0.0.1:1", raising=False)
        M._maybe_start_alarm_ws_proxy()

        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", wss_port), timeout=0.3).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("wss listener never came up")

        async def _probe():
            import websockets
            # The self-signed test cert is its own trust anchor — full
            # verification, no CERT_NONE.
            cctx = ssl.create_default_context(cafile=str(crt))
            async with websockets.connect(
                f"wss://127.0.0.1:{wss_port}/ws/alarm", ssl=cctx,
                open_timeout=5, close_timeout=5,
            ) as ws:
                await ws.recv()

        import websockets.exceptions as wexc
        with pytest.raises(wexc.ConnectionClosed) as exc_info:
            asyncio.run(_probe())
        assert exc_info.value.rcvd.code == 1008
