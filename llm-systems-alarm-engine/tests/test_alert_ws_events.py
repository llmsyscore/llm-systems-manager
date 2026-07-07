"""#221: AlertManager broadcasts alert_* lifecycle WS events."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from backend._time import now_utc
from backend.engine.alert_manager import AlertManager
from backend.models.alert import Alert, AlertCreate, AlertStatus, AlertUpdate


class FakeAlertRepo:
    """Same shape as tests/test_alert_manager.py's fake, minus call counters."""

    def __init__(self, active=None):
        self._alerts: dict[UUID, Alert] = {}
        for a in (active or []):
            self._alerts[a.alert_id] = a

    def create(self, alert_create: AlertCreate) -> Alert:
        alert = alert_create.to_alert()
        self._alerts[alert.alert_id] = alert
        return alert

    def get_active(self):
        return [a for a in self._alerts.values()
                if a.status in (AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED)]

    def is_rule_ignored(self, rule_id) -> bool:
        return False

    def set_rule_ignored(self, rule_id, until) -> None:
        pass

    def clear_rule_ignored(self, rule_id) -> None:
        pass

    def get_by_id(self, alert_id: UUID) -> Optional[Alert]:
        return self._alerts.get(alert_id)

    def update(self, alert_id: UUID, update: AlertUpdate) -> Optional[Alert]:
        alert = self._alerts.get(alert_id)
        if alert is None:
            return None
        data = alert.model_dump()
        for k, v in update.model_dump(exclude_unset=True).items():
            data[k] = v
        if update.status == AlertStatus.ACKNOWLEDGED:
            data["acknowledged_at"] = now_utc()
        if update.status == AlertStatus.CLOSED:
            data["closed_at"] = now_utc()
        updated = Alert(**data)
        self._alerts[alert_id] = updated
        return updated

    def refresh(self, alert: Alert, current_value: float,
                evaluated_at: Optional[datetime] = None) -> Alert:
        data = alert.model_dump()
        data["current_value"] = current_value
        data["trigger_count"] = alert.trigger_count + 1
        data["last_evaluated_at"] = evaluated_at or now_utc()
        refreshed = Alert(**data)
        self._alerts[alert.alert_id] = refreshed
        return refreshed


class FakeRuleRepo:
    def get_all(self, enabled_only=True):
        return []


class RecordingBroadcast:
    """Async callable that records every (event, payload) it's awaited with."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, event, payload):
        self.calls.append((event, payload))


def _ac(rule_id=None, host="hostA"):
    return AlertCreate(rule_id=rule_id or uuid4(), metric_source="gpu",
                       metric_name="temp", current_value=90.0,
                       threshold_value=85.0, message="hot", source_host=host)


def _mgr(active=None, ws_broadcast=None):
    m = AlertManager(FakeAlertRepo(active), FakeRuleRepo())
    m.ws_broadcast = ws_broadcast
    return m


async def _settle():
    """Give a fire-and-forget create_task a turn to run before asserting."""
    await asyncio.sleep(0)


def test_alert_created_emits_ws_event():
    bc = RecordingBroadcast()

    async def body():
        m = _mgr(ws_broadcast=bc)
        alert = m.process_alert(_ac())
        await _settle()
        return alert

    alert = asyncio.run(body())
    assert len(bc.calls) == 1
    event, payload = bc.calls[0]
    assert event == "alert_created"
    assert payload["incident_id"] == alert.incident_id
    assert payload["incident_size"] == 1


def test_dedup_refresh_emits_nothing():
    bc = RecordingBroadcast()
    rid = uuid4()

    async def body():
        m = _mgr(ws_broadcast=bc)
        m.process_alert(_ac(rule_id=rid))
        await _settle()
        bc.calls.clear()
        # Second alert on the same rule dedups (refresh path), not create.
        result = m.process_alert(_ac(rule_id=rid))
        await _settle()
        return result

    result = asyncio.run(body())
    assert result is None
    assert bc.calls == []


def test_acknowledge_emits_ws_event():
    bc = RecordingBroadcast()

    async def body():
        m = _mgr(ws_broadcast=bc)
        alert = m.process_alert(_ac())
        await _settle()
        bc.calls.clear()
        result = m.acknowledge_alert(str(alert.alert_id))
        await _settle()
        return result

    result = asyncio.run(body())
    assert result is not None
    assert len(bc.calls) == 1
    event, payload = bc.calls[0]
    assert event == "alert_acknowledged"
    assert payload["status"] == "acknowledged"
    assert "incident_size" in payload


def test_close_emits_ws_event():
    bc = RecordingBroadcast()

    async def body():
        m = _mgr(ws_broadcast=bc)
        alert = m.process_alert(_ac())
        await _settle()
        bc.calls.clear()
        result = m.close_alert(str(alert.alert_id), reason="manual")
        await _settle()
        return result

    result = asyncio.run(body())
    assert result is not None
    assert len(bc.calls) == 1
    event, payload = bc.calls[0]
    assert event == "alert_closed"
    assert payload["status"] == "closed"


def test_ignore_emits_ws_event():
    bc = RecordingBroadcast()

    async def body():
        m = _mgr(ws_broadcast=bc)
        alert = m.process_alert(_ac())
        await _settle()
        bc.calls.clear()
        result = m.ignore_alert(str(alert.alert_id))
        await _settle()
        return result

    result = asyncio.run(body())
    assert result is not None
    assert len(bc.calls) == 1
    event, payload = bc.calls[0]
    assert event == "alert_ignored"
    assert payload["status"] == "ignored"


def test_ws_broadcast_none_no_error():
    async def body():
        m = _mgr(ws_broadcast=None)
        alert = m.process_alert(_ac())
        await _settle()
        m.acknowledge_alert(str(alert.alert_id))
        await _settle()
        return alert

    alert = asyncio.run(body())
    assert alert is not None


def test_no_running_loop_does_not_crash():
    # Plain sync call, no event loop running: get_running_loop() raises
    # RuntimeError, which _emit_ws_event must swallow.
    bc = RecordingBroadcast()
    m = _mgr(ws_broadcast=bc)
    alert = m.process_alert(_ac())
    assert alert is not None
    assert bc.calls == []
