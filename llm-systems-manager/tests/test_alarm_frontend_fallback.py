"""Split-install /alarm/<path> fallback: only the index, js/ and css/ assets and
the health probe reach the alarm engine, and never with the AE bearer."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
import requests
from flask import Flask

import proxies


class _CaptureAdapter(requests.adapters.BaseAdapter):
    """Records each prepared request and answers 200 without any network."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        resp = requests.Response()
        resp.status_code = 200
        resp.url = request.url
        resp.request = request
        resp.headers["content-type"] = "text/plain"
        resp.raw = io.BytesIO(b"upstream-body")
        return resp

    def close(self):
        pass


@pytest.fixture
def split_client(monkeypatch, tmp_path):
    adapter = _CaptureAdapter()
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = "Bearer mgmt-secret"
    session.mount("http://", adapter)
    ctx = SimpleNamespace(ae_session=session,
                          alarm_engine_url=lambda: "http://ae.test:8081",
                          require_admin=lambda: None)
    monkeypatch.setattr(proxies, "_deps", SimpleNamespace())
    app = Flask(__name__)
    proxies.register_routes(app, ctx, repo_root=tmp_path,
                            install_topology=lambda: {"split": True},
                            request_host_no_port=lambda: "mgr.test",
                            rewrite_loopback_host=lambda url, host: url)
    return app.test_client(), adapter


@pytest.mark.parametrize("path", [
    "health",
    "js/api.js",
    "js/vendor/chart.umd.min.js",
    "css/main.css",
])
def test_allowed_paths(path):
    assert proxies._alarm_frontend_path_allowed(path) is True


@pytest.mark.parametrize("path", [
    "api/alarm/admin/config",
    "api/alarm/admin/log/tail",
    "api/alarm/dbstats/sqlite",
    "ws",
    "docs",
    "openapi.json",
    "health/",
    "js",
    "js/",
    "js/../api/alarm/admin/config",
    "js/./api.js",
    "js//api.js",
    "js/.hidden",
    "js/%2e%2e/api/alarm/admin/config",
    "js/..%2fapi/alarm/admin/config",
    "js\\..\\api",
    "js/api.js?x=1",
    "js/api.js#x",
    "js/api.js\n",
    "jsx/api.js",
    "",
])
def test_rejected_paths(path):
    assert proxies._alarm_frontend_path_allowed(path) is False


@pytest.mark.parametrize("url", [
    "/alarm/api/alarm/admin/config",
    "/alarm/api/alarm/admin/log/tail",
    "/alarm/api/alarm/dbstats/sqlite",
    "/alarm/openapi.json",
    "/alarm/js/%2e%2e/api/alarm/admin/config",
    "/alarm/js/%252e%252e/api/alarm/admin/config",
])
def test_non_asset_paths_are_not_forwarded(split_client, url):
    client, adapter = split_client
    r = client.get(url)
    assert r.status_code == 404
    assert adapter.sent == []


@pytest.mark.parametrize("url,upstream", [
    ("/alarm/", "http://ae.test:8081/"),
    ("/alarm/index.html", "http://ae.test:8081/"),
    ("/alarm/js/vendor/chart.umd.min.js", "http://ae.test:8081/js/vendor/chart.umd.min.js"),
    ("/alarm/css/main.css?v=20260911g", "http://ae.test:8081/css/main.css"),
    ("/alarm/health", "http://ae.test:8081/health"),
])
def test_assets_are_forwarded_without_the_bearer(split_client, url, upstream):
    client, adapter = split_client
    r = client.get(url)
    assert r.status_code == 200
    assert r.get_data() == b"upstream-body"
    assert [req.url for req in adapter.sent] == [upstream]
    assert "Authorization" not in adapter.sent[0].headers
