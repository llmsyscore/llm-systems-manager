"""#1191 a refresh never overwrites a newer cached alert state; #1192 close-all
closes ignored alerts too; #1193 an OTLP client disconnect is handled quietly."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import ClientDisconnect

from backend._time import now_utc
from backend.models.alert import Alert, AlertStatus
from backend.receivers import otlp_receiver as otlp
from backend.storage.repositories import AlertRepository


def _alert(status=AlertStatus.ACTIVE, **over):
    base = dict(alert_id=uuid4(), rule_id=uuid4(), rule_name="r", metric_source="s",
                metric_name="m", current_value=1.0, threshold_value=0.5, severity="critical",
                status=status, message="msg", created_at=now_utc(), trigger_count=3)
    base.update(over)
    return Alert(**base)


class TestRefreshKeepsNewerState:
    def test_an_acknowledge_that_landed_mid_cycle_survives_the_refresh(self):
        repo = AlertRepository(alarms_db=None)
        snapshot = _alert()                                   # the engine's copy: still active
        cached = snapshot.model_copy(update={"status": AlertStatus.ACKNOWLEDGED,
                                             "acknowledged_by": "op"})
        repo._active_cache = [cached]                         # re-read after the ack
        repo.refresh(snapshot, 9.5)
        assert repo._active_cache[0] is cached                # same object, not the snapshot
        assert cached.status == AlertStatus.ACKNOWLEDGED and cached.acknowledged_by == "op"
        assert cached.current_value == 9.5 and cached.trigger_count == 4
        assert cached.last_evaluated_at == snapshot.last_evaluated_at

    def test_refresh_of_the_cached_object_itself_still_bumps_it(self):
        repo = AlertRepository(alarms_db=None)
        a = _alert()
        repo._active_cache = [a]
        repo.refresh(a, 2.0)
        assert a.current_value == 2.0 and a.trigger_count == 4

    def test_no_cache_is_fine(self):
        repo = AlertRepository(alarms_db=None)
        repo._active_cache = None
        assert repo.refresh(_alert(), 2.0).current_value == 2.0


class TestCloseAll:
    async def test_ignored_alerts_are_closed_too(self):
        seen = {}

        class _Db:
            def bulk_update_status(self, **kw):
                seen.update(kw)
                return 3

        repo = AlertRepository(alarms_db=_Db())
        assert await repo.close_all_alerts() == 3
        assert set(seen["from_statuses"]) == {"active", "acknowledged", "ignored"}
        assert seen["to_status"] == "closed" and seen["closed_at"]


class _GoneRequest:
    url = SimpleNamespace(path="/v1/metrics")

    async def body(self):
        raise ClientDisconnect()


class TestOtlpDisconnect:
    @pytest.mark.parametrize("route", [otlp.receive_metrics, otlp.receive_traces, otlp.receive_logs])
    async def test_a_disconnect_returns_400_without_an_error_log(self, route, caplog):
        with caplog.at_level(logging.DEBUG, logger=otlp.logger.name):
            resp = await route(_GoneRequest(), _auth=None)
        assert resp.status_code == 400
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("disconnected" in r.getMessage() for r in caplog.records)
