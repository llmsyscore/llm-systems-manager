"""#215: incident assignment — group join, window join, self-root."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from backend._time import now_utc
from backend.engine.alert_manager import AlertManager
from backend.models.alert import AlertCreate, AlertStatus


class FakeAlertRepo:
    def __init__(self, active=None):
        self._active = list(active or [])
        self.created = []

    def get_active(self):
        return list(self._active)

    def create(self, ac):
        alert = ac.to_alert()
        self.created.append(alert)
        return alert

    def refresh(self, alert, current_value):
        return alert


class FakeRuleRepo:
    def __init__(self, groups=None):
        self._groups = groups or {}

    def get_all(self, enabled_only=True):
        return [SimpleNamespace(rule_id=rid, correlation_group=g)
                for rid, g in self._groups.items()]


def _ongoing(host="hostA", incident="inc-1", age_s=10, rule_id=None):
    ts = now_utc() - timedelta(seconds=age_s)
    return SimpleNamespace(
        rule_id=rule_id or uuid4(), source_host=host, incident_id=incident,
        status=AlertStatus.ACTIVE, is_ongoing=True,
        created_at=ts, last_evaluated_at=ts)


def _ac(host="hostA", rule_id=None):
    return AlertCreate(rule_id=rule_id or uuid4(), metric_source="gpu",
                       metric_name="temp", current_value=90.0,
                       threshold_value=85.0, message="hot", source_host=host)


def _mgr(active, groups=None):
    return AlertManager(FakeAlertRepo(active), FakeRuleRepo(groups))


def test_window_join_same_host():
    m = _mgr([_ongoing(age_s=10)])
    alert = m.process_alert(_ac())
    assert alert.incident_id == "inc-1"


def test_no_join_outside_window():
    m = _mgr([_ongoing(age_s=3600)])
    alert = m.process_alert(_ac())
    assert alert.incident_id == str(alert.alert_id)


def test_no_join_across_hosts():
    m = _mgr([_ongoing(host="hostB", age_s=5)])
    alert = m.process_alert(_ac(host="hostA"))
    assert alert.incident_id == str(alert.alert_id)


def test_correlation_group_joins_outside_window():
    r_old, r_new = uuid4(), uuid4()
    m = _mgr([_ongoing(age_s=3600, rule_id=r_old)],
             groups={r_old: "thermal", r_new: "thermal"})
    alert = m.process_alert(_ac(rule_id=r_new))
    assert alert.incident_id == "inc-1"


def test_disabled_self_roots(monkeypatch):
    import backend.engine.alert_manager as am
    monkeypatch.setattr(
        am, "settings",
        SimpleNamespace(alarm_engine=SimpleNamespace(
            correlation=SimpleNamespace(enabled=False, window_seconds=60.0))))
    m = _mgr([_ongoing(age_s=5)])
    alert = m.process_alert(_ac())
    assert alert.incident_id == str(alert.alert_id)


def test_same_rule_still_refreshes_not_creates():
    rid = uuid4()
    existing = _ongoing(rule_id=rid)
    m = _mgr([existing])
    assert m.process_alert(_ac(rule_id=rid)) is None  # dedup path unchanged
