"""#433: /health must not report InfluxDB "connected" on a bare port check.

InfluxDB's /ping answers 204 with no auth, so a freshly-installed server
with REPLACE_ME (or wrong) tokens used to show as connected in the admin
tab and Database Performance card. _ping_influxdb now follows the ping
with an authenticated read and reports tokens_unset / auth_failed.
"""
from __future__ import annotations

from config.unified_config import settings
from backend import alarm_engine as ae


class _Resp:
    def __init__(self, status_code=204, headers=None):
        self.status_code = status_code
        self.headers = headers or {"X-Influxdb-Version": "v2.7.0"}


def _wire(monkeypatch, token, bucket_status=200, ping_exc=None, bucket_exc=None):
    monkeypatch.setattr(settings.influxdb, "host", "influx-test", raising=False)
    monkeypatch.setattr(settings.influxdb.tokens, "metrics", token, raising=False)

    def fake_get(url, **kw):
        if url.endswith("/ping"):
            if ping_exc:
                raise ping_exc
            return _Resp(204)
        if "/api/v2/buckets" in url:
            if bucket_exc:
                raise bucket_exc
            return _Resp(bucket_status)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("requests.get", fake_get)


def test_placeholder_token_is_not_connected(monkeypatch):
    _wire(monkeypatch, "REPLACE_ME")
    status, latency, version = ae._ping_influxdb()
    assert status == "tokens_unset"
    assert latency is not None
    assert version == "v2.7.0"


def test_empty_token_is_not_connected(monkeypatch):
    _wire(monkeypatch, "")
    assert ae._ping_influxdb()[0] == "tokens_unset"


def test_valid_token_connected(monkeypatch):
    _wire(monkeypatch, "tok123", bucket_status=200)
    assert ae._ping_influxdb()[0] == "connected"


def test_wrong_token_auth_failed(monkeypatch):
    _wire(monkeypatch, "tok123", bucket_status=401)
    assert ae._ping_influxdb()[0] == "auth_failed"


def test_server_down_unreachable(monkeypatch):
    _wire(monkeypatch, "tok123", ping_exc=ConnectionError("refused"))
    assert ae._ping_influxdb()[0].startswith("unreachable")


def test_auth_probe_error_unreachable(monkeypatch):
    _wire(monkeypatch, "tok123", bucket_exc=TimeoutError("slow"))
    assert ae._ping_influxdb()[0].startswith("unreachable")
