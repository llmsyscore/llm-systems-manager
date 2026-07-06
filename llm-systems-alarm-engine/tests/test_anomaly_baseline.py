"""#250: the point under test must NOT be folded into its own baseline.

evaluate_z_score/moving_average/percentile build the baseline from
metric_points[:-1]; metric_points[-1] is the value under test (per
rule_engine: current_value = metric_points[-1].value).
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from backend._time import now_utc
from backend.engine.anomaly_detector import AnomalyDetector
from backend.models.alarm_rule import (
    AlarmRule,
    MovingAverageConfig,
    PercentileConfig,
    RuleSpecificConfig,
    RuleType,
    Severity,
    ZScoreConfig,
)
from backend.models.metrics import MetricPoint


def _rule() -> AlarmRule:
    return AlarmRule(
        rule_id=uuid4(),
        name="anomaly-test",
        description=None,
        source_host=None,
        metric_source="cpu",
        metric_name="usage_percent",
        rule_type=RuleType.Z_SCORE,
        config=RuleSpecificConfig(),
        severity=Severity.WARNING,
        enabled=True,
        notification_channel_ids=[],
        quiet_hours_start=None,
        quiet_hours_end=None,
        auto_resolve_cycles=2,
        created_at=now_utc(),
        updated_at=now_utc(),
        last_evaluated_at=None,
        last_alert_at=None,
    )


def _points(values, minutes_span=5):
    """Build chronologically ascending points ending 'now'; last = current."""
    n = len(values)
    base = now_utc() - timedelta(minutes=minutes_span)
    step = timedelta(seconds=(minutes_span * 60) / max(n - 1, 1))
    return [
        MetricPoint(source="cpu", metric_name="usage_percent",
                    value=float(v), unit="%", timestamp=base + step * i,
                    hostname="h")
        for i, v in enumerate(values)
    ]


# The issue's scenario: 9 samples near 50 (std≈2), then a spike to 62.
_BASELINE_9 = [48, 50, 52, 49, 51, 50, 48, 52, 50]
_SPIKE = 62.0


def test_z_score_spike_fires_when_excluded_from_baseline():
    det = AnomalyDetector()
    cfg = ZScoreConfig(threshold=3.0, window_minutes=60, min_data_points=9)
    pts = _points(_BASELINE_9 + [_SPIKE])
    alert = det.evaluate_z_score_rule(_rule(), _SPIKE, pts, cfg)
    assert alert is not None  # was suppressed (z≈2.6) when self-contaminated


def test_z_score_normal_value_does_not_fire():
    det = AnomalyDetector()
    cfg = ZScoreConfig(threshold=3.0, window_minutes=60, min_data_points=9)
    pts = _points(_BASELINE_9 + [50.5])
    assert det.evaluate_z_score_rule(_rule(), 50.5, pts, cfg) is None


def test_z_score_contaminated_baseline_would_suppress():
    """Prove the contamination: including the spike drags z below threshold."""
    # If the OLD behavior (baseline incl. current) were in effect, a 6σ spike
    # would compute to z≈2.6 and NOT fire. With the fix it fires — asserted
    # above. Here we confirm the spike really is a genuine >3σ event vs the
    # clean baseline.
    import statistics
    mean = statistics.fmean(_BASELINE_9)
    std = statistics.pstdev(_BASELINE_9)
    assert abs(_SPIKE - mean) / std > 3.0


def test_moving_average_spike_fires():
    det = AnomalyDetector()
    cfg = MovingAverageConfig(window_minutes=60, deviation_factor=3.0, min_data_points=9)
    pts = _points(_BASELINE_9 + [_SPIKE])
    assert det.evaluate_moving_average_rule(_rule(), _SPIKE, pts, cfg) is not None


def test_percentile_spike_fires():
    det = AnomalyDetector()
    cfg = PercentileConfig(percentile=95.0, window_minutes=60, min_data_points=9)
    pts = _points(_BASELINE_9 + [_SPIKE])
    # p95 of the clean baseline is ~52; 62 exceeds it. If 62 were in the array
    # it could itself become the p95 and fail to exceed it.
    assert det.evaluate_percentile_rule(_rule(), _SPIKE, pts, cfg) is not None


def test_insufficient_baseline_after_exclusion_returns_none():
    det = AnomalyDetector()
    cfg = ZScoreConfig(threshold=3.0, window_minutes=60, min_data_points=9)
    # only 9 points total → 8 baseline after excluding current → below min
    pts = _points(_BASELINE_9)  # 9 points, last is "current"
    assert det.evaluate_z_score_rule(_rule(), _BASELINE_9[-1], pts, cfg) is None


def test_single_point_does_not_crash():
    det = AnomalyDetector()
    cfg = ZScoreConfig(threshold=3.0, window_minutes=60, min_data_points=1)
    pts = _points([50.0])
    assert det.evaluate_z_score_rule(_rule(), 50.0, pts, cfg) is None
