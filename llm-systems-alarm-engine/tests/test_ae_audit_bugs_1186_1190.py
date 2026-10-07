"""Audit batch (#1186, #1187, #1189, #1190): quiet hours keep ongoing alerts, a removed or
disabled rule closes its alerts, port 465 email uses TLS, non-finite values never alert."""
from __future__ import annotations

from email.mime.text import MIMEText
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fastapi.testclient import TestClient

from backend import alarm_engine as ae
from backend._time import now_utc
from backend.api.routes import rules as rules_routes
from backend.engine import notification_dispatcher as nd
from backend.engine.alert_manager import AlertManager
from backend.engine.rule_engine import RuleEngine
from backend.engine.threshold_evaluator import ThresholdEvaluator
from backend.models.alarm_rule import AlarmRuleUpdate, RuleType, ThresholdConfig
from backend.models.alert import AlertStatus
from backend.models.metrics import MetricPoint
from tests.test_ae_audit_batch import _points
from tests.test_alert_manager import FakeAlertRepository, FakeRuleRepository, _make_alert_create
from tests.test_threshold_evaluator import _make_rule


def _rule(rule_type=RuleType.THRESHOLD_ABOVE, **cfg):
    return _make_rule(rule_type=rule_type, threshold=ThresholdConfig(**(cfg or {"value": 90.0})))


def _engine_with_alert(rule, points):
    """Engine whose rule already has one ongoing alert; records closes and clear notices."""
    repo = FakeAlertRepository()
    manager = AlertManager(alert_repository=repo, rule_repository=FakeRuleRepository())
    alert = manager.process_alert(_make_alert_create(rule_id=rule.rule_id))
    cleared = []
    eng = RuleEngine(
        rule_repository=SimpleNamespace(_save_rule=lambda *a, **k: None),
        alert_repository=repo, alert_manager=manager,
        notification_dispatcher=SimpleNamespace(notify_alert_resolved=cleared.append,
                                                send_notifications=lambda a: None),
        metric_repo=SimpleNamespace(get_points=lambda *a, **k: points),
    )
    return eng, repo, alert, cleared


# ── #1186 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [99, 1])
async def test_quiet_hours_keep_an_ongoing_alert_open(monkeypatch, value):
    monkeypatch.setattr(ThresholdEvaluator, "_is_in_quiet_hours", lambda self, rule: True)
    rule = _rule()
    eng, repo, alert, cleared = _engine_with_alert(rule, _points([value] * 6, now_utc()))
    for _ in range(5):
        assert await eng._evaluate_rule(rule) is False
    assert repo.get_by_id(alert.alert_id).status == AlertStatus.ACTIVE
    assert cleared == []
    assert eng._ok_streak == {}
    assert eng._recent_evaluations[-1]["status"] == "quiet"


async def test_recovery_still_closes_outside_quiet_hours():
    rule = _rule()
    eng, repo, alert, cleared = _engine_with_alert(rule, _points([1] * 6, now_utc()))
    for _ in range(2):
        await eng._evaluate_rule(rule)
    assert repo.get_by_id(alert.alert_id).status == AlertStatus.CLOSED
    assert len(cleared) == 1


# ── #1190 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan"])
def test_metric_point_rejects_non_finite_values(bad):
    with pytest.raises(ValueError):
        MetricPoint(source="cpu", metric_name="usage_percent", value=bad)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_ingest_route_answers_422_for_non_finite_values(monkeypatch, raw):
    from config.unified_config import settings
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", "", raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", "", raising=False)
    from backend.api.routes import metrics as metrics_routes
    monkeypatch.setattr(metrics_routes, "_metric_repo", object())
    client = TestClient(ae.app, raise_server_exceptions=False)
    body = '{"source":"cpu","metric_name":"usage_percent","value":%s}' % raw
    r = client.post("/api/alarm/metrics", content=body,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "value"]


@pytest.mark.parametrize("rule", [
    _rule(RuleType.THRESHOLD_ABOVE, value=90.0),
    _rule(RuleType.THRESHOLD_BELOW, value=10.0),
    _rule(RuleType.THRESHOLD_RANGE, lower=10.0, upper=90.0),
])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_value_is_never_a_breach(rule, bad):
    assert ThresholdEvaluator().evaluate_rule(rule, bad, []) is None


async def test_non_finite_sample_neither_fires_nor_resolves():
    rule = _rule()
    points = _points([1] * 5, now_utc())
    points.append(MetricPoint.model_construct(
        source="cpu", metric_name="usage_percent", value=float("nan"), unit="%",
        timestamp=now_utc(), hostname="h"))
    eng, repo, alert, cleared = _engine_with_alert(rule, points)
    for _ in range(5):
        assert await eng._evaluate_rule(rule) is False
    assert repo.get_by_id(alert.alert_id).status == AlertStatus.ACTIVE
    assert cleared == []
    assert eng._recent_evaluations[-1]["status"] == "invalid"


# ── #1189 ────────────────────────────────────────────────────────────────────

class _FakeSMTP:
    calls: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls.append((type(self).__name__, port))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")

    def send_message(self, msg):
        self.calls.append("send")


class _FakeSMTPSSL(_FakeSMTP):
    pass


@pytest.mark.parametrize("port,expected", [
    (465, [("_FakeSMTPSSL", 465), "login", "send"]),
    (587, [("_FakeSMTP", 587), "starttls", "login", "send"]),
    (25, [("_FakeSMTP", 25), "login", "send"]),
])
def test_email_transport_follows_the_port(monkeypatch, port, expected):
    _FakeSMTP.calls = []
    monkeypatch.setattr(nd.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(nd.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    monkeypatch.setattr(nd, "settings", SimpleNamespace(notifications=SimpleNamespace(
        smtp=SimpleNamespace(server="mail.example", port=port, user="u@example", password="p"),
        timeouts=SimpleNamespace(smtp=5))))
    nd.NotificationDispatcher._send_sync_email(SimpleNamespace(), MIMEText("body"), None)
    assert _FakeSMTP.calls == expected


# ── #1187 ────────────────────────────────────────────────────────────────────

@pytest.fixture
def manager():
    return AlertManager(alert_repository=FakeAlertRepository(), rule_repository=FakeRuleRepository())


def test_close_rule_alerts_closes_only_that_rule(manager):
    mine, other = uuid4(), uuid4()
    a = manager.process_alert(_make_alert_create(rule_id=mine))
    b = manager.process_alert(_make_alert_create(rule_id=other))
    assert manager.close_rule_alerts(str(mine), "rule deleted") == 1
    repo = manager.alert_repository
    assert repo.get_by_id(a.alert_id).status == AlertStatus.CLOSED
    assert repo.get_by_id(a.alert_id).resolution_reason == "rule deleted"
    assert repo.get_by_id(b.alert_id).status == AlertStatus.ACTIVE


class _RuleRepo:
    def __init__(self, rule):
        self.rule = rule

    def get_by_id(self, uid):
        return self.rule if self.rule and uid == self.rule.rule_id else None

    def update(self, uid, update):
        for k, v in update.model_dump(exclude_unset=True).items():
            setattr(self.rule, k, v)
        return self.rule

    def delete(self, uid):
        return self.get_by_id(uid) is not None

    def delete_all(self):
        return True

    def get_all(self, enabled_only=True):
        return [self.rule]


@pytest.fixture
def wired(manager):
    rule = _rule()
    alert = manager.process_alert(_make_alert_create(rule_id=rule.rule_id))
    saved = dict(rules_routes._dependency_map)
    rules_routes.set_dependencies({"rule_repository": _RuleRepo(rule), "alert_manager": manager})
    yield rule, lambda: manager.alert_repository.get_by_id(alert.alert_id)
    rules_routes.set_dependencies(saved)


async def test_deleting_a_rule_closes_its_alerts(wired):
    rule, alert = wired
    await rules_routes.delete_rule(str(rule.rule_id))
    assert (alert().status, alert().resolution_reason) == (AlertStatus.CLOSED, "rule deleted")


async def test_deleting_all_rules_closes_their_alerts_only(wired, manager):
    _, alert = wired
    external = manager.process_alert(_make_alert_create(rule_id=uuid4()))
    await rules_routes.delete_all_rules()
    assert (alert().status, alert().resolution_reason) == (AlertStatus.CLOSED, "rule deleted")
    assert manager.alert_repository.get_by_id(external.alert_id).status == AlertStatus.ACTIVE


async def test_toggling_a_rule_off_closes_its_alerts(wired):
    rule, alert = wired
    await rules_routes.toggle_rule(str(rule.rule_id))
    assert (alert().status, alert().resolution_reason) == (AlertStatus.CLOSED, "rule disabled")


async def test_updating_a_rule_to_disabled_closes_its_alerts(wired):
    rule, alert = wired
    await rules_routes.update_rule(str(rule.rule_id), AlarmRuleUpdate(enabled=False))
    assert (alert().status, alert().resolution_reason) == (AlertStatus.CLOSED, "rule disabled")


async def test_updating_an_enabled_rule_keeps_its_alerts(wired):
    rule, alert = wired
    await rules_routes.update_rule(str(rule.rule_id), AlarmRuleUpdate(name="renamed"))
    assert alert().status == AlertStatus.ACTIVE
