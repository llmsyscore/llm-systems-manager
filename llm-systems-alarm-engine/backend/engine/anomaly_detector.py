"""Advanced anomaly detection engine.

Provides statistical and machine-learning-style anomaly detection methods:
- Z-score based anomaly detection
- Moving average deviation
- Percentile-based detection
- Rate of change detection
- Trend analysis (linear regression)
- Seasonality detection (basic)
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from .._time import now_utc
from typing import Optional

from ..models.alarm_rule import (
    AlarmRule,
    MovingAverageConfig,
    PercentileConfig,
    RateOfChangeConfig,
    ZScoreConfig,
)
from ..models.alert import AlertCreate
from ..models.metrics import MetricPoint


def _to_naive_utc(dt: datetime) -> datetime:
    """Drop tzinfo, converting to UTC first if aware. MetricPoints from
    InfluxDB carry tz-aware timestamps; cached points are tz-naive."""
    if dt.tzinfo is None:
        return dt
    offset = dt.utcoffset() or timedelta(0)
    return (dt - offset).replace(tzinfo=None)


def _filter_by_window(
    points: list[MetricPoint], window_minutes: int
) -> list[MetricPoint]:
    """Return points whose timestamps fall within the last `window_minutes`."""
    if not points:
        return []
    cutoff = now_utc() - timedelta(minutes=window_minutes)
    return [p for p in points if _to_naive_utc(p.timestamp) >= cutoff]


def _severity_str(rule: "AlarmRule") -> str:
    return rule.severity.value if hasattr(rule.severity, "value") else str(rule.severity)


def _host_from_points(points: Optional[list[MetricPoint]], rule: Optional["AlarmRule"] = None) -> Optional[str]:
    """Pull hostname from the most recent metric point so anomaly alerts
    carry the same source-of-origin context as threshold alerts. Falls back
    to the rule's configured source_host so attribution is reliable even
    when the cached point is missing its hostname tag."""
    if points:
        h = getattr(points[-1], "hostname", None)
        if h:
            return h
    if rule is not None:
        return getattr(rule, "source_host", None)
    return None

logger = logging.getLogger(__name__)


@dataclass
class AnomalyResult:
    """Result of an anomaly detection check."""

    is_anomaly: bool
    score: float  # How anomalous (0.0 = normal, higher = more anomalous)
    method: str
    details: str
    timestamp: datetime


@dataclass
class TrendResult:
    """Result of trend analysis."""

    direction: str  # "increasing", "decreasing", "stable"
    slope: float
    intercept: float
    r_squared: float
    is_significant: bool


@dataclass
class PredictedValue:
    """Prediction from a forecasting method."""

    predicted_value: float
    confidence_lower: float
    confidence_upper: float
    horizon_minutes: int


class AnomalyDetector:
    """Advanced anomaly detection using statistical methods."""

    def __init__(
        self,
        z_score_threshold: float = 3.0,
        min_data_points: int = 10,
        moving_avg_window: int = 50,
        percentile: float = 95.0,
    ):
        self.z_score_threshold = z_score_threshold
        self.min_data_points = min_data_points
        self.moving_avg_window = moving_avg_window
        self.percentile = percentile

    def detect_anomalies(
        self,
        current_value: float,
        metric_points: list[MetricPoint],
        methods: Optional[list[str]] = None,
    ) -> list[AnomalyResult]:
        """Run all configured anomaly detection methods.

        Args:
            current_value: The current metric value to check.
            metric_points: Historical metric data points.
            methods: Which methods to run. Defaults to all.

        Returns:
            List of anomaly results, one per method.
        """
        if len(metric_points) < self.min_data_points:
            return [
                AnomalyResult(
                    is_anomaly=False,
                    score=0.0,
                    method=method,
                    details=f"Insufficient data points ({len(metric_points)} < {self.min_data_points})",
                    timestamp=now_utc(),
                )
                for method in (methods or ["z_score", "moving_average", "percentile"])
            ]

        results = []
        methods = methods or ["z_score", "moving_average", "percentile", "rate_of_change"]

        for method in methods:
            if method == "z_score":
                results.append(self.detect_z_score(current_value, metric_points))
            elif method == "moving_average":
                results.append(self.detect_moving_average(current_value, metric_points))
            elif method == "percentile":
                results.append(self.detect_percentile(current_value, metric_points))
            elif method == "rate_of_change":
                results.append(self.detect_rate_of_change(current_value, metric_points))
            elif method == "trend":
                results.append(self.detect_trend_anomaly(current_value, metric_points))

        return results

    def detect_z_score(self, value: float, points: list[MetricPoint]) -> AnomalyResult:
        """Detect anomalies using Z-score method.

        Returns anomaly if the value is more than z_score_threshold standard deviations
        from the mean of recent data.
        """
        window = points[-self.moving_avg_window :]
        values = [p.value for p in window]
        n = len(values)

        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0
        std_dev = math.sqrt(variance)

        if std_dev < 1e-10:
            return AnomalyResult(
                is_anomaly=False,
                score=0.0,
                method="z_score",
                details=f"Standard deviation too low (std={std_dev:.6f})",
                timestamp=now_utc(),
            )

        z_score = abs(value - mean) / std_dev
        is_anomaly = z_score > self.z_score_threshold

        return AnomalyResult(
            is_anomaly=is_anomaly,
            score=z_score,
            method="z_score",
            details=f"Z-score: {z_score:.2f} (threshold: {self.z_score_threshold}, mean: {mean:.2f}, std: {std_dev:.2f})",
            timestamp=now_utc(),
        )

    def detect_moving_average(self, value: float, points: list[MetricPoint]) -> AnomalyResult:
        """Detect anomalies using moving average deviation.

        Returns anomaly if the value deviates more than 2x the standard deviation
        from the moving average.
        """
        window = points[-self.moving_avg_window :]
        values = [p.value for p in window]
        n = len(values)

        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0
        std_dev = math.sqrt(variance)

        deviation = abs(value - mean)
        threshold = 2.0 * std_dev if std_dev > 0 else mean * 0.1  # 10% of mean as fallback

        # Score: how many standard deviations away
        score = deviation / threshold if threshold > 0 else 0.0
        is_anomaly = score > 1.0

        return AnomalyResult(
            is_anomaly=is_anomaly,
            score=score,
            method="moving_average",
            details=f"Deviation: {deviation:.2f} from MA ({mean:.2f}), threshold: {threshold:.2f}",
            timestamp=now_utc(),
        )

    def detect_percentile(self, value: float, points: list[MetricPoint]) -> AnomalyResult:
        """Detect anomalies using percentile threshold.

        Returns anomaly if the value exceeds the configured percentile of historical data.
        """
        window = points[-self.moving_avg_window :]
        values = sorted([p.value for p in window])
        n = len(values)

        idx = int(self.percentile / 100 * (n - 1))
        percentile_value = values[min(idx, n - 1)]

        # Score: ratio above percentile
        score = value / percentile_value if percentile_value > 0 else (1.0 if value > 0 else 0.0)
        is_anomaly = value > percentile_value

        return AnomalyResult(
            is_anomaly=is_anomaly,
            score=score,
            method="percentile",
            details=f"Value {value:.2f} vs {self.percentile}th percentile ({percentile_value:.2f})",
            timestamp=now_utc(),
        )

    def detect_rate_of_change(self, current_value: float, points: list[MetricPoint]) -> AnomalyResult:
        """Detect anomalies using rate of change.

        Returns anomaly if the value changes too rapidly compared to historical rates.
        """
        if len(points) < 2:
            return AnomalyResult(
                is_anomaly=False,
                score=0.0,
                method="rate_of_change",
                details="Insufficient data points",
                timestamp=now_utc(),
            )

        # Use the last N points for the window
        window = points[-min(20, len(points)) :]

        # Calculate historical rates of change
        rates = []
        for i in range(1, len(window)):
            dt = (window[i].timestamp - window[i - 1].timestamp).total_seconds()
            if dt > 0:
                rate = abs(window[i].value - window[i - 1].value) / dt
                rates.append(rate)

        if not rates:
            return AnomalyResult(
                is_anomaly=False,
                score=0.0,
                method="rate_of_change",
                details="No valid rates calculated",
                timestamp=now_utc(),
            )

        # Current rate of change
        dt = (points[-1].timestamp - window[0].timestamp).total_seconds()
        current_rate = abs(current_value - window[0].value) / dt if dt > 0 else 0

        # Statistical analysis of rates
        mean_rate = sum(rates) / len(rates)
        variance = sum((r - mean_rate) ** 2 for r in rates) / len(rates) if len(rates) > 1 else 0
        std_rate = math.sqrt(variance)

        # Score: how many std devs above mean rate
        score = (current_rate - mean_rate) / std_rate if std_rate > 0 else (
            1.0 if current_rate > mean_rate else 0.0
        )

        is_anomaly = score > 2.0  # 2 std devs above mean rate

        return AnomalyResult(
            is_anomaly=is_anomaly,
            score=score,
            method="rate_of_change",
            details=f"Current rate: {current_rate:.4f}, historical mean: {mean_rate:.4f}, std: {std_rate:.4f}",
            timestamp=now_utc(),
        )

    def detect_trend_anomaly(self, current_value: float, points: list[MetricPoint]) -> AnomalyResult:
        """Detect anomalies based on trend analysis.

        Uses linear regression to detect the trend and checks if the current
        value is anomalous relative to the trend prediction.
        """
        if len(points) < 5:
            return AnomalyResult(
                is_anomaly=False,
                score=0.0,
                method="trend",
                details="Insufficient data for trend analysis",
                timestamp=now_utc(),
            )

        window = points[-100:]  # Last 100 points
        trend = self._linear_regression([p.value for p in window])

        # Predict using trend
        n = len(window)
        predicted = trend.slope * (n - 1) + trend.intercept
        deviation = abs(current_value - predicted)

        # Simple score based on deviation relative to data range
        data_range = max(window, key=lambda p: p.value).value - min(window, key=lambda p: p.value).value
        score = deviation / data_range if data_range > 0 else 0.0

        is_anomaly = score > 1.5 and trend.is_significant

        return AnomalyResult(
            is_anomaly=is_anomaly,
            score=score,
            method="trend",
            details=f"Trend: {trend.direction} (slope: {trend.slope:.4f}, R²: {trend.r_squared:.4f}), deviation: {deviation:.2f}",
            timestamp=now_utc(),
        )

    def analyze_trend(self, points: list[MetricPoint]) -> TrendResult:
        """Analyze the trend of metric data using linear regression.

        Returns:
            TrendResult with direction, slope, R², and significance.
        """
        if len(points) < 3:
            return TrendResult(
                direction="stable",
                slope=0.0,
                intercept=0.0,
                r_squared=0.0,
                is_significant=False,
            )

        values = [p.value for p in points[-100:]]
        trend = self._linear_regression(values)
        return trend

    def predict_value(
        self,
        points: list[MetricPoint],
        horizon_minutes: int = 60,
    ) -> Optional[PredictedValue]:
        """Predict future metric value using linear regression extrapolation.

        Args:
            points: Historical metric data points.
            horizon_minutes: Prediction horizon in minutes.

        Returns:
            PredictedValue with prediction and confidence interval, or None if insufficient data.
        """
        if len(points) < 5:
            return None

        values = [p.value for p in points[-100:]]
        trend = self._linear_regression(values)

        # Calculate average time delta between points
        time_deltas = []
        for i in range(1, len(points)):
            dt = (points[i].timestamp - points[i - 1].timestamp).total_seconds() / 60.0  # minutes
            if dt > 0:
                time_deltas.append(dt)

        avg_delta = sum(time_deltas) / len(time_deltas) if time_deltas else 1.0
        steps_ahead = horizon_minutes / avg_delta if avg_delta > 0 else horizon_minutes

        # Predicted value
        n = len(values)
        predicted = trend.slope * (n + steps_ahead) + trend.intercept

        # Confidence interval (wider for further predictions)
        residuals = [values[i] - (trend.slope * i + trend.intercept) for i in range(n)]
        std_residuals = math.sqrt(sum(r ** 2 for r in residuals) / n) if n > 0 else 0.0
        confidence_width = std_residuals * (1 + steps_ahead * 0.1)

        return PredictedValue(
            predicted_value=predicted,
            confidence_lower=predicted - confidence_width,
            confidence_upper=predicted + confidence_width,
            horizon_minutes=horizon_minutes,
        )

    # ── Rule-driven evaluators (canonical home for anomaly logic) ────────
    # Each returns an AlertCreate when the rule's anomaly condition fires,
    # or None otherwise. Configuration comes from the rule, not from the
    # AnomalyDetector instance defaults.

    def evaluate_z_score_rule(
        self,
        rule: AlarmRule,
        current_value: float,
        metric_points: Optional[list[MetricPoint]],
        config: ZScoreConfig,
    ) -> Optional[AlertCreate]:
        if not metric_points:
            return None
        # Baseline excludes metric_points[-1], the value under test.
        baseline = _filter_by_window(metric_points[:-1], config.window_minutes)
        if len(baseline) < config.min_data_points:
            return None
        values = [p.value for p in baseline]
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0
        std_dev = math.sqrt(variance)
        if std_dev < 1e-10:
            return None
        z_score = abs(current_value - mean) / std_dev
        if z_score <= config.threshold:
            return None
        logger.info(
            "anomaly fired (z_score): rule=%s metric=%s/%s value=%.4f z=%.2f threshold=%.2f mean=%.2f std=%.2f n=%d",
            rule.name, rule.metric_source, rule.metric_name,
            current_value, z_score, config.threshold, mean, std_dev, n,
        )
        return AlertCreate(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            metric_source=rule.metric_source,
            metric_name=rule.metric_name,
            current_value=current_value,
            threshold_value=round(z_score, 4),
            severity=_severity_str(rule),
            message=(
                f"Z-score {z_score:.2f} exceeds threshold "
                f"{config.threshold} (mean={mean:.2f}, std={std_dev:.2f})"
            ),
            source_host=_host_from_points(metric_points, rule),
        )

    def evaluate_moving_average_rule(
        self,
        rule: AlarmRule,
        current_value: float,
        metric_points: Optional[list[MetricPoint]],
        config: MovingAverageConfig,
    ) -> Optional[AlertCreate]:
        if not metric_points:
            return None
        # Baseline excludes metric_points[-1], the value under test.
        baseline = _filter_by_window(metric_points[:-1], config.window_minutes)
        if len(baseline) < config.min_data_points:
            return None
        values = [p.value for p in baseline]
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0
        std_dev = math.sqrt(variance)
        if std_dev < 1e-10:
            return None
        deviation = abs(current_value - mean) / std_dev
        if deviation <= config.deviation_factor:
            return None
        logger.info(
            "anomaly fired (moving_avg): rule=%s metric=%s/%s value=%.4f deviation=%.2fσ factor=%.2f mean=%.2f n=%d",
            rule.name, rule.metric_source, rule.metric_name,
            current_value, deviation, config.deviation_factor, mean, n,
        )
        return AlertCreate(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            metric_source=rule.metric_source,
            metric_name=rule.metric_name,
            current_value=current_value,
            threshold_value=round(mean + config.deviation_factor * std_dev, 4),
            severity=_severity_str(rule),
            message=(
                f"Value {current_value:.2f} deviates {deviation:.2f}σ "
                f"from moving average {mean:.2f}"
            ),
            source_host=_host_from_points(metric_points, rule),
        )

    def evaluate_percentile_rule(
        self,
        rule: AlarmRule,
        current_value: float,
        metric_points: Optional[list[MetricPoint]],
        config: PercentileConfig,
    ) -> Optional[AlertCreate]:
        if not metric_points:
            return None
        # Baseline excludes metric_points[-1], the value under test.
        baseline = _filter_by_window(metric_points[:-1], config.window_minutes)
        if len(baseline) < config.min_data_points:
            return None
        values = sorted(p.value for p in baseline)
        n = len(values)
        idx = int(config.percentile / 100 * (n - 1))
        percentile_value = values[min(idx, n - 1)]
        if current_value <= percentile_value:
            return None
        logger.info(
            "anomaly fired (percentile): rule=%s metric=%s/%s value=%.4f p%.0f=%.4f n=%d",
            rule.name, rule.metric_source, rule.metric_name,
            current_value, config.percentile, percentile_value, n,
        )
        return AlertCreate(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            metric_source=rule.metric_source,
            metric_name=rule.metric_name,
            current_value=current_value,
            threshold_value=round(percentile_value, 4),
            severity=_severity_str(rule),
            message=(
                f"Value {current_value:.2f} exceeds "
                f"{config.percentile}th percentile ({percentile_value:.2f})"
            ),
            source_host=_host_from_points(metric_points, rule),
        )

    def evaluate_rate_of_change_rule(
        self,
        rule: AlarmRule,
        current_value: float,
        metric_points: Optional[list[MetricPoint]],
        config: RateOfChangeConfig,
    ) -> Optional[AlertCreate]:
        if not metric_points or len(metric_points) < 2:
            return None
        recent = _filter_by_window(metric_points, config.window_minutes)
        if len(recent) < config.min_data_points:
            return None
        oldest = recent[0]
        latest = recent[-1]
        time_diff_seconds = (latest.timestamp - oldest.timestamp).total_seconds()
        if time_diff_seconds < 1e-10:
            return None
        change_per_minute = ((latest.value - oldest.value) / time_diff_seconds) * 60.0
        if abs(change_per_minute) <= config.max_change_per_minute:
            return None
        direction = "increasing" if change_per_minute > 0 else "decreasing"
        logger.info(
            "anomaly fired (rate_of_change): rule=%s metric=%s/%s rate=%.4f/min direction=%s threshold=%.4f/min",
            rule.name, rule.metric_source, rule.metric_name,
            change_per_minute, direction, config.max_change_per_minute,
        )
        return AlertCreate(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            metric_source=rule.metric_source,
            metric_name=rule.metric_name,
            current_value=current_value,
            threshold_value=round(config.max_change_per_minute, 6),
            severity=_severity_str(rule),
            message=(
                f"Value is {direction} at {change_per_minute:.4f}/min, "
                f"exceeding {config.max_change_per_minute}/min"
            ),
            source_host=_host_from_points(metric_points, rule),
        )

    def _linear_regression(self, values: list[float]) -> TrendResult:
        """Simple linear regression.

        Returns TrendResult with slope, intercept, R², and direction.
        """
        n = len(values)
        if n < 2:
            return TrendResult(direction="stable", slope=0.0, intercept=0.0, r_squared=0.0, is_significant=False)

        x_values = list(range(n))
        x_mean = sum(x_values) / n
        y_mean = sum(values) / n

        ss_xy = sum((x_values[i] - x_mean) * (values[i] - y_mean) for i in range(n))
        ss_xx = sum((x - x_mean) ** 2 for x in x_values)
        ss_yy = sum((y - y_mean) ** 2 for y in values)

        slope = ss_xy / ss_xx if ss_xx > 0 else 0.0
        intercept = y_mean - slope * x_mean

        # R-squared
        ss_res = sum((values[i] - (slope * x_values[i] + intercept)) ** 2 for i in range(n))
        r_squared = 1 - (ss_res / ss_yy) if ss_yy > 0 else 0.0

        # Direction
        if abs(slope) < 1e-10:
            direction = "stable"
        elif slope > 0:
            direction = "increasing"
        else:
            direction = "decreasing"

        # Significance (R² > 0.5 and enough data)
        is_significant = r_squared > 0.5 and n >= 5

        return TrendResult(
            direction=direction,
            slope=slope,
            intercept=intercept,
            r_squared=r_squared,
            is_significant=is_significant,
        )