"""
Unit tests for backend.engine.threshold_evaluator.

Threshold logic is pure — no DB, no network — so each test fabricates an
AlarmRule, feeds a value into ThresholdEvaluator.evaluate_rule(), and asserts
on the returned AlertCreate (or None).
"""
from __future__ import annotations

from datetime import datetime
from backend._time import now_utc
from uuid import uuid4

import pytest

from backend.engine.threshold_evaluator import ThresholdEvaluator
from backend.models.alarm_rule import (
    AlarmRule,
    RuleSpecificConfig,
    RuleType,
    Severity,
    ThresholdConfig,
)


def _make_rule(
    *,
    rule_type: RuleType,
    threshold: ThresholdConfig,
    enabled: bool = True,
    severity: Severity = Severity.WARNING,
    quiet_start: str | None = None,
    quiet_end: str | None = None,
    source_host: str | None = None,
) -> AlarmRule:
    return AlarmRule(
        rule_id=uuid4(),
        name="test-rule",
        description=None,
        source_host=source_host,
        metric_source="cpu",
        metric_name="usage_percent",
        rule_type=rule_type,
        config=RuleSpecificConfig(threshold=threshold),
        severity=severity,
        enabled=enabled,
        notification_channel_ids=[],
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        auto_resolve_cycles=2,
        created_at=now_utc(),
        updated_at=now_utc(),
        last_evaluated_at=None,
        last_alert_at=None,
    )


@pytest.fixture
def evaluator():
    return ThresholdEvaluator()


# ── THRESHOLD_ABOVE ──────────────────────────────────────────────────────────

class TestThresholdAbove:
    def test_breach_fires_alert(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=80.0, unit="%"),
        )
        result = evaluator.evaluate_rule(rule, 95.5)
        assert result is not None
        assert result.current_value == 95.5
        assert result.threshold_value == 80.0
        assert result.severity == "warning"
        assert "exceeds" in result.message
        assert "95.5%" in result.message  # _fmt: no space for %

    def test_at_threshold_does_not_fire(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=80.0),
        )
        assert evaluator.evaluate_rule(rule, 80.0) is None

    def test_below_threshold_does_not_fire(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=80.0),
        )
        assert evaluator.evaluate_rule(rule, 79.99) is None

    def test_upper_field_takes_precedence_over_value(self, evaluator):
        # _resolve_upper checks upper → value → critical → warning in order
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(upper=90.0, value=50.0),
        )
        result = evaluator.evaluate_rule(rule, 85.0)
        # 85 > 50 (value) but 85 < 90 (upper) — upper wins, so no fire
        assert result is None
        result = evaluator.evaluate_rule(rule, 95.0)
        assert result is not None
        assert result.threshold_value == 90.0

    def test_critical_field_used_when_upper_value_missing(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(critical=70.0),
        )
        result = evaluator.evaluate_rule(rule, 75.0)
        assert result is not None
        assert result.threshold_value == 70.0

    def test_no_threshold_configured_does_not_fire(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(),  # no fields set
        )
        assert evaluator.evaluate_rule(rule, 1_000_000) is None


# ── THRESHOLD_BELOW ──────────────────────────────────────────────────────────

class TestThresholdBelow:
    def test_breach_fires_alert(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_BELOW,
            threshold=ThresholdConfig(value=10.0, unit="%"),
            severity=Severity.CRITICAL,
        )
        result = evaluator.evaluate_rule(rule, 5.0)
        assert result is not None
        assert result.threshold_value == 10.0
        assert result.severity == "critical"
        assert "falls below" in result.message

    def test_at_threshold_does_not_fire(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_BELOW,
            threshold=ThresholdConfig(value=10.0),
        )
        assert evaluator.evaluate_rule(rule, 10.0) is None

    def test_above_threshold_does_not_fire(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_BELOW,
            threshold=ThresholdConfig(value=10.0),
        )
        assert evaluator.evaluate_rule(rule, 10.01) is None

    def test_lower_takes_precedence_over_value(self, evaluator):
        # _resolve_lower: lower → value → warning → critical
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_BELOW,
            threshold=ThresholdConfig(lower=20.0, value=50.0),
        )
        # 15 < 50 (value) but 15 < 20 (lower) too — lower wins
        result = evaluator.evaluate_rule(rule, 15.0)
        assert result is not None
        assert result.threshold_value == 20.0


# ── THRESHOLD_RANGE ──────────────────────────────────────────────────────────

class TestThresholdRange:
    def test_inside_range_does_not_fire(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_RANGE,
            threshold=ThresholdConfig(lower=10.0, upper=90.0),
        )
        for value in (10.0, 50.0, 90.0):  # inclusive on both ends
            assert evaluator.evaluate_rule(rule, value) is None

    def test_above_range_fires(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_RANGE,
            threshold=ThresholdConfig(lower=10.0, upper=90.0),
        )
        result = evaluator.evaluate_rule(rule, 95.0)
        assert result is not None
        assert result.threshold_value == 90.0  # upper bound on over-breach
        assert "outside range" in result.message

    def test_below_range_fires(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_RANGE,
            threshold=ThresholdConfig(lower=10.0, upper=90.0),
        )
        result = evaluator.evaluate_rule(rule, 5.0)
        assert result is not None
        assert result.threshold_value == 10.0  # lower bound on under-breach

    def test_partial_range_config_does_not_fire(self, evaluator):
        # Range needs BOTH bounds; missing either is a no-op.
        for cfg in (
            ThresholdConfig(lower=10.0),
            ThresholdConfig(upper=90.0),
            ThresholdConfig(),
        ):
            rule = _make_rule(rule_type=RuleType.THRESHOLD_RANGE, threshold=cfg)
            assert evaluator.evaluate_rule(rule, 5.0) is None
            assert evaluator.evaluate_rule(rule, 95.0) is None


# ── Rule gating: enabled, quiet hours ────────────────────────────────────────

class TestRuleGating:
    def test_disabled_rule_never_fires(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=10.0),
            enabled=False,
        )
        assert evaluator.evaluate_rule(rule, 1_000_000) is None

    def test_quiet_hours_within_window_suppresses(self, evaluator, monkeypatch):
        # Pin "now" to 03:30 UTC so we control the quiet-hours math deterministically.
        monkeypatch.setattr(
            "backend.engine.threshold_evaluator.now_utc",
            lambda: datetime(2026, 1, 1, 3, 30, 0),
        )
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=10.0),
            quiet_start="02:00",
            quiet_end="06:00",
        )
        assert evaluator.evaluate_rule(rule, 100.0) is None

    def test_quiet_hours_outside_window_fires(self, evaluator, monkeypatch):
        monkeypatch.setattr(
            "backend.engine.threshold_evaluator.now_utc",
            lambda: datetime(2026, 1, 1, 12, 0, 0),
        )
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=10.0),
            quiet_start="02:00",
            quiet_end="06:00",
        )
        assert evaluator.evaluate_rule(rule, 100.0) is not None

    def test_quiet_hours_wraparound_midnight(self, evaluator, monkeypatch):
        # 22:00-06:00 wraps midnight; 03:00 is inside the window.
        monkeypatch.setattr(
            "backend.engine.threshold_evaluator.now_utc",
            lambda: datetime(2026, 1, 1, 3, 0, 0),
        )
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=10.0),
            quiet_start="22:00",
            quiet_end="06:00",
        )
        assert evaluator.evaluate_rule(rule, 100.0) is None

    def test_malformed_quiet_hours_does_not_suppress(self, evaluator):
        # Bad format should fail open (alert fires) rather than suppress silently.
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=10.0),
            quiet_start="not-a-time",
            quiet_end="also-not",
        )
        result = evaluator.evaluate_rule(rule, 100.0)
        assert result is not None


# ── Hostname propagation ─────────────────────────────────────────────────────

class TestHostPropagation:
    def test_host_from_latest_metric_point(self, evaluator):
        from backend.models.metrics import MetricPoint

        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=10.0),
        )
        points = [
            MetricPoint(source="cpu", metric_name="usage_percent",
                        value=95.0, timestamp=now_utc(), hostname="gpu-host"),
        ]
        result = evaluator.evaluate_rule(rule, 95.0, metric_points=points)
        assert result.source_host == "gpu-host"

    def test_host_falls_back_to_rule_source_host(self, evaluator):
        rule = _make_rule(
            rule_type=RuleType.THRESHOLD_ABOVE,
            threshold=ThresholdConfig(value=10.0),
            source_host="rule-bound-host",
        )
        # No metric_points OR latest point lacks hostname → use rule.source_host
        result = evaluator.evaluate_rule(rule, 95.0, metric_points=None)
        assert result.source_host == "rule-bound-host"


# ── Format helpers ───────────────────────────────────────────────────────────

class TestFmt:
    def test_percent_has_no_space(self):
        assert ThresholdEvaluator._fmt(97.97702, "%") == "97.98%"

    def test_celsius_has_no_space(self):
        assert ThresholdEvaluator._fmt(85.0, "°C") == "85.0°C"

    def test_general_unit_has_leading_space(self):
        assert ThresholdEvaluator._fmt(12.345, "Mbps") == "12.35 Mbps"

    def test_no_unit(self):
        assert ThresholdEvaluator._fmt(3.14159, None) == "3.14"
        assert ThresholdEvaluator._fmt(3.14159, "") == "3.14"

    def test_rounding(self):
        assert ThresholdEvaluator._fmt(0.005, "") == "0.01"  # banker's rounding edge


class TestResolveUpper:
    def test_priority_order(self):
        # upper → value → critical → warning
        assert ThresholdEvaluator._resolve_upper(
            ThresholdConfig(upper=1, value=2, critical=3, warning=4)) == 1
        assert ThresholdEvaluator._resolve_upper(
            ThresholdConfig(value=2, critical=3, warning=4)) == 2
        assert ThresholdEvaluator._resolve_upper(
            ThresholdConfig(critical=3, warning=4)) == 3
        assert ThresholdEvaluator._resolve_upper(
            ThresholdConfig(warning=4)) == 4
        assert ThresholdEvaluator._resolve_upper(ThresholdConfig()) is None


class TestResolveLower:
    def test_priority_order(self):
        # lower → value → warning → critical
        assert ThresholdEvaluator._resolve_lower(
            ThresholdConfig(lower=1, value=2, warning=3, critical=4)) == 1
        assert ThresholdEvaluator._resolve_lower(
            ThresholdConfig(value=2, warning=3, critical=4)) == 2
        assert ThresholdEvaluator._resolve_lower(
            ThresholdConfig(warning=3, critical=4)) == 3
        assert ThresholdEvaluator._resolve_lower(
            ThresholdConfig(critical=4)) == 4
        assert ThresholdEvaluator._resolve_lower(ThresholdConfig()) is None
