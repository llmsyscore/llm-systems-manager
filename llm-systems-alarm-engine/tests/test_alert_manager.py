"""
Unit tests for backend.engine.alert_manager.AlertManager.

AlertManager is a thin lifecycle coordinator over AlertRepository. We don't
exercise SQLite here — repository persistence has its own test surface and
mocking the repo lets us prove the manager's branching (deduplication,
status transitions, invalid-UUID handling) without spinning up a DB.
"""
from __future__ import annotations

from datetime import datetime
from backend._time import now_utc
from typing import Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.alert_manager import AlertManager
from backend.models.alert import Alert, AlertCreate, AlertStatus, AlertUpdate


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeAlertRepository:
    """In-memory stand-in for AlertRepository.

    Tracks call counts so tests can assert on dispatch shape (e.g. "process
    refreshed instead of creating") without inspecting log lines.
    """

    def __init__(self):
        self._alerts: dict[UUID, Alert] = {}
        self.create_calls = 0
        self.refresh_calls = 0
        self.update_calls = 0
        self.delete_calls = 0
        # Optional: AlertManager.get_alert_stats touches alarms_db when present
        self.alarms_db = None

    def create(self, alert_create: AlertCreate) -> Alert:
        self.create_calls += 1
        alert = alert_create.to_alert()
        self._alerts[alert.alert_id] = alert
        return alert

    def get_active(self) -> list[Alert]:
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
        self.update_calls += 1
        alert = self._alerts.get(alert_id)
        if alert is None:
            return None
        data = alert.model_dump()
        for k, v in update.model_dump(exclude_unset=True).items():
            data[k] = v
        # Stamp transition timestamps the way the real repo does.
        if update.status == AlertStatus.ACKNOWLEDGED:
            data["acknowledged_at"] = now_utc()
        if update.status == AlertStatus.CLOSED:
            data["closed_at"] = now_utc()
        updated = Alert(**data)
        self._alerts[alert_id] = updated
        return updated

    def refresh(self, alert: Alert, current_value: float,
                evaluated_at: Optional[datetime] = None) -> Alert:
        self.refresh_calls += 1
        data = alert.model_dump()
        data["current_value"] = current_value
        data["trigger_count"] = alert.trigger_count + 1
        data["last_evaluated_at"] = evaluated_at or now_utc()
        refreshed = Alert(**data)
        self._alerts[alert.alert_id] = refreshed
        return refreshed

    def delete(self, alert_id: UUID) -> bool:
        self.delete_calls += 1
        return self._alerts.pop(alert_id, None) is not None


class FakeRuleRepository:
    """AlertManager scans this for correlation_group lookups (#215)."""

    def get_all(self, enabled_only: bool = True) -> list:
        return []


@pytest.fixture
def alert_repo():
    return FakeAlertRepository()


@pytest.fixture
def manager(alert_repo):
    return AlertManager(alert_repository=alert_repo, rule_repository=FakeRuleRepository())


def _make_alert_create(rule_id: Optional[UUID] = None, value: float = 95.0) -> AlertCreate:
    return AlertCreate(
        rule_id=rule_id or uuid4(),
        rule_name="cpu-high",
        metric_source="cpu",
        metric_name="usage_percent",
        current_value=value,
        threshold_value=80.0,
        severity="warning",
        message="Test alert",
        source_host="host-1",
    )


# ── process_alert: create + dedupe ───────────────────────────────────────────

class TestProcessAlert:
    def test_new_alert_creates(self, manager, alert_repo):
        ac = _make_alert_create()
        alert = manager.process_alert(ac)
        assert alert is not None
        assert alert.status == AlertStatus.ACTIVE
        assert alert.trigger_count == 1
        assert alert_repo.create_calls == 1
        assert alert_repo.refresh_calls == 0

    def test_second_alert_same_rule_deduplicates(self, manager, alert_repo):
        rule_id = uuid4()
        first = manager.process_alert(_make_alert_create(rule_id, value=95.0))
        assert first is not None

        # Second fire for same rule → return None (dedupe) and refresh
        second = manager.process_alert(_make_alert_create(rule_id, value=97.5))
        assert second is None
        assert alert_repo.create_calls == 1
        assert alert_repo.refresh_calls == 1

        # The stored alert's current_value should be the latest fire
        active = alert_repo.get_active()
        assert len(active) == 1
        assert active[0].current_value == 97.5
        assert active[0].trigger_count == 2

    def test_different_rule_creates_new(self, manager, alert_repo):
        manager.process_alert(_make_alert_create(rule_id=uuid4()))
        manager.process_alert(_make_alert_create(rule_id=uuid4()))
        assert alert_repo.create_calls == 2
        assert len(alert_repo.get_active()) == 2

    def test_closed_alert_does_not_dedupe(self, manager, alert_repo):
        rule_id = uuid4()
        first = manager.process_alert(_make_alert_create(rule_id))
        # Close the alert manually
        manager.close_alert(str(first.alert_id), reason="manual", resolved_value=70.0)
        assert first.alert_id not in [a.alert_id for a in alert_repo.get_active()]

        # New fire for the same rule should CREATE, not dedupe
        second = manager.process_alert(_make_alert_create(rule_id))
        assert second is not None
        assert alert_repo.create_calls == 2

    def test_acknowledged_alert_still_dedupes(self, manager, alert_repo):
        # Ack means "I've seen it, but the condition is still firing" — new
        # fires should refresh, not create a duplicate.
        rule_id = uuid4()
        first = manager.process_alert(_make_alert_create(rule_id))
        manager.acknowledge_alert(str(first.alert_id))

        second = manager.process_alert(_make_alert_create(rule_id, value=99.0))
        assert second is None
        assert alert_repo.create_calls == 1
        assert alert_repo.refresh_calls == 1


# ── acknowledge / close / delete / ignore ────────────────────────────────────

class TestAcknowledge:
    def test_marks_acknowledged(self, manager):
        first = manager.process_alert(_make_alert_create())
        ack = manager.acknowledge_alert(str(first.alert_id))
        assert ack is not None
        assert ack.status == AlertStatus.ACKNOWLEDGED
        assert ack.acknowledged_at is not None

    def test_invalid_uuid_returns_none(self, manager):
        assert manager.acknowledge_alert("not-a-uuid") is None

    def test_missing_alert_returns_none(self, manager):
        assert manager.acknowledge_alert(str(uuid4())) is None


class TestClose:
    def test_closes_with_reason_and_value(self, manager):
        first = manager.process_alert(_make_alert_create())
        closed = manager.close_alert(str(first.alert_id), reason="auto", resolved_value=42.0)
        assert closed is not None
        assert closed.status == AlertStatus.CLOSED
        assert closed.closed_at is not None
        assert closed.resolution_reason == "auto"
        assert closed.resolved_value == 42.0

    def test_close_invalid_uuid_returns_none(self, manager):
        assert manager.close_alert("garbage") is None

    def test_close_missing_returns_none(self, manager):
        assert manager.close_alert(str(uuid4())) is None


class TestDelete:
    def test_delete_returns_true(self, manager, alert_repo):
        first = manager.process_alert(_make_alert_create())
        assert manager.delete_alert(str(first.alert_id)) is True
        assert alert_repo.delete_calls == 1
        assert alert_repo.get_by_id(first.alert_id) is None

    def test_delete_invalid_uuid_returns_false(self, manager):
        assert manager.delete_alert("not-a-uuid") is False

    def test_delete_missing_returns_false(self, manager):
        assert manager.delete_alert(str(uuid4())) is False


class TestIgnore:
    def test_marks_ignored(self, manager, alert_repo):
        first = manager.process_alert(_make_alert_create())
        ignored = manager.ignore_alert(str(first.alert_id))
        assert ignored is not None
        assert ignored.status == AlertStatus.IGNORED
        # Ignored should also drop out of active list
        assert ignored.alert_id not in [a.alert_id for a in alert_repo.get_active()]

    def test_invalid_uuid_returns_none(self, manager):
        assert manager.ignore_alert("xxx") is None


# ── Lifecycle (fire → ack → resolve) ─────────────────────────────────────────

class TestLifecycle:
    def test_fire_then_ack_then_close(self, manager, alert_repo):
        first = manager.process_alert(_make_alert_create())
        assert first.status == AlertStatus.ACTIVE
        ack = manager.acknowledge_alert(str(first.alert_id))
        assert ack.status == AlertStatus.ACKNOWLEDGED

        closed = manager.close_alert(str(first.alert_id), reason="auto", resolved_value=10.0)
        assert closed.status == AlertStatus.CLOSED
        assert alert_repo.get_active() == []

    def test_mark_as_read_aliases_acknowledge(self, manager):
        first = manager.process_alert(_make_alert_create())
        result = manager.mark_as_read(str(first.alert_id))
        assert result is not None
        assert result.status == AlertStatus.ACKNOWLEDGED


# ── Helpers ──────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_get_active_alerts_returns_active_only(self, manager):
        a1 = manager.process_alert(_make_alert_create(rule_id=uuid4()))
        a2 = manager.process_alert(_make_alert_create(rule_id=uuid4()))
        manager.close_alert(str(a1.alert_id))
        active = manager.get_active_alerts()
        assert len(active) == 1
        assert active[0].alert_id == a2.alert_id

    def test_get_alert_stats_returns_zero_dict_without_alarms_db(self, manager):
        # FakeAlertRepository.alarms_db is None — manager returns a zero shape
        stats = manager.get_alert_stats()
        assert stats == {
            "total": 0, "active": 0, "acknowledged": 0, "closed": 0,
            "ignored": 0, "by_severity": {},
        }

    def test_get_alert_stats_uses_alarms_db_when_present(self, manager, alert_repo):
        class FakeDB:
            def count_by_status_and_severity(self):
                return {
                    "total": 7,
                    "by_status": {"active": 3, "acknowledged": 1,
                                  "closed": 2, "ignored": 1},
                    "by_severity": {"critical": 2, "warning": 5},
                }
        alert_repo.alarms_db = FakeDB()
        stats = manager.get_alert_stats()
        assert stats["total"] == 7
        assert stats["active"] == 3
        assert stats["by_severity"] == {"critical": 2, "warning": 5}
