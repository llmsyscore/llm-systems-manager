"""#1273: /health carries the engine's own CPU and memory, standard library only."""
from __future__ import annotations

import math

from fastapi.testclient import TestClient

from backend import alarm_engine as ae


def _client():
    return TestClient(ae.app, raise_server_exceptions=False)


def test_health_carries_a_process_block():
    body = _client().get("/health").json()
    proc = body["components"]["process"]
    assert set(proc) == {"cpu_pct", "rss_mb", "ram_pct"}
    assert proc["rss_mb"] is None or proc["rss_mb"] > 0
    assert proc["ram_pct"] is None or 0 <= proc["ram_pct"] <= 100


def test_second_read_gives_a_finite_cpu_share():
    c = _client()
    c.get("/health")
    proc = c.get("/health").json()["components"]["process"]
    assert proc["cpu_pct"] is not None
    assert math.isfinite(proc["cpu_pct"]) and proc["cpu_pct"] >= 0


def test_a_failing_reader_yields_nulls_not_a_missing_block(monkeypatch):
    def boom():
        raise OSError("no /proc")
    monkeypatch.setattr(ae, "_process_rss_bytes", boom)
    monkeypatch.setattr(ae, "_host_ram_bytes", boom)
    proc = _client().get("/health").json()["components"]["process"]
    assert proc["rss_mb"] is None and proc["ram_pct"] is None
    assert "cpu_pct" in proc
