"""#1202: the image-generation port is validated when the proxy target is built, and the
proxied pages' policy pins WebSockets to the manager host, allows frames only from the
manager and sandboxes the page without top navigation."""
from __future__ import annotations

import pytest

import agent_registry
import manager_mod as M
import proxies
from config.unified_config import ManagerSecurity, settings


class TestValidPort:
    @pytest.mark.parametrize("value,expected", [
        (1234, 1234), ("1236", 1236), (" 80 ", 80), (65535, 65535),
        (0, None), (65536, None), (-1, None), (True, None), ("abc", None), (None, None),
        (12.5, None), ("", None),
    ])
    def test_values(self, value, expected):
        assert proxies._valid_port(value) == expected


def _agent(port):
    return {"agents": {"a1": {"agent_id": "a1", "hostname": "mac", "status": "approved",
                              "registered_from": "10.0.0.7", "capabilities": {"image_gen": True},
                              "image_gen_port": port}}, "global": {}}


class TestImageGenTarget:
    @pytest.fixture(autouse=True)
    def _auto(self, monkeypatch):
        monkeypatch.setattr(settings.manager.proxies, "image_gen", True, raising=False)

    def test_a_valid_advertised_port_is_used(self, monkeypatch):
        monkeypatch.setattr(agent_registry, "load_agents", lambda: _agent(1236))
        assert proxies.resolve_proxy_target("image_gen") == "http://10.0.0.7:1236"

    @pytest.mark.parametrize("bad", [99999, 0, "abc", True, {"p": 1}])
    def test_an_unusable_port_falls_back_to_the_default(self, monkeypatch, bad, caplog):
        monkeypatch.setattr(agent_registry, "load_agents", lambda: _agent(bad))
        with caplog.at_level("WARNING", logger=proxies.log.name):
            assert proxies.resolve_proxy_target("image_gen") == "http://10.0.0.7:1234"
        assert any("unusable image_gen_port" in r.getMessage() for r in caplog.records)

    def test_a_missing_port_uses_the_default_quietly(self, monkeypatch, caplog):
        monkeypatch.setattr(agent_registry, "load_agents", lambda: _agent(None))
        with caplog.at_level("WARNING", logger=proxies.log.name):
            assert proxies.resolve_proxy_target("image_gen") == "http://10.0.0.7:1234"
        assert not caplog.records


DEFAULT = ManagerSecurity().proxy_html_csp


class TestProxiedPolicy:
    def test_the_default_policy_pins_sockets_frames_and_sandboxes(self):
        assert "connect-src 'self' data: ws://{host}:* wss://{host}:*" in DEFAULT
        assert " ws: " not in DEFAULT and " wss:;" not in DEFAULT
        assert "frame-src 'self'" in DEFAULT
        assert "sandbox allow-scripts allow-same-origin" in DEFAULT
        assert "allow-top-navigation" not in DEFAULT
        assert "frame-ancestors 'self'" in DEFAULT and "object-src 'none'" in DEFAULT

    def test_host_placeholder_becomes_the_browser_hostname(self, monkeypatch):
        monkeypatch.setattr(settings.manager.security, "proxy_html_csp", DEFAULT, raising=False)
        with M.app.test_request_context("/proxy/imggen/", headers={"Host": "mgr.example:5000"}):
            pairs = proxies._csp_header_pairs("text/html; charset=utf-8")
        assert pairs and pairs[0][0] == "Content-Security-Policy"
        assert "ws://mgr.example:* wss://mgr.example:*" in pairs[0][1]
        assert "{host}" not in pairs[0][1]

    def test_non_html_gets_no_policy(self):
        with M.app.test_request_context("/proxy/imggen/x.js", headers={"Host": "mgr.example"}):
            assert proxies._csp_header_pairs("application/javascript") == []

    def test_the_pre_2026_10_07_default_in_a_toml_is_read_as_the_current_default(self, monkeypatch):
        monkeypatch.setattr(settings.manager.security, "proxy_html_csp", proxies._LEGACY_PROXY_CSP, raising=False)
        with M.app.test_request_context("/proxy/imggen/", headers={"Host": "mgr.example"}):
            (_, value), = proxies._csp_header_pairs("text/html")
        assert "sandbox allow-scripts" in value and "ws://mgr.example:*" in value
        assert " ws: wss:" not in value

    def test_a_policy_without_the_placeholder_is_passed_through(self, monkeypatch):
        monkeypatch.setattr(settings.manager.security, "proxy_html_csp", "default-src 'self'", raising=False)
        with M.app.test_request_context("/proxy/imggen/", headers={"Host": "mgr.example"}):
            assert proxies._csp_header_pairs("text/html") == [("Content-Security-Policy", "default-src 'self'")]
