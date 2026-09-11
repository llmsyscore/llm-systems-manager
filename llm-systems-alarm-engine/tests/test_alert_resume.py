"""#937: ending an ignore window resumes the alert, it does not close it.

Ignoring sets the alert status and opens a suppression window on its rule, so
resuming has to reverse both halves and clear the stored deadline.
"""
from __future__ import annotations

import uuid

import pytest

from backend.models.alert import AlertCreate, AlertStatus
from backend.engine.alert_manager import AlertManager
from backend.storage.ae_alarms_db import AeAlarmsDB
from backend.storage.cache import MetricCache
from backend.storage.repositories import AlertRepository, RuleRepository


@pytest.fixture
def mgr_repo(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "resume.db")
    repo = AlertRepository(alarms_db=db)
    mgr = AlertManager(alert_repository=repo, rule_repository=RuleRepository(MetricCache()))
    try:
        yield mgr, repo
    finally:
        db.close()


def _alert(repo, rule_id):
    return repo.create(AlertCreate(
        rule_id=rule_id, rule_name="CPU temperature warning",
        metric_source="system", metric_name="cpu_temperature_c",
        current_value=91.2, threshold_value=82.0, severity="warning",
        message="hot", source_host="agent-host",
    ))


def test_resume_returns_the_alert_to_active(mgr_repo):
    mgr, repo = mgr_repo
    rule_id = uuid.uuid4()
    a = _alert(repo, rule_id)
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    assert repo.get_by_id(a.alert_id).status == AlertStatus.IGNORED

    out = mgr.resume_alert(str(a.alert_id))
    assert out is not None
    assert out.status == AlertStatus.ACTIVE, "resume must not close the alert"
    assert out.status != AlertStatus.CLOSED


def test_resume_clears_the_stored_deadline(mgr_repo):
    mgr, repo = mgr_repo
    rule_id = uuid.uuid4()
    a = _alert(repo, rule_id)
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    assert repo.get_by_id(a.alert_id).ignored_until is not None

    mgr.resume_alert(str(a.alert_id))
    assert repo.get_by_id(a.alert_id).ignored_until is None


def test_resume_lifts_the_rule_suppression_window(mgr_repo):
    mgr, repo = mgr_repo
    rule_id = uuid.uuid4()
    a = _alert(repo, rule_id)
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    assert repo.is_rule_ignored(str(rule_id)) is True

    mgr.resume_alert(str(a.alert_id))
    assert repo.is_rule_ignored(str(rule_id)) is False


def test_resumed_alert_counts_as_ongoing_again(mgr_repo):
    mgr, repo = mgr_repo
    a = _alert(repo, uuid.uuid4())
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    mgr.resume_alert(str(a.alert_id))
    assert repo.get_by_id(a.alert_id).is_ongoing is True


def test_resume_on_an_unknown_alert_returns_none(mgr_repo):
    mgr, _ = mgr_repo
    assert mgr.resume_alert(str(uuid.uuid4())) is None
    assert mgr.resume_alert("not-a-uuid") is None


def test_ignoring_does_not_acknowledge(mgr_repo):
    mgr, repo = mgr_repo
    a = _alert(repo, uuid.uuid4())
    mgr.ignore_alert(str(a.alert_id), duration_hours=24)
    got = repo.get_by_id(a.alert_id)
    assert got.status == AlertStatus.IGNORED
    assert got.acknowledged_at is None, "ignore must not stamp an acknowledgement"


def test_resume_clears_the_acknowledgement(mgr_repo):
    mgr, repo = mgr_repo
    a = _alert(repo, uuid.uuid4())
    mgr.acknowledge_alert(str(a.alert_id))
    assert repo.get_by_id(a.alert_id).acknowledged_at is not None

    out = mgr.resume_alert(str(a.alert_id))
    assert out.status == AlertStatus.ACTIVE
    got = repo.get_by_id(a.alert_id)
    assert got.acknowledged_at is None and got.acknowledged_by is None


def test_acknowledge_then_unacknowledge_round_trips(mgr_repo):
    mgr, repo = mgr_repo
    a = _alert(repo, uuid.uuid4())
    for _ in range(2):
        mgr.acknowledge_alert(str(a.alert_id))
        assert repo.get_by_id(a.alert_id).status == AlertStatus.ACKNOWLEDGED
        mgr.resume_alert(str(a.alert_id))
        assert repo.get_by_id(a.alert_id).status == AlertStatus.ACTIVE
