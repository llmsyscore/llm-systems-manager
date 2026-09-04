"""The OpenClaw proxy rewrites the control UI's root-absolute asset URLs to
/proxy/openclaw, stamps the mount base path, and pins upstream Accept-Encoding."""
from __future__ import annotations

import proxies

HTML = (
    '<!doctype html>\n'
    '<html data-openclaw-control-ui-base-path="" lang="en"><head>'
    '<link rel="icon" href="/favicon.svg" />'
    '<link rel="modulepreload" href="/assets/lit-runtime-B.js" />'
    '<link rel="stylesheet" href="/assets/control-ui-core-C.css" />'
    '<script type="module" src="/assets/index-A.js"></script>'
    "<script src='/assets/single-quoted.js'></script>"
    '<link href="//cdn.example/x.css" />'
    '<a href="https://docs.openclaw.ai/web/control-ui">docs</a>'
    '<img src="data:image/png;base64,AAAA" />'
    '<img data-src="/lazy.png" />'
    '<script>a.href="/login";b.src="/x.js"</script>'
    '</head><body></body></html>'
)


def test_root_absolute_href_and_src_get_the_prefix():
    out = proxies._rewrite_openclaw_html(HTML)
    assert 'href="/proxy/openclaw/favicon.svg"' in out
    assert 'href="/proxy/openclaw/assets/lit-runtime-B.js"' in out
    assert 'href="/proxy/openclaw/assets/control-ui-core-C.css"' in out
    assert 'src="/proxy/openclaw/assets/index-A.js"' in out
    assert "src='/proxy/openclaw/assets/single-quoted.js'" in out


def test_non_root_urls_are_left_alone():
    out = proxies._rewrite_openclaw_html(HTML)
    assert 'href="//cdn.example/x.css"' in out
    assert 'href="https://docs.openclaw.ai/web/control-ui"' in out
    assert 'src="data:image/png;base64,AAAA"' in out
    assert 'data-src="/lazy.png"' in out
    assert 'a.href="/login";b.src="/x.js"' in out


def test_empty_base_path_is_stamped_with_the_prefix():
    out = proxies._rewrite_openclaw_html(HTML)
    assert 'data-openclaw-control-ui-base-path="/proxy/openclaw"' in out


def test_configured_base_path_is_not_overwritten():
    html = HTML.replace('data-openclaw-control-ui-base-path=""', 'data-openclaw-control-ui-base-path="/claw"')
    out = proxies._rewrite_openclaw_html(html)
    assert 'data-openclaw-control-ui-base-path="/claw"' in out


def test_rewrite_is_idempotent():
    once = proxies._rewrite_openclaw_html(HTML)
    assert proxies._rewrite_openclaw_html(once) == once


def test_forward_headers_pin_accept_encoding_to_what_urllib3_decodes():
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/proxy/openclaw/", headers={
            "Host": "manager:5000", "Accept-Encoding": "gzip, deflate, br, zstd",
            "Cookie": "session=abc", "Content-Length": "0", "Transfer-Encoding": "chunked"}):
        out = proxies._forward_headers()
    assert out["Accept-Encoding"] == "gzip, deflate"
    assert out.get("Cookie") == "session=abc"
    for k in out:
        assert k.lower() not in ("host", "content-length", "transfer-encoding")


def test_ws_shim_dials_the_bridge_with_a_ticket_when_the_bridge_is_on():
    js = proxies._build_openclaw_ws_patch("10.0.0.5:18789", "18789", "wss://mgr:5446/ws/openclaw", "1.n.s")
    assert '"wss://mgr:5446/ws/openclaw"' in js
    assert '"1.n.s"' in js
    assert "/api/openclaw-ws-ticket" in js
    assert "10\\.0\\.0\\.5:18789" in js  # gateway-host dials are caught too
    assert "$1" not in js  # no direct host rewrite


def test_ws_shim_rewrites_the_host_directly_without_a_bridge():
    js = proxies._build_openclaw_ws_patch("10.0.0.5:18789", "18789")
    assert "10.0.0.5:18789" in js
    assert "openclaw-ws-ticket" not in js


def test_openclaw_ws_url_for_browser_prefers_wss_on_https(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(proxies, "settings", SimpleNamespace(manager=SimpleNamespace(ws_proxy_port=5444)))
    monkeypatch.setattr(proxies, "_deps", SimpleNamespace(
        wss_bridge_port=lambda: 5446, request_is_https=lambda: True, request_host_no_port=lambda: "mgr"))
    assert proxies.openclaw_ws_url_for_browser() == "wss://mgr:5446/ws/openclaw"
    proxies._deps.request_is_https = lambda: False
    assert proxies.openclaw_ws_url_for_browser() == "ws://mgr:5444/ws/openclaw"
    proxies.settings.manager.ws_proxy_port = 0
    assert proxies.openclaw_ws_url_for_browser() == ""
