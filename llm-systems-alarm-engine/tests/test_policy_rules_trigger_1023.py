"""#1023: policy trigger counter, rule filters on policies, rule
min_trigger_cycles, per-alert delivery lookup."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend._time import now_utc
from backend.engine.notification_dispatcher import NotificationDispatcher
from backend.engine.rule_engine import RuleEngine
from backend.models.alarm_rule import (
    AlarmRule, AlarmRuleCreate, AlarmRuleUpdate, RuleSpecificConfig, RuleType,
    Severity, ThresholdConfig,
)
from backend.models.metrics import MetricPoint
from backend.models.notification import NotificationConfig, NotificationConfigCreate
from backend.storage.ae_settings_db import AeSettingsDB
from backend.storage.cache import MetricCache
from backend.storage.repositories import NotificationRepository, RuleRepository


def _policy(**kw):
    base = dict(config_id=uuid4(), name="p", description=None, channels=[uuid4()],
                enabled=True, created_at=now_utc(), last_triggered_at=None, trigger_count=0)
    base.update(kw)
    return NotificationConfig(**base)


def _alert(rule_id=None, **kw):
    return SimpleNamespace(alert_id=uuid4(), rule_id=rule_id or uuid4(), severity="warning",
                           metric_source="cpu", metric_name="usage_percent", source_host="h",
                           status="active", incident_id=None, **kw)


class _Repo:
    def __init__(self, policies):
        self._p = policies
        self.bumped = []

    def list_configs(self):
        return list(self._p)

    def increment_trigger_count(self, config_id):
        self.bumped.append(str(config_id))


# ── policy rule filter ──────────────────────────────────────────────────────

def test_policy_with_rule_ids_matches_only_those_rules():
    r1, r2 = uuid4(), uuid4()
    p = _policy(rule_ids=[str(r1)])
    assert p.matches_alert(_alert(rule_id=r1)) is True
    assert p.matches_alert(_alert(rule_id=r2)) is False


def test_policy_without_rule_ids_matches_any_rule():
    assert _policy().matches_alert(_alert()) is True


def test_config_create_accepts_rule_ids():
    rid = uuid4()
    c = NotificationConfigCreate(name="x", channels=[], rule_ids=[rid])
    assert c.rule_ids == [str(rid)]


# ── trigger counter ─────────────────────────────────────────────────────────

def test_firing_dispatch_bumps_matched_policy_trigger_count():
    p = _policy()
    repo = _Repo([p])
    d = NotificationDispatcher(notification_repository=repo)
    out = d._policies_that_should_dispatch(_alert(), "firing", [p])
    assert out == {str(p.channels[0])}
    assert repo.bumped == [str(p.config_id)]


def test_unmatched_policy_is_not_bumped():
    p = _policy(min_severity="critical")
    repo = _Repo([p])
    d = NotificationDispatcher(notification_repository=repo)
    assert d._policies_that_should_dispatch(_alert(), "firing", [p]) == set()
    assert repo.bumped == []


def test_min_count_gate_defers_the_bump():
    p = _policy(min_alarm_count=2)
    repo = _Repo([p])
    d = NotificationDispatcher(notification_repository=repo)
    a = _alert()
    d._policies_that_should_dispatch(a, "firing", [p])
    assert repo.bumped == []
    d._policies_that_should_dispatch(a, "firing", [p])
    assert repo.bumped == [str(p.config_id)]


# ── settings db round trips ─────────────────────────────────────────────────

def test_config_rule_ids_round_trip(tmp_path):
    db = AeSettingsDB.open(tmp_path / "s.db")
    try:
        repo = NotificationRepository(MetricCache(), settings_db=db)
        rid = uuid4()
        cfg = repo.create_config(NotificationConfigCreate(name="x", channels=[], rule_ids=[rid]))
        assert db.get_config(str(cfg.config_id))["rule_ids"] == [str(rid)]
        assert repo.get_config(cfg.config_id).rule_ids == [str(rid)]
    finally:
        db.close()


def test_rule_min_trigger_cycles_round_trip(tmp_path):
    db = AeSettingsDB.open(tmp_path / "s.db")
    try:
        repo = RuleRepository(MetricCache(), settings_db=db)
        rule = repo.create(AlarmRuleCreate(
            name="r", metric_source="cpu", metric_name="usage_percent",
            rule_type=RuleType.THRESHOLD_ABOVE,
            config=RuleSpecificConfig(threshold=ThresholdConfig(value=10.0)),
            min_trigger_cycles=3,
        ))
        assert db.query_rules()[0]["min_trigger_cycles"] == 3
        assert repo.get_by_id(rule.rule_id).min_trigger_cycles == 3
        repo.update(rule.rule_id, AlarmRuleUpdate(min_trigger_cycles=1))
        assert db.query_rules()[0]["min_trigger_cycles"] == 1
    finally:
        db.close()


def test_min_trigger_cycles_rejects_zero():
    with pytest.raises(Exception):
        AlarmRuleCreate(
            name="r", metric_source="cpu", metric_name="usage_percent",
            rule_type=RuleType.THRESHOLD_ABOVE,
            config=RuleSpecificConfig(threshold=ThresholdConfig(value=10.0)),
            min_trigger_cycles=0,
        )


# ── rule engine breach streak ───────────────────────────────────────────────

def _rule(min_trigger_cycles=1):
    return AlarmRule(
        rule_id=uuid4(), name="r", description=None, source_host=None,
        metric_source="cpu", metric_name="usage_percent",
        rule_type=RuleType.THRESHOLD_ABOVE,
        config=RuleSpecificConfig(threshold=ThresholdConfig(value=10.0)),
        severity=Severity.WARNING, enabled=True, notification_channel_ids=[],
        quiet_hours_start=None, quiet_hours_end=None, auto_resolve_cycles=2,
        min_trigger_cycles=min_trigger_cycles,
        created_at=now_utc(), updated_at=now_utc(), last_evaluated_at=None, last_alert_at=None,
    )


def _points(values):
    end = now_utc()
    return [MetricPoint(source="cpu", metric_name="usage_percent", value=float(v), unit="%",
                        timestamp=end - timedelta(seconds=15 * (len(values) - 1 - i)), hostname="h")
            for i, v in enumerate(values)]


def _engine(holder):
    calls = []
    mgr = SimpleNamespace(process_alert=lambda ac: calls.append(ac) or None)
    eng = RuleEngine(
        rule_repository=SimpleNamespace(_save_rule=lambda *a, **k: None),
        alert_repository=SimpleNamespace(get_active=lambda: []),
        alert_manager=mgr, notification_dispatcher=None,
        metric_repo=SimpleNamespace(get_points=lambda *a, **k: holder["points"]),
    )
    return eng, calls


def test_alert_waits_for_min_trigger_cycles():
    holder = {"points": _points([50, 50, 50])}
    eng, calls = _engine(holder)
    rule = _rule(min_trigger_cycles=3)
    for _ in range(2):
        asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    assert calls == []
    asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    assert len(calls) == 1


def test_clean_cycle_resets_the_breach_streak():
    holder = {"points": _points([50, 50, 50])}
    eng, calls = _engine(holder)
    rule = _rule(min_trigger_cycles=2)
    asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    holder["points"] = _points([1, 1, 1])
    asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    holder["points"] = _points([50, 50, 50])
    asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    assert calls == []
    asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    assert len(calls) == 1


def test_streak_restarts_after_the_alert_is_created():
    holder = {"points": _points([50, 50, 50])}
    eng, calls = _engine(holder)
    rule = _rule(min_trigger_cycles=3)
    for _ in range(3):
        asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    assert len(calls) == 1
    # Operator closed the alert while the metric still breaches: three more cycles are needed.
    for _ in range(2):
        asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    assert len(calls) == 1
    asyncio.run(eng._evaluate_rule(rule, active_alerts=[]))
    assert len(calls) == 2


def test_default_min_trigger_cycles_fires_on_first_breach():
    holder = {"points": _points([50, 50, 50])}
    eng, calls = _engine(holder)
    asyncio.run(eng._evaluate_rule(_rule(), active_alerts=[]))
    assert len(calls) == 1


# ── per-alert deliveries ────────────────────────────────────────────────────

def test_deliveries_for_alert_only_returns_that_alerts_rows(tmp_path):
    db = AeSettingsDB.open(tmp_path / "s.db")
    try:
        repo = NotificationRepository(MetricCache(), settings_db=db)
        a, b = str(uuid4()), str(uuid4())
        for aid, ok in ((a, True), (b, True), (a, False)):
            repo.record_delivery(channel_id=None, channel_type="email", title="t", body="b",
                                 severity="warning", recipient="ops@example.com", success=ok,
                                 error_message=None if ok else "boom", metadata={"alert_id": aid})
        rows = repo.get_deliveries_for_alert(a)
        assert len(rows) == 2
        assert {r.success for r in rows} == {True, False}
        assert all(r.metadata.get("alert_id") == a for r in rows)
    finally:
        db.close()


# ── rules list exposes the pending breach streak ────────────────────────────

def test_rules_list_reports_breaching_cycles(tmp_path):
    from backend.api.routes import rules as rules_routes
    db = AeSettingsDB.open(tmp_path / "s.db")
    try:
        repo = RuleRepository(MetricCache(), settings_db=db)
        rule = repo.create(AlarmRuleCreate(
            name="r", metric_source="cpu", metric_name="usage_percent",
            rule_type=RuleType.THRESHOLD_ABOVE,
            config=RuleSpecificConfig(threshold=ThresholdConfig(value=10.0)),
            min_trigger_cycles=3,
        ))
        engine = SimpleNamespace(_breach_streak={str(rule.rule_id): 2})
        rules_routes.set_dependencies({"rule_repository": repo, "rule_engine": engine})
        rows = asyncio.run(rules_routes.list_rules())
        assert rows[0]["breaching"] == 2
        rules_routes.set_dependencies({"rule_repository": repo})
        assert asyncio.run(rules_routes.list_rules())[0]["breaching"] == 0
    finally:
        rules_routes.set_dependencies({})
        db.close()
