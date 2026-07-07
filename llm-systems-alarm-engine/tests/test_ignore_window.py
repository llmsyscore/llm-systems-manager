"""#247: "Ignore for N hours" must actually suppress re-firing until the
window expires. Exercises the real AlertRepository + AeAlarmsDB persistence
path (ignored_until column) and the process_alert suppression choke point.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from backend._time import now_utc
from backend.models.alert import AlertCreate, AlertStatus
from backend.storage.ae_alarms_db import AeAlarmsDB
from backend.storage.repositories import AlertRepository
from backend.engine.alert_manager import AlertManager


class _FakeRuleRepo:
    def get_all(self, enabled_only: bool = False):
        return []


def _ac(rule_id, value: float = 95.0) -> AlertCreate:
    return AlertCreate(
        rule_id=rule_id, rule_name="cpu-high", metric_source="cpu",
        metric_name="usage_percent", current_value=value, threshold_value=80.0,
        severity="warning", message="x", source_host="h1",
    )


@pytest.fixture
def db(tmp_path):
    d = AeAlarmsDB.open(tmp_path / "ae_alarms.db")
    yield d
    d.close()


@pytest.fixture
def mgr(db):
    repo = AlertRepository(alarms_db=db)
    return AlertManager(alert_repository=repo, rule_repository=_FakeRuleRepo())


def test_ignore_persists_until_and_suppresses_recreate(mgr):
    rid = uuid4()
    first = mgr.process_alert(_ac(rid))
    assert first is not None
    ignored = mgr.ignore_alert(str(first.alert_id), duration_hours=24)
    assert ignored is not None
    assert ignored.status == AlertStatus.IGNORED
    assert ignored.ignored_until is not None
    assert ignored.ignored_until > now_utc()
    # Next eval tick: rule still breaching, no live alert — must NOT re-create.
    again = mgr.process_alert(_ac(rid, value=97.0))
    assert again is None
    assert mgr.alert_repository.get_active() == []


def test_expired_window_allows_recreate(mgr):
    rid = uuid4()
    first = mgr.process_alert(_ac(rid))
    mgr.ignore_alert(str(first.alert_id), duration_hours=1)
    # Force the window into the past, as if the hours had elapsed.
    mgr.alert_repository.set_rule_ignored(str(rid), now_utc() - timedelta(seconds=1))
    again = mgr.process_alert(_ac(rid))
    assert again is not None
    assert again.status == AlertStatus.ACTIVE


def test_window_survives_repository_restart(db):
    rid = uuid4()
    repo1 = AlertRepository(alarms_db=db)
    mgr1 = AlertManager(alert_repository=repo1, rule_repository=_FakeRuleRepo())
    first = mgr1.process_alert(_ac(rid))
    mgr1.ignore_alert(str(first.alert_id), duration_hours=24)
    # Fresh repository over the same DB simulates an AE process restart:
    # the ignore window must rehydrate from the persisted ignored_until.
    repo2 = AlertRepository(alarms_db=db)
    mgr2 = AlertManager(alert_repository=repo2, rule_repository=_FakeRuleRepo())
    assert mgr2.process_alert(_ac(rid)) is None


def test_ignore_all_sets_windows(db):
    r1, r2 = uuid4(), uuid4()
    repo = AlertRepository(alarms_db=db)
    mgr = AlertManager(alert_repository=repo, rule_repository=_FakeRuleRepo())
    mgr.process_alert(_ac(r1))
    mgr.process_alert(_ac(r2))
    n = asyncio.run(repo.ignore_all_alerts(24))
    assert n == 2
    assert mgr.process_alert(_ac(r1)) is None
    assert mgr.process_alert(_ac(r2)) is None


def test_manual_alert_without_rule_is_not_suppressed(mgr):
    # rule_id=None alerts can't key an ignore window; they must still create.
    a = mgr.process_alert(_ac(None))
    assert a is not None


def test_acknowledging_ignored_alert_lifts_window(mgr):
    rid = uuid4()
    first = mgr.process_alert(_ac(rid))
    mgr.ignore_alert(str(first.alert_id), duration_hours=24)
    assert mgr.alert_repository.is_rule_ignored(str(rid)) is True
    mgr.acknowledge_alert(str(first.alert_id))
    # Handling the alert lifts the suppression window on its rule.
    assert mgr.alert_repository.is_rule_ignored(str(rid)) is False


def test_deleting_ignored_alert_lifts_window(mgr):
    rid = uuid4()
    first = mgr.process_alert(_ac(rid))
    mgr.ignore_alert(str(first.alert_id), duration_hours=24)
    mgr.delete_alert(str(first.alert_id))
    assert mgr.process_alert(_ac(rid)) is not None


def test_lifted_window_does_not_rehydrate_on_restart(db):
    rid = uuid4()
    repo1 = AlertRepository(alarms_db=db)
    mgr1 = AlertManager(alert_repository=repo1, rule_repository=_FakeRuleRepo())
    first = mgr1.process_alert(_ac(rid))
    mgr1.ignore_alert(str(first.alert_id), duration_hours=24)
    mgr1.acknowledge_alert(str(first.alert_id))
    # Restart: the persisted ignored_until was cleared, so nothing rehydrates.
    repo2 = AlertRepository(alarms_db=db)
    assert repo2.is_rule_ignored(str(rid)) is False


def test_purge_keeps_rows_with_open_ignore_window(db):
    from backend._time import now_utc
    rid = uuid4()
    repo = AlertRepository(alarms_db=db)
    mgr = AlertManager(alert_repository=repo, rule_repository=_FakeRuleRepo())
    first = mgr.process_alert(_ac(rid))
    mgr.ignore_alert(str(first.alert_id), duration_hours=24)  # archived to history
    # Retention sweep with a cutoff in the far future would normally purge it,
    # but the open ignore window must keep the row.
    db.purge_history_older_than((now_utc() + timedelta(days=3650)).isoformat())
    assert db.get_alert(str(first.alert_id)) is not None
