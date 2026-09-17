"""#986: a closed alert cannot be acknowledged or ignored again.

Both transitions used to re-open the closed row (status flipped, ack time
stamped, history row un-archived) and the next auto-close left
acknowledged_at later than closed_at.
"""
from __future__ import annotations

import uuid

import pytest

from backend.models.alert import AlertCreate, AlertStatus
from backend.engine.alert_manager import AlertManager, AlertStateError
from backend.storage.ae_alarms_db import AeAlarmsDB
from backend.storage.cache import MetricCache
from backend.storage.repositories import AlertRepository, RuleRepository


@pytest.fixture
def mgr_repo(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "ack_closed.db")
    repo = AlertRepository(alarms_db=db)
    mgr = AlertManager(alert_repository=repo, rule_repository=RuleRepository(MetricCache()))
    try:
        yield mgr, repo
    finally:
        db.close()


def _closed_alert(mgr, repo):
    alert = repo.create(AlertCreate(
        rule_id=uuid.uuid4(), rule_name="CPU temperature warning",
        metric_source="system", metric_name="cpu_temperature_c",
        current_value=91.2, threshold_value=82.0, severity="warning",
        message="hot", source_host="agent-host",
    ))
    closed = mgr.close_alert(str(alert.alert_id), reason="auto", resolved_value=70.0)
    assert closed is not None and closed.status == AlertStatus.CLOSED
    return closed


def test_acknowledge_refuses_a_closed_alert(mgr_repo):
    mgr, repo = mgr_repo
    closed = _closed_alert(mgr, repo)
    with pytest.raises(AlertStateError):
        mgr.acknowledge_alert(str(closed.alert_id))
    again = repo.get_by_id(closed.alert_id)
    assert again.status == AlertStatus.CLOSED
    assert again.acknowledged_at is None
    assert again.closed_at == closed.closed_at


def test_ignore_refuses_a_closed_alert(mgr_repo):
    mgr, repo = mgr_repo
    closed = _closed_alert(mgr, repo)
    with pytest.raises(AlertStateError):
        mgr.ignore_alert(str(closed.alert_id), 2)
    again = repo.get_by_id(closed.alert_id)
    assert again.status == AlertStatus.CLOSED
    assert again.ignored_until is None


def test_closed_alert_stays_archived_after_refused_ack(mgr_repo):
    mgr, repo = mgr_repo
    closed = _closed_alert(mgr, repo)
    with pytest.raises(AlertStateError):
        mgr.acknowledge_alert(str(closed.alert_id))
    assert all(a.alert_id != closed.alert_id for a in repo.get_active())


def test_acknowledging_an_open_alert_still_works(mgr_repo):
    mgr, repo = mgr_repo
    alert = repo.create(AlertCreate(
        rule_id=uuid.uuid4(), rule_name="r", metric_source="system",
        metric_name="m", current_value=1.0, threshold_value=0.5,
        severity="warning", message="x", source_host="h",
    ))
    out = mgr.acknowledge_alert(str(alert.alert_id))
    assert out.status == AlertStatus.ACKNOWLEDGED
    assert out.acknowledged_at is not None
