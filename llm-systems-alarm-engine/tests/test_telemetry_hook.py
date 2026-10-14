"""The AE loads the shared telemetry module by path and reports unhandled route errors."""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend import alarm_engine as AE


def test_loader_finds_manager_module():
    mod = AE._telemetry()
    assert mod is not None and hasattr(mod, "report_exception")


def test_unhandled_error_reports_crash(monkeypatch):
    mod = AE._telemetry()
    seen = []
    monkeypatch.setattr(mod, "report_exception",
                        lambda exc, tb, component: seen.append((type(exc).__name__, component)))

    @AE.app.get("/__telemetry_boom")
    async def _boom():
        raise KeyError("x")

    r = TestClient(AE.app, raise_server_exceptions=False).get("/__telemetry_boom")
    assert r.status_code == 500
    assert seen == [("KeyError", "alarm_engine")]
