"""The catch-all reverse proxies never forward the manager's own session cookie upstream."""
from __future__ import annotations

from types import SimpleNamespace

from flask import Flask

import proxies


def test_forward_headers_drop_the_manager_session_cookies():
    app = Flask(__name__)
    with app.test_request_context("/proxy/llmchat/", headers={
            "Cookie": "theme=dark; session=abc; __Secure-session=def; sessionx=keep"}):
        assert proxies._forward_headers().get("Cookie") == "theme=dark; sessionx=keep"


def test_forward_headers_omit_cookie_when_only_session_cookies_were_sent():
    app = Flask(__name__)
    with app.test_request_context("/proxy/llmchat/", headers={
            "Cookie": "__Secure-session=def; session = abc"}):
        assert "Cookie" not in proxies._forward_headers()


def test_forward_headers_follow_a_renamed_session_cookie():
    app = Flask(__name__)
    app.config["SESSION_COOKIE_NAME"] = "mgr"
    with app.test_request_context("/proxy/llmchat/", headers={
            "Cookie": "mgr=abc; __Secure-mgr=def; session=upstream"}):
        assert proxies._forward_headers().get("Cookie") == "session=upstream"


def test_alarm_engine_proxy_forwards_no_cookie(monkeypatch):
    sent = {}

    class _Upstream:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b"{}"

        def iter_content(self, chunk_size=8192):
            return iter([b"{}"])

    def _request(**kw):
        sent.update(kw["headers"])
        return _Upstream()

    monkeypatch.setattr(proxies._deps, "ctx", SimpleNamespace(
        alarm_engine_url=lambda: "http://ae.internal:8090",
        ae_session=SimpleNamespace(request=_request)), raising=False)
    app = Flask(__name__)
    with app.test_request_context("/api/alarm/alerts", headers={
            "Cookie": "session=abc; __Secure-session=def", "Accept": "application/json"}):
        resp = proxies._proxy_alarm_engine("alerts")
    assert resp.status_code == 200
    assert sent.get("Accept") == "application/json"
    assert not any(k.lower() == "cookie" for k in sent)
