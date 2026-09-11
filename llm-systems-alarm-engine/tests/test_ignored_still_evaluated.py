"""#938: ignoring silences and hides an alert; it keeps being evaluated."""
from __future__ import annotations

import uuid

import pytest

from backend.models.alert import (
    ONGOING_STATUSES, AlertCreate, AlertStatus,
)
from backend.engine.alert_manager import AlertManager
from backend.storage.ae_alarms_db import AeAlarmsDB
from backend.storage.cache import MetricCache
from backend.storage.repositories import AlertRepository, RuleRepository


@pytest.fixture
def mgr_repo(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "ig.db")
    repo = AlertRepository(alarms_db=db)
    mgr = AlertManager(alert_repository=repo, rule_repository=RuleRepository(MetricCache()))
    try:
        yield mgr, repo
    finally:
        db.close()


def _ac(rule_id, value=91.0):
    return AlertCreate(
        rule_id=rule_id, rule_name="CPU warn", metric_source="system",
        metric_name="cpu_temperature_c", current_value=value, threshold_value=82.0,
        severity="warning", message="hot", source_host="agent-host",
    )


def test_ignored_is_an_ongoing_status():
    assert AlertStatus.IGNORED in ONGOING_STATUSES


def test_ignored_alert_stays_in_the_evaluated_set(mgr_repo):
    mgr, repo = mgr_repo
    a = mgr.process_alert(_ac(uuid.uuid4()))
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    ongoing = repo.get_active()
    assert [str(x.alert_id) for x in ongoing] == [str(a.alert_id)]
    assert ongoing[0].status == AlertStatus.IGNORED


def test_ignored_alert_can_still_be_closed_automatically(mgr_repo):
    mgr, repo = mgr_repo
    a = mgr.process_alert(_ac(uuid.uuid4()))
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    closed = mgr.close_alert(str(a.alert_id), reason="auto", resolved_value=70.0)
    assert closed is not None and closed.status == AlertStatus.CLOSED


def test_auto_close_does_not_lift_the_operator_window(mgr_repo):
    mgr, repo = mgr_repo
    rid = uuid.uuid4()
    a = mgr.process_alert(_ac(rid))
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    mgr.close_alert(str(a.alert_id), reason="auto", resolved_value=70.0)
    assert repo.is_rule_ignored(str(rid)) is True, "auto-resolve must not un-silence the rule"


def test_manual_close_does_lift_the_operator_window(mgr_repo):
    mgr, repo = mgr_repo
    rid = uuid.uuid4()
    a = mgr.process_alert(_ac(rid))
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    mgr.close_alert(str(a.alert_id), reason="manual")
    assert repo.is_rule_ignored(str(rid)) is False


def test_breach_inside_an_open_window_is_recorded_but_born_ignored(mgr_repo):
    mgr, repo = mgr_repo
    rid = uuid.uuid4()
    a = mgr.process_alert(_ac(rid))
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    mgr.close_alert(str(a.alert_id), reason="auto", resolved_value=70.0)

    again = mgr.process_alert(_ac(rid, value=99.0))
    assert again is not None, "the condition must still be tracked, not dropped"
    assert again.status == AlertStatus.IGNORED, "and stay silent + hidden"
    assert again.ignored_until is not None


def test_a_born_ignored_alert_sends_no_notification(mgr_repo):
    """The dispatcher gates on status, so a born-ignored alert is silent."""
    from backend.engine.notification_dispatcher import _alert_status, _HANDLED_STATUSES
    mgr, repo = mgr_repo
    rid = uuid.uuid4()
    a = mgr.process_alert(_ac(rid))
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    mgr.close_alert(str(a.alert_id), reason="auto", resolved_value=70.0)
    again = mgr.process_alert(_ac(rid, value=99.0))
    assert _alert_status(again) in _HANDLED_STATUSES
