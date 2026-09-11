"""#939: the timeline is an append-only record, so repeated transitions all survive."""
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
    db = AeAlarmsDB.open(tmp_path / "ev.db")
    repo = AlertRepository(alarms_db=db)
    mgr = AlertManager(alert_repository=repo, rule_repository=RuleRepository(MetricCache()))
    try:
        yield mgr, repo
    finally:
        db.close()


def _mk(mgr):
    return mgr.process_alert(AlertCreate(
        rule_id=uuid.uuid4(), rule_name="CPU warn", metric_source="system",
        metric_name="cpu_temperature_c", current_value=91.0, threshold_value=82.0,
        severity="warning", message="hot", source_host="agent-host"))


def _events(repo, a):
    return [e["event"] for e in repo.get_events(a.alert_id)]


def test_creation_is_recorded(mgr_repo):
    mgr, repo = mgr_repo
    a = _mk(mgr)
    assert _events(repo, a) == ["created"]


def test_full_lifecycle_is_recorded_in_order(mgr_repo):
    mgr, repo = mgr_repo
    a = _mk(mgr)
    aid = str(a.alert_id)
    mgr.acknowledge_alert(aid)
    mgr.resume_alert(aid)
    mgr.ignore_alert(aid, duration_hours=2)
    mgr.resume_alert(aid)
    mgr.close_alert(aid, reason="auto", resolved_value=70.0)
    assert _events(repo, a) == [
        "created", "acknowledged", "unacknowledged",
        "ignored", "ignore_ended", "closed",
    ]


def test_repeated_acknowledge_cycles_all_survive(mgr_repo):
    """The old timestamp-derived timeline could only show one of these."""
    mgr, repo = mgr_repo
    a = _mk(mgr)
    aid = str(a.alert_id)
    for _ in range(3):
        mgr.acknowledge_alert(aid)
        mgr.resume_alert(aid)
    assert _events(repo, a) == ["created"] + ["acknowledged", "unacknowledged"] * 3


def test_resume_is_labelled_by_what_it_ended(mgr_repo):
    mgr, repo = mgr_repo
    a = _mk(mgr)
    aid = str(a.alert_id)
    mgr.ignore_alert(aid, duration_hours=1)
    mgr.resume_alert(aid)
    assert _events(repo, a)[-1] == "ignore_ended"
    mgr.acknowledge_alert(aid)
    mgr.resume_alert(aid)
    assert _events(repo, a)[-1] == "unacknowledged"


def test_events_carry_timestamps_and_detail(mgr_repo):
    mgr, repo = mgr_repo
    a = _mk(mgr)
    mgr.close_alert(str(a.alert_id), reason="auto", resolved_value=70.0)
    evs = repo.get_events(a.alert_id)
    assert all(e["at"] for e in evs)
    assert "auto" in evs[-1]["detail"]


def test_events_are_scoped_per_alert(mgr_repo):
    mgr, repo = mgr_repo
    a, b = _mk(mgr), _mk(mgr)
    mgr.acknowledge_alert(str(a.alert_id))
    assert _events(repo, a) == ["created", "acknowledged"]
    assert _events(repo, b) == ["created"]
