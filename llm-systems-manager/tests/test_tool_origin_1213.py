"""#1213: proxied tool pages are served from their own listener, with their own
sign-in, in sandboxed frames; the dashboard refuses the old routes and foreign-origin writes."""
from __future__ import annotations

import time

import pytest

import auth
import manager_mod as M
import proxies
import settings_catalog
from config.unified_config import ManagerConfig, ManagerSecurity, settings


class TestConfigKeys:
    def test_defaults(self):
        cfg = ManagerConfig()
        assert cfg.tools_port == 5447
        assert cfg.tools_tls_port == 5448

    def test_catalog_lists_both_ports(self):
        paths = {e["path"] for e in settings_catalog.CATALOG}
        assert {"manager.tools_port", "manager.tools_tls_port"} <= paths




@pytest.fixture
def tool_ports(monkeypatch):
    monkeypatch.setattr(settings.manager, "tools_port", 5447, raising=False)
    monkeypatch.setattr(settings.manager, "tools_tls_port", 5448, raising=False)
    monkeypatch.setattr(M, "_TOOL_LISTENER_PORTS", {5447, 5448})


class TestListenerKind:
    def test_tool_port_is_tools(self, tool_ports):
        with M.app.test_request_context("/proxy/openclaw/", base_url="http://localhost:5447"):
            assert M._listener_kind() == "tools"

    def test_dashboard_port_is_dashboard(self, tool_ports):
        with M.app.test_request_context("/", base_url="http://localhost:5000"):
            assert M._listener_kind() == "dashboard"

    def test_configured_but_unstarted_port_is_dashboard(self, tool_ports, monkeypatch):
        monkeypatch.setattr(M, "_TOOL_LISTENER_PORTS", set())
        with M.app.test_request_context("/", base_url="http://localhost:5447"):
            assert M._listener_kind() == "dashboard"

    def test_enabled_follows_config(self, monkeypatch):
        monkeypatch.setattr(settings.manager, "tools_port", 0, raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        assert M._tools_origin_enabled() is False
        monkeypatch.setattr(settings.manager, "tools_tls_port", 5448, raising=False)
        assert M._tools_origin_enabled() is True


class TestToolListenerStartup:
    def _capture(self, monkeypatch):
        started = []
        self.threads = []
        monkeypatch.setattr(M, "_new_cheroot_server",
                            lambda bind, **kw: self.threads.append(kw.get("numthreads")) or ("srv", bind))
        monkeypatch.setattr(M, "_cheroot_serve_with_keepalive", lambda srv: started.append(srv))
        monkeypatch.setattr(M, "_ensure_manager_server_cert", lambda: None)
        monkeypatch.setattr(M, "_custom_manager_tls_files", lambda quiet=False: None)
        monkeypatch.setattr(M, "_TOOL_LISTENER_PORTS", set())
        return started

    def test_plain_listener_starts_and_registers_its_port(self, monkeypatch):
        started = self._capture(monkeypatch)
        monkeypatch.setattr(settings.manager, "tools_port", 5447, raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        M._maybe_start_tool_origin_servers(threaded=False)
        assert started == [("srv", ("0.0.0.0", 5447))]
        assert M._TOOL_LISTENER_PORTS == {5447}

    def test_plain_listener_uses_a_quarter_size_pool(self, monkeypatch):
        self._capture(monkeypatch)
        monkeypatch.setattr(settings.manager, "http_threads", 64, raising=False)
        monkeypatch.setattr(settings.manager, "tools_port", 5447, raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        M._maybe_start_tool_origin_servers(threaded=False)
        assert self.threads == [16]

    @pytest.mark.parametrize("http_threads,want", [(64, 16), (16, 8)])
    def test_tool_listener_threads(self, monkeypatch, http_threads, want):
        monkeypatch.setattr(settings.manager, "http_threads", http_threads, raising=False)
        assert M._tool_listener_threads() == want

    def test_colliding_port_is_refused(self, monkeypatch, caplog):
        started = self._capture(monkeypatch)
        monkeypatch.setattr(settings.manager, "tools_port", int(settings.manager.port), raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        with caplog.at_level("ERROR"):
            M._maybe_start_tool_origin_servers(threaded=False)
        assert started == [] and M._TOOL_LISTENER_PORTS == set()
        assert any("tools_port" in r.getMessage() for r in caplog.records)

    def test_disabled_ports_start_nothing(self, monkeypatch):
        started = self._capture(monkeypatch)
        monkeypatch.setattr(settings.manager, "tools_port", 0, raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        M._maybe_start_tool_origin_servers(threaded=False)
        assert started == []




def _admin(c, host="localhost", scheme="http"):
    with c.session_transaction(base_url=f"{scheme}://{host}") as s:
        s["auth_ok"] = True
        s["role"] = "admin"


class TestToolTicket:
    def test_round_trip_carries_tool_user_role(self):
        t = M._issue_tool_ticket("openclaw", "alice", "operator")
        claims = M._consume_tool_ticket(t)
        assert claims["t"] == "openclaw" and claims["u"] == "alice" and claims["r"] == "operator"

    def test_ticket_is_one_shot(self):
        t = M._issue_tool_ticket("llmchat", "alice", "admin")
        assert M._consume_tool_ticket(t) is not None
        assert M._consume_tool_ticket(t) is None

    def test_expired_ticket_is_refused(self, monkeypatch):
        t = M._issue_tool_ticket("imggen", "alice", "admin")
        later = time.time() + M._TOOL_TICKET_TTL_S + 2
        monkeypatch.setattr(M.time, "time", lambda: later)
        assert M._consume_tool_ticket(t) is None

    def test_ticket_signed_with_another_secret_is_refused(self, monkeypatch):
        t = M._issue_tool_ticket("imggen", "alice", "admin")
        monkeypatch.setattr(M, "_manager_secret", lambda: b"x" * 32)
        assert M._consume_tool_ticket(t) is None

    @pytest.mark.parametrize("bad", ["", "a", "a.b.c", "notb64.sig", None, "x.é"])
    def test_malformed_is_refused(self, bad):
        assert M._consume_tool_ticket(bad) is None


class TestToolCookie:
    def test_exchange_sets_cookie_and_redirects_without_the_ticket(self):
        t = M._issue_tool_ticket("openclaw", "alice", "admin")
        with M.app.test_request_context(f"/proxy/openclaw/?tool_ticket={t}&x=1", base_url="http://localhost:5447"):
            resp = M._exchange_tool_ticket(t)
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/proxy/openclaw/"
        cookie = resp.headers.get("Set-Cookie", "")
        assert cookie.startswith(M._TOOL_COOKIE + "=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie

    def test_tls_exchange_sets_the_secure_prefixed_cookie(self):
        t = M._issue_tool_ticket("openclaw", "alice", "admin")
        with M.app.test_request_context(f"/proxy/openclaw/?tool_ticket={t}", base_url="https://localhost:5448"):
            resp = M._exchange_tool_ticket(t)
        cookie = resp.headers.get("Set-Cookie", "")
        assert cookie.startswith(f"__Secure-{M._TOOL_COOKIE}=") and "Secure" in cookie
        value = cookie.split(";")[0].split("=", 1)[1]
        with M.app.test_request_context("/proxy/openclaw/", base_url="https://localhost:5448",
                                        headers={"Cookie": f"__Secure-{M._TOOL_COOKIE}={value}"}):
            assert M._tool_cookie_claims() == ("alice", "admin")
        with M.app.test_request_context("/proxy/openclaw/", base_url="http://localhost:5447",
                                        headers={"Cookie": f"__Secure-{M._TOOL_COOKIE}={value}"}):
            assert M._tool_cookie_claims() is None

    def test_cookie_claims_read_back(self):
        t = M._issue_tool_ticket("openclaw", "alice", "operator")
        with M.app.test_request_context("/proxy/openclaw/?tool_ticket=" + t):
            resp = M._exchange_tool_ticket(t)
        value = resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]
        with M.app.test_request_context("/proxy/openclaw/", headers={"Cookie": f"{M._TOOL_COOKIE}={value}"}):
            assert M._tool_cookie_claims() == ("alice", "operator")

    def test_bad_cookie_is_none(self):
        with M.app.test_request_context("/proxy/openclaw/", headers={"Cookie": f"{M._TOOL_COOKIE}=junk.junk"}):
            assert M._tool_cookie_claims() is None
        with M.app.test_request_context("/proxy/openclaw/"):
            assert M._tool_cookie_claims() is None

    def test_invalid_ticket_exchange_is_none(self):
        with M.app.test_request_context("/proxy/openclaw/?tool_ticket=bad"):
            assert M._exchange_tool_ticket("bad") is None


class TestToolTicketRoute:
    def test_plain_dashboard_gets_plain_tool_url(self, tool_ports):
        with M.app.test_client() as c:
            _admin(c, "mgr.example:5000")
            r = c.get("/api/tool-ticket?tool=openclaw", headers={"Host": "mgr.example:5000"})
        assert r.status_code == 200, r.data
        j = r.get_json()
        assert j["port"] == 5447 and j["ttl_s"] == 60
        assert j["url"].startswith("http://mgr.example:5447/proxy/openclaw/?tool_ticket=")

    def test_https_dashboard_gets_tls_tool_url(self, tool_ports):
        with M.app.test_client() as c:
            _admin(c, "mgr.example:5443", "https")
            r = c.get("/api/tool-ticket?tool=llmchat", headers={"Host": "mgr.example:5443"},
                      base_url="https://mgr.example:5443")
        assert r.get_json()["url"].startswith("https://mgr.example:5448/proxy/llmchat/?tool_ticket=")

    def test_ipv6_host_is_bracketed(self, tool_ports):
        with M.app.test_client() as c:
            _admin(c)
            sess = c.get_cookie("session").value
        r = M.app.test_client(use_cookies=False).get(
            "/api/tool-ticket?tool=imggen",
            headers={"Host": "[fd00::7]:5000", "Cookie": f"session={sess}"})
        assert r.get_json()["url"].startswith("http://[fd00::7]:5447/proxy/imggen/")

    def test_disabled_port_is_409(self, tool_ports, monkeypatch):
        monkeypatch.setattr(settings.manager, "tools_port", 0, raising=False)
        with M.app.test_client() as c:
            _admin(c, "mgr.example:5000")
            r = c.get("/api/tool-ticket?tool=openclaw", headers={"Host": "mgr.example:5000"})
        assert r.status_code == 409 and r.get_json()["error"] == "tool origin disabled"

    def test_unknown_tool_is_404(self, tool_ports):
        with M.app.test_client() as c:
            _admin(c)
            assert c.get("/api/tool-ticket?tool=shell").status_code == 404

    def test_https_flag_gets_tls_tool_url_on_a_plain_request(self, tool_ports):
        with M.app.test_client() as c:
            _admin(c, "mgr.example")
            r = c.get("/api/tool-ticket?tool=openclaw&https=1", headers={"Host": "mgr.example"})
        assert r.get_json()["url"].startswith("https://mgr.example:5448/proxy/openclaw/?tool_ticket=")
        assert r.get_json()["port"] == 5448

    def test_no_role_is_401(self, tool_ports):
        with M.app.test_request_context("/api/tool-ticket?tool=openclaw", base_url="http://mgr.example:5000"):
            resp, status = M.tool_ticket()
        assert status == 401 and resp.get_json() == {"ok": False, "error": "authentication required"}

    def test_needs_a_session(self, tool_ports):
        with M.app.test_client() as c:
            assert c.get("/api/tool-ticket?tool=openclaw").status_code == 401


class TestToolFrameAncestors:
    @pytest.mark.parametrize("host,want", [
        ("mgr.example:5447", "mgr.example"),
        ("10.0.0.5:5447", "10.0.0.5"),
        ("[fd00::7]:5447", "[fd00::7]"),
    ])
    def test_same_host_any_port_either_scheme(self, host, want):
        with M.app.test_request_context("/", headers={"Host": host}):
            assert M._tool_frame_ancestors() == (
                f"http://{want} https://{want} http://{want}:* https://{want}:*")



TOOLS_URL = "http://localhost:5447"


@pytest.fixture(autouse=True)
def _pin_tool_hooks(monkeypatch):
    """Points auth.tool_origin at manager_mod; a second manager import rewires it."""
    monkeypatch.setattr(auth.tool_origin, "listener_kind", M._listener_kind)
    monkeypatch.setattr(auth.tool_origin, "enabled", M._tools_origin_enabled)
    monkeypatch.setattr(auth.tool_origin, "tools_port", M._tools_port_for_request)
    monkeypatch.setattr(auth.tool_origin, "exchange_ticket", M._exchange_tool_ticket)
    monkeypatch.setattr(auth.tool_origin, "cookie_claims", M._tool_cookie_claims)


def _tool_cookie_for(user="alice", role="admin"):
    t = M._issue_tool_ticket("openclaw", user, role)
    with M.app.test_request_context("/proxy/openclaw/?tool_ticket=" + t):
        resp = M._exchange_tool_ticket(t)
    return resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1]


@pytest.fixture
def proxy_stub(monkeypatch):
    """Every proxy target resolves and the helpers answer 200 with the role seen."""
    from flask import g as _g, jsonify as _j
    monkeypatch.setattr(proxies, "resolve_proxy_target", lambda name: "http://10.0.0.7:1234")
    for name in ("_proxy_openclaw", "_proxy_llmchat", "_proxy_imggen", "_proxy_sdcpp"):
        monkeypatch.setattr(proxies, name, lambda path, base: _j({"role": getattr(_g, "auth_role", None)}))


class TestToolListenerGate:
    def test_ticket_exchange_sets_cookie(self, tool_ports, proxy_stub):
        t = M._issue_tool_ticket("openclaw", "alice", "admin")
        with M.app.test_client() as c:
            r = c.get(f"/proxy/openclaw/?tool_ticket={t}", base_url=TOOLS_URL)
        assert r.status_code == 302 and r.headers["Location"] == "/proxy/openclaw/"
        assert M._TOOL_COOKIE in r.headers.get("Set-Cookie", "")

    def test_bad_ticket_is_403(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            r = c.get("/proxy/openclaw/?tool_ticket=bad", base_url=TOOLS_URL)
        assert r.status_code == 403 and "ticket" in r.get_json()["error"]

    def test_cookie_admits_and_sets_role(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            c.set_cookie(M._TOOL_COOKIE, _tool_cookie_for(role="operator"))
            r = c.get("/proxy/llmchat/", base_url=TOOLS_URL)
        assert r.status_code == 200 and r.get_json()["role"] == "operator"

    def test_no_cookie_is_403_without_redirect(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            r = c.get("/proxy/llmchat/", base_url=TOOLS_URL)
        assert r.status_code == 403 and r.get_json()["tool_auth_required"] is True

    def test_dashboard_session_is_ignored_on_the_tool_listener(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            _admin(c)
            r = c.get("/proxy/openclaw/", base_url=TOOLS_URL)
        assert r.status_code == 403

    def test_other_paths_are_404(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            c.set_cookie(M._TOOL_COOKIE, _tool_cookie_for())
            assert c.get("/", base_url=TOOLS_URL).status_code == 404
            assert c.get("/api/admin/system-health", base_url=TOOLS_URL).status_code == 404
            assert c.get("/login", base_url=TOOLS_URL).status_code == 404

    def test_openclaw_ws_ticket_is_served_under_the_cookie(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            c.set_cookie(M._TOOL_COOKIE, _tool_cookie_for())
            r = c.get("/api/openclaw-ws-ticket", base_url=TOOLS_URL)
        assert r.status_code == 200 and "ticket" in r.get_json()

    def test_sdcpp_is_served_under_the_cookie(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            c.set_cookie(M._TOOL_COOKIE, _tool_cookie_for())
            assert c.post("/sdcpp/txt2img", json={}, base_url=TOOLS_URL).status_code == 200

    def _ping(self, c, origin):
        return c.get("/tools-ping", base_url=TOOLS_URL, headers={"Host": "mgr.example:5447", "Origin": origin})

    def test_tools_ping_grants_cors_to_the_same_host_any_port_or_scheme(self, tool_ports):
        with M.app.test_client() as c:
            for origin in ("https://mgr.example", "http://mgr.example:9999", "http://MGR.example:5000"):
                r = self._ping(c, origin)
                assert r.status_code == 200 and r.headers["Access-Control-Allow-Origin"] == origin

    def test_tools_ping_refuses_other_hosts(self, tool_ports):
        with M.app.test_client() as c:
            r = self._ping(c, "http://evil.example")
        assert r.status_code == 200 and "Access-Control-Allow-Origin" not in r.headers

    def test_tools_ping_is_never_cached_and_varies_on_origin(self, tool_ports):
        with M.app.test_client() as c:
            for origin in ("http://evil.example", "https://mgr.example"):
                r = self._ping(c, origin)
                assert r.headers["Cache-Control"] == "no-store" and "Origin" in r.headers["Vary"]

    def test_dashboard_session_cookie_is_not_reissued(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            with c.session_transaction(base_url=TOOLS_URL) as s:
                s["auth_ok"] = True
                s["role"] = "admin"
                s.permanent = True
            c.set_cookie(M._TOOL_COOKIE, _tool_cookie_for())
            r = c.get("/proxy/llmchat/", base_url=TOOLS_URL)
        assert r.status_code == 200
        name = M.app.config["SESSION_COOKIE_NAME"]
        assert not any(v.startswith(f"{name}=") or v.startswith(f"__Secure-{name}=")
                       for v in r.headers.getlist("Set-Cookie"))


class TestDashboardRefusals:
    def test_proxy_and_sdcpp_are_404_when_enabled(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            _admin(c)
            r = c.get("/proxy/openclaw/")
            assert r.status_code == 404 and "5447" in r.get_json()["error"]
            assert c.get("/sdcpp/models").status_code == 404

    def test_proxy_still_served_when_disabled(self, proxy_stub, monkeypatch):
        monkeypatch.setattr(settings.manager, "tools_port", 0, raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        with M.app.test_client() as c:
            _admin(c)
            assert c.get("/proxy/openclaw/").status_code == 200


class TestOriginCheck:
    def _post(self, c, **headers):
        return c.post("/api/tool-ticket", headers={"Host": "mgr.example:5000", **headers})

    def test_same_origin_write_passes_the_gate(self):
        with M.app.test_client() as c:
            _admin(c, host="mgr.example:5000")
            r = self._post(c, Origin="http://mgr.example:5000")
        assert r.status_code == 405  # the gate let it through; the route has no POST

    def test_foreign_port_origin_is_403(self):
        with M.app.test_client() as c:
            _admin(c, host="mgr.example:5000")
            r = self._post(c, Origin="http://mgr.example:5447")
        assert r.status_code == 403 and r.get_json()["error"] == "cross-origin request refused"

    def test_null_origin_is_refused(self):
        with M.app.test_client() as c:
            _admin(c, host="mgr.example:5000")
            assert self._post(c, Origin="null").status_code == 403

    def test_scheme_only_difference_passes(self):
        with M.app.test_client() as c:
            _admin(c, host="mgr.example:5000")
            assert self._post(c, Origin="https://mgr.example:5000").status_code == 405

    def test_no_origin_passes(self):
        with M.app.test_client() as c:
            _admin(c, host="mgr.example:5000")
            assert self._post(c).status_code == 405

    def test_safe_methods_are_not_checked(self):
        with M.app.test_client() as c:
            _admin(c, host="mgr.example:5000")
            r = c.get("/api/tool-ticket?tool=openclaw", headers={"Host": "mgr.example:5000",
                                                                 "Origin": "http://mgr.example:5447"})
        assert r.status_code != 403



DEFAULT_CSP = ManagerSecurity().proxy_html_csp


MGR_ANCESTORS = "http://mgr.example https://mgr.example http://mgr.example:* https://mgr.example:*"
LOCAL_ANCESTORS = "http://localhost https://localhost http://localhost:* https://localhost:*"


class TestToolOriginPolicy:
    def _pairs(self, monkeypatch, kind, csp=DEFAULT_CSP):
        monkeypatch.setattr(settings.manager.security, "proxy_html_csp", csp, raising=False)
        monkeypatch.setattr(proxies._deps, "listener_kind", lambda: kind, raising=False)
        monkeypatch.setattr(proxies._deps, "tool_frame_ancestors", M._tool_frame_ancestors, raising=False)
        with M.app.test_request_context("/proxy/imggen/", headers={"Host": "mgr.example:5447"}):
            return proxies._csp_header_pairs("text/html")

    def test_tool_listener_frames_from_the_same_host(self, monkeypatch):
        csp = self._pairs(monkeypatch, "tools")[0][1]
        assert f"frame-ancestors {MGR_ANCESTORS}" in csp
        assert "frame-ancestors 'self'" not in csp

    def test_dashboard_listener_is_unchanged(self, monkeypatch):
        assert "frame-ancestors 'self'" in self._pairs(monkeypatch, "dashboard")[0][1]

    def test_custom_policy_without_frame_ancestors_gets_them_on_the_tool_listener(self, monkeypatch):
        assert self._pairs(monkeypatch, "tools", "default-src 'self'") == [
            ("Content-Security-Policy", f"default-src 'self'; frame-ancestors {MGR_ANCESTORS}")]

    def test_custom_policy_on_the_dashboard_listener_passes_through(self, monkeypatch):
        assert self._pairs(monkeypatch, "dashboard", "default-src 'self'") == [
            ("Content-Security-Policy", "default-src 'self'")]


@pytest.fixture
def html_proxy_stub(monkeypatch):
    """Proxy helpers answer an HTML page carrying the proxy CSP pairs, as the real helpers do."""
    from flask import Response
    monkeypatch.setattr(proxies, "resolve_proxy_target", lambda name: "http://10.0.0.7:1234")
    monkeypatch.setattr(proxies._deps, "listener_kind", M._listener_kind, raising=False)
    monkeypatch.setattr(proxies._deps, "tool_frame_ancestors", M._tool_frame_ancestors, raising=False)
    monkeypatch.setattr(proxies, "_proxy_llmchat", lambda path, base: Response(
        "<html></html>", content_type="text/html", headers=proxies._csp_header_pairs("text/html")))


class TestToolListenerFramingHeaders:
    def _get(self, monkeypatch, csp):
        monkeypatch.setattr(settings.manager.security, "proxy_html_csp", csp, raising=False)
        with M.app.test_client() as c:
            c.set_cookie(M._TOOL_COOKIE, _tool_cookie_for())
            return c.get("/proxy/llmchat/", base_url=TOOLS_URL)

    def test_empty_policy_frames_from_the_same_host_without_x_frame_options(self, tool_ports, html_proxy_stub,
                                                                           monkeypatch):
        r = self._get(monkeypatch, "")
        assert r.status_code == 200
        assert r.headers["Content-Security-Policy"] == f"frame-ancestors {LOCAL_ANCESTORS}"
        assert "X-Frame-Options" not in r.headers

    def test_custom_policy_gets_frame_ancestors_appended(self, tool_ports, html_proxy_stub, monkeypatch):
        r = self._get(monkeypatch, "default-src 'self'")
        assert r.headers["Content-Security-Policy"] == f"default-src 'self'; frame-ancestors {LOCAL_ANCESTORS}"
        assert "X-Frame-Options" not in r.headers

    def test_dashboard_listener_keeps_self_and_sameorigin(self, html_proxy_stub, monkeypatch):
        monkeypatch.setattr(settings.manager, "tools_port", 0, raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        monkeypatch.setattr(settings.manager.security, "proxy_html_csp", "", raising=False)
        with M.app.test_client() as c:
            _admin(c)
            r = c.get("/proxy/llmchat/")
        assert r.status_code == 200
        assert r.headers["Content-Security-Policy"] == "frame-ancestors 'self'"
        assert r.headers["X-Frame-Options"] == "SAMEORIGIN"


class TestUpstreamCookie:
    def test_session_and_tool_cookies_are_stripped(self):
        name = M.app.config["SESSION_COOKIE_NAME"]
        with M.app.test_request_context("/"):
            out = proxies._upstream_cookie(f"{name}=a; llmsys_tools=b; __Secure-llmsys_tools=c; other=d")
        assert out == "other=d"


class TestOpenClawFetchShim:
    def test_shim_rewrites_root_absolute_manager_urls(self):
        js = proxies._build_openclaw_ws_patch("10.0.0.7:18789", "18789", "ws://mgr.example:5444/ws/openclaw", "t1",
                                              page_host="mgr.example")
        assert "window.fetch=" in js
        assert "mgr\\.example" in js and "/proxy/openclaw/" in js

    def test_shim_rewrites_request_objects_only_for_get_and_head(self):
        js = proxies._build_openclaw_ws_patch("10.0.0.7:18789", "18789", "", "", page_host="mgr.example")
        assert "i.method" in js and "'GET'" in js and "'HEAD'" in js
        assert "new Request(fixF(i.url),i)" in js

    def test_shim_without_page_host_leaves_fetch_alone(self):
        js = proxies._build_openclaw_ws_patch("10.0.0.7:18789", "18789", "", "")
        assert "window.fetch=" not in js

    def test_shim_ignores_unsafe_page_host(self):
        js = proxies._build_openclaw_ws_patch("10.0.0.7:18789", "18789", "", "", page_host="mgr</script><script>")
        assert "window.fetch=" not in js
        assert "mgr</script>" not in js and js.count("</script>") == 1


# ── #1288 follow-ups ───────────────────────────────────────────────────
class TestTicketToolClaim:
    def test_ticket_for_another_tool_is_refused(self, tool_ports, proxy_stub):
        t = M._issue_tool_ticket("openclaw", "alice", "admin")
        with M.app.test_client() as c:
            r = c.get(f"/proxy/llmchat/?tool_ticket={t}", base_url=TOOLS_URL)
        assert r.status_code == 403 and "ticket" in r.get_json()["error"]
        assert "Set-Cookie" not in r.headers

    def test_ticket_for_a_sub_path_of_its_tool_is_exchanged(self, tool_ports, proxy_stub):
        t = M._issue_tool_ticket("imggen", "alice", "admin")
        with M.app.test_client() as c:
            r = c.get(f"/proxy/imggen/x/y?tool_ticket={t}", base_url=TOOLS_URL)
        assert r.status_code == 302 and r.headers["Location"] == "/proxy/imggen/"

    def test_exchange_on_the_wrong_path_is_none_and_spends_the_ticket(self):
        t = M._issue_tool_ticket("openclaw", "alice", "admin")
        with M.app.test_request_context("/proxy/llmchat/?tool_ticket=" + t):
            assert M._exchange_tool_ticket(t) is None
        with M.app.test_request_context("/proxy/openclaw/?tool_ticket=" + t):
            assert M._exchange_tool_ticket(t) is None


class TestDashboardRefusalPortText:
    def test_https_dashboard_names_the_tls_tool_port(self, tool_ports, proxy_stub):
        with M.app.test_client() as c:
            _admin(c, scheme="https")
            r = c.get("/proxy/openclaw/", base_url="https://localhost:5000")
        assert r.status_code == 404 and "5448" in r.get_json()["error"]

    def test_https_dashboard_falls_back_to_the_plain_port_when_tls_is_off(self, tool_ports, proxy_stub, monkeypatch):
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        with M.app.test_client() as c:
            _admin(c, scheme="https")
            r = c.get("/proxy/openclaw/", base_url="https://localhost:5000")
        assert "5447" in r.get_json()["error"]

    def test_plain_dashboard_names_the_tls_port_when_only_tls_is_on(self, tool_ports, proxy_stub, monkeypatch):
        monkeypatch.setattr(settings.manager, "tools_port", 0, raising=False)
        with M.app.test_client() as c:
            _admin(c)
            r = c.get("/proxy/openclaw/")
        assert "5448" in r.get_json()["error"]


class TestOpenClawShimGatewayHost:
    BAD = "10.0.0.7:18789</script><script>alert(1)//"

    def test_unsafe_gateway_host_is_not_embedded_without_the_bridge(self):
        js = proxies._build_openclaw_ws_patch(self.BAD, "18789", "", "", page_host="mgr.example")
        assert "alert(1)" not in js and js.count("</script>") == 1
        assert "window.WebSocket=" in js and "window.fetch=" in js

    def test_unsafe_gateway_host_is_not_embedded_with_the_bridge(self):
        js = proxies._build_openclaw_ws_patch(self.BAD, "18789", "ws://mgr.example:5444/ws/openclaw", "t1")
        assert "alert(1)" not in js and js.count("</script>") == 1
        assert "var B=" in js and "M.test(u)" in js and "G.test(u)" not in js

    def test_unsafe_port_is_not_embedded(self):
        js = proxies._build_openclaw_ws_patch("10.0.0.7:1", "1</script>", "", "")
        assert js.count("</script>") == 1

    def test_safe_ipv6_gateway_host_still_rewrites(self):
        js = proxies._build_openclaw_ws_patch("[fd00::7]:18789", "18789", "ws://mgr.example:5444/ws/openclaw", "t1")
        assert "G.test(u)" in js and "fd00" in js


class TestToolPortCollisions(TestToolListenerStartup):
    def _refused(self, monkeypatch, caplog, plain, tls=0, env_tls=""):
        started = self._capture(monkeypatch)
        monkeypatch.setattr(settings.manager, "tools_port", plain, raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", tls, raising=False)
        if env_tls:
            monkeypatch.setenv("MANAGER_TLS_PORT", env_tls)
        else:
            monkeypatch.delenv("MANAGER_TLS_PORT", raising=False)
        with caplog.at_level("ERROR"):
            M._maybe_start_tool_origin_servers(threaded=False)
        return started == [] and M._TOOL_LISTENER_PORTS == set() and \
            any("tools_port" in r.getMessage() for r in caplog.records)

    def test_env_tls_port_override_collides(self, monkeypatch, caplog):
        monkeypatch.setattr(settings.manager, "tls_port", 0, raising=False)
        assert self._refused(monkeypatch, caplog, plain=0, tls=5443, env_tls="5443")

    def test_ws_proxy_port_collides(self, monkeypatch, caplog):
        monkeypatch.setattr(settings.manager, "ws_proxy_port", 5444, raising=False)
        assert self._refused(monkeypatch, caplog, plain=5444)

    def test_ws_proxy_tls_port_collides(self, monkeypatch, caplog):
        monkeypatch.setattr(settings.manager, "ws_proxy_tls_port", 5446, raising=False)
        assert self._refused(monkeypatch, caplog, plain=0, tls=5446)

    def test_stream_proxy_port_collides(self, monkeypatch, caplog):
        monkeypatch.setattr(settings.manager, "stream_proxy_port", 5445, raising=False)
        assert self._refused(monkeypatch, caplog, plain=5445)

    def test_defaults_do_not_collide(self, monkeypatch):
        started = self._capture(monkeypatch)
        monkeypatch.delenv("MANAGER_TLS_PORT", raising=False)
        monkeypatch.setattr(settings.manager, "tools_port", 5447, raising=False)
        monkeypatch.setattr(settings.manager, "tools_tls_port", 0, raising=False)
        M._maybe_start_tool_origin_servers(threaded=False)
        assert started == [("srv", ("0.0.0.0", 5447))]


class TestToolListenerGateCoverage:
    def test_operator_role_is_refused_on_a_denied_tool_path(self, tool_ports, proxy_stub, monkeypatch):
        monkeypatch.setattr(auth, "_operator_denied", lambda path: path.startswith("/proxy/openclaw/"))
        with M.app.test_client() as c:
            c.set_cookie(M._TOOL_COOKIE, _tool_cookie_for(role="operator"))
            r = c.get("/proxy/openclaw/", base_url=TOOLS_URL)
            assert r.status_code == 403 and r.get_json()["role_denied"] is True
            assert c.get("/proxy/llmchat/", base_url=TOOLS_URL).status_code == 200

    def test_agent_bearer_is_ignored_on_the_tool_listener(self, tool_ports, proxy_stub, monkeypatch):
        monkeypatch.setattr(auth, "_agent_by_token", lambda tok: {"id": "a1"} if tok == "tok" else None)
        with M.app.test_client() as c:
            hdr = {"Authorization": "Bearer tok"}
            r = c.get("/proxy/openclaw/", base_url=TOOLS_URL, headers=hdr)
            assert r.status_code == 403 and r.get_json()["tool_auth_required"] is True
            assert c.get("/api/metrics", base_url=TOOLS_URL, headers=hdr).status_code == 404


class TestOriginCheckIPv6:
    def _foreign(self, origin):
        with M.app.test_request_context("/api/tool-ticket", method="POST", base_url="http://[fd00::7]:5000",
                                        headers={"Origin": origin}):
            return auth._foreign_origin()

    def test_same_ipv6_origin_passes(self):
        assert self._foreign("http://[fd00::7]:5000") is False

    def test_ipv6_origin_on_the_tool_port_is_foreign(self):
        assert self._foreign("http://[fd00::7]:5447") is True
