"""AE audit batch (#690-#694): stale-metric gate, honest SMS delivery rows,
local-time quiet hours, no dead evaluation loop, no no-op update_channel."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend._time import now_utc
from backend.engine.notification_dispatcher import NotificationDispatcher
from backend.engine.rule_engine import RuleEngine
from backend.engine.threshold_evaluator import ThresholdEvaluator
from backend.models.alarm_rule import RuleType, ThresholdConfig
from backend.models.metrics import MetricPoint
from tests.test_threshold_evaluator import _make_rule


def _rule(threshold=10.0, quiet_start=None, quiet_end=None):
    return _make_rule(rule_type=RuleType.THRESHOLD_ABOVE, threshold=ThresholdConfig(value=threshold),
                      quiet_start=quiet_start, quiet_end=quiet_end)


def _must_not_fire(*a, **k):
    raise AssertionError("must not fire")


def _points(values, end):
    return [MetricPoint(source="cpu", metric_name="usage_percent", value=float(v), unit="%",
                        timestamp=end - timedelta(seconds=15 * (len(values) - 1 - i)), hostname="h")
            for i, v in enumerate(values)]


def _engine(points, **kw):
    return RuleEngine(
        rule_repository=SimpleNamespace(_save_rule=lambda *a, **k: None),
        alert_repository=SimpleNamespace(get_active=lambda: []),
        alert_manager=SimpleNamespace(create_alert=_must_not_fire), notification_dispatcher=SimpleNamespace(),
        metric_repo=SimpleNamespace(get_points=lambda *a, **k: points), **kw,
    )


# ── #690 ─────────────────────────────────────────────────────────────────────

async def test_stale_metric_points_are_not_evaluated():
    eng = _engine(_points([100, 100, 100], now_utc() - timedelta(minutes=20)))
    assert await eng._evaluate_rule(_rule(), active_alerts=[]) is False
    assert eng._recent_evaluations[-1]["status"] == "stale"


async def test_fresh_metric_points_are_evaluated():
    eng = _engine(_points([1, 1, 1], now_utc()))
    assert await eng._evaluate_rule(_rule(), active_alerts=[]) is False
    assert eng._recent_evaluations[-1]["status"] == "ok"


async def test_stale_gate_handles_tz_aware_timestamps():
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    eng = _engine(_points([100, 100, 100], end))
    assert await eng._evaluate_rule(_rule(), active_alerts=[]) is False
    assert eng._recent_evaluations[-1]["status"] == "stale"


def test_stale_gate_default_and_override():
    assert _engine([]).metric_max_age_s == 600.0
    assert _engine([], metric_max_age_s=42.0).metric_max_age_s == 42.0


async def test_stale_gap_resets_the_auto_resolve_streak():
    eng = _engine(_points([1, 1, 1], now_utc()))
    rule = _rule()
    await eng._evaluate_rule(rule, active_alerts=[])
    assert eng._ok_streak.get(str(rule.rule_id)) == 1
    eng.metric_repo = SimpleNamespace(get_points=lambda *a, **k: _points([1, 1, 1], now_utc() - timedelta(hours=1)))
    await eng._evaluate_rule(rule, active_alerts=[])
    assert str(rule.rule_id) not in eng._ok_streak


def test_rule_engine_has_no_dangling_is_running():
    assert not hasattr(RuleEngine, "is_running")


# ── #693 ─────────────────────────────────────────────────────────────────────

def test_rule_engine_has_no_parallel_evaluation_loop():
    for name in ("start", "stop", "_evaluation_loop"):
        assert not hasattr(RuleEngine, name), name
    with pytest.raises(TypeError):
        _engine([], evaluation_interval=30.0)


# ── #694 ─────────────────────────────────────────────────────────────────────

def test_dispatcher_has_no_noop_update_channel():
    assert not hasattr(NotificationDispatcher, "update_channel")


# ── #691 ─────────────────────────────────────────────────────────────────────

async def test_sms_channel_records_failure_not_success():
    d = NotificationDispatcher()
    rec = []
    d._record_delivery = lambda *a, **k: rec.append(k)
    chan = SimpleNamespace(channel_id="c1", config=SimpleNamespace(sms=SimpleNamespace(enabled=True, to_number="+15550000000")))
    alert = SimpleNamespace(severity="warning", rule_name="r", message="m")
    await d._send_sms_channels(alert, [chan])
    assert len(rec) == 1 and rec[0]["success"] is False
    assert "not implemented" in rec[0]["error_message"].lower()


# ── #692 ─────────────────────────────────────────────────────────────────────

def _pin(monkeypatch, utc_hhmm, tz):
    h, m = utc_hhmm
    monkeypatch.setattr("backend.engine.threshold_evaluator.now_utc", lambda: datetime(2026, 1, 1, h, m))
    monkeypatch.setattr("backend.engine.threshold_evaluator._quiet_tz", lambda: tz)


def test_quiet_hours_use_local_time(monkeypatch):
    # 03:30 UTC is 23:30 in UTC-4 — outside a 02:00-06:00 local window.
    _pin(monkeypatch, (3, 30), timezone(timedelta(hours=-4)))
    ev = ThresholdEvaluator()
    assert ev._is_in_quiet_hours(_rule(quiet_start="02:00", quiet_end="06:00")) is False
    # 07:30 UTC is 03:30 in UTC-4 — inside.
    _pin(monkeypatch, (7, 30), timezone(timedelta(hours=-4)))
    assert ev._is_in_quiet_hours(_rule(quiet_start="02:00", quiet_end="06:00")) is True


def test_quiet_hours_default_zone_follows_the_process_tz(monkeypatch):
    import time as _time
    from backend.engine import threshold_evaluator as te
    monkeypatch.setenv("TZ", "UTC-4")  # POSIX sign: UTC-4 is 4 h AHEAD of UTC
    _time.tzset()
    try:
        assert te._quiet_tz().utcoffset(datetime(2026, 1, 1)) == timedelta(hours=4)
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()
