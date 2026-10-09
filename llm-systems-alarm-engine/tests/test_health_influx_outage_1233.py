"""#1233: /health says degraded while InfluxDB is down; writes/s counts accepted writes only."""
from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend import alarm_engine as ae
from backend.rate_counter import INFLUX_WRITES
from backend.storage import influxdb_client as ic
from backend.storage.influxdb_client import InfluxDBClient


@pytest.fixture(autouse=True)
def _reset_counter():
    INFLUX_WRITES.reset()
    yield
    INFLUX_WRITES.reset()


def _health(monkeypatch, host, ping):
    monkeypatch.setattr(ae.settings.influxdb, "host", host, raising=False)
    monkeypatch.setattr(ae, "_ping_influxdb", lambda: ping)
    return asyncio.run(ae.health_check())


def test_status_degraded_while_influxdb_unreachable(monkeypatch):
    body = _health(monkeypatch, "influx.test", ("unreachable: ConnectionError", None, None))
    assert body["status"] == "degraded"
    assert body["components"]["influxdb"] == "unreachable: ConnectionError"


@pytest.mark.parametrize("state", ["auth_failed", "tokens_unset", "http_503"])
def test_status_degraded_for_any_non_connected_state(monkeypatch, state):
    assert _health(monkeypatch, "influx.test", (state, 1.0, "2.7"))["status"] == "degraded"


def test_status_ok_when_connected(monkeypatch):
    assert _health(monkeypatch, "influx.test", ("connected", 1.2, "2.7"))["status"] == "ok"


def test_status_ok_when_influxdb_not_configured(monkeypatch):
    body = _health(monkeypatch, "", ("unused", None, None))
    assert body["status"] == "ok"
    assert body["components"]["influxdb"] == "not configured"


def _points(n):
    now = int(time.time() * 1e9)
    return [{"measurement": "metrics", "tags": {"hostname": "h"},
             "fields": {"value": float(i)}, "time": now + i} for i in range(n)]


def _wait_counter(expected, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline and INFLUX_WRITES.total() < expected:
        time.sleep(0.1)
    return INFLUX_WRITES.total()


def test_failed_batch_writes_are_not_counted(monkeypatch):
    real = ic.WriteOptions
    # No retries so the dead-port batch fails at the first flush.
    monkeypatch.setattr(ic, "WriteOptions",
                        lambda **kw: real(**{**kw, "max_retries": 0, "flush_interval": 200}))
    client = InfluxDBClient(url="http://127.0.0.1:1", metrics_token="t", rollup_enabled=False)
    try:
        client.write_metrics_batch(_points(10))
        client.write_metric(_points(1)[0])
        assert INFLUX_WRITES.total() == 0
        time.sleep(1.5)
        assert INFLUX_WRITES.total() == 0
        with pytest.raises(Exception):
            client.write_metric_sync(_points(1)[0])
        assert INFLUX_WRITES.total() == 0
    finally:
        client.close()


class _Accept(BaseHTTPRequestHandler):
    lines: list[int] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Accept.lines.append(len(body.decode().splitlines()))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_accepted_batch_writes_are_counted_per_point():
    srv = HTTPServer(("127.0.0.1", 0), _Accept)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = InfluxDBClient(url=f"http://127.0.0.1:{srv.server_port}", metrics_token="t",
                            rollup_enabled=False)
    try:
        client.write_metrics_batch(_points(7))
        client.write_metric(_points(1)[0])
        assert _wait_counter(8) == 8
        assert sum(_Accept.lines) == 8
        client.write_metric_sync(_points(1)[0])
        assert INFLUX_WRITES.total() == 9
    finally:
        client.close()
        srv.shutdown()
