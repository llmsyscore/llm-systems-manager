"""Threshold-based rule evaluation engine.

Threshold rules (above/below/range) are handled here. Anomaly rule types
(z_score, moving_average, percentile, rate_of_change) are delegated to
:class:`AnomalyDetector`, which is the canonical home for that logic.
"""

import logging
from datetime import datetime
from .._time import now_utc
from typing import Optional

from ..models.alarm_rule import (
    AlarmRule,
    MovingAverageConfig,
    PercentileConfig,
    RateOfChangeConfig,
    RuleType,
    ThresholdConfig,
    ZScoreConfig,
)
from ..models.alert import AlertCreate
from ..models.metrics import MetricPoint
from .anomaly_detector import AnomalyDetector

logger = logging.getLogger(__name__)


class ThresholdEvaluator:
    """Evaluates metric values against alarm rules.

    Threshold rule types are handled here. Anomaly rule types are routed to
    the injected :class:`AnomalyDetector`.
    """

    def __init__(
        self,
        alert_repository=None,
        metric_repository=None,
        anomaly_detector: Optional[AnomalyDetector] = None,
    ):
        self.alert_repository = alert_repository
        self.metric_repository = metric_repository
        self.anomaly_detector = anomaly_detector or AnomalyDetector()

    def evaluate_rule(
        self,
        rule: AlarmRule,
        current_value: float,
        metric_points: Optional[list[MetricPoint]] = None,
    ) -> Optional[AlertCreate]:
        """Evaluate a single rule against the current metric value.

        Returns an AlertCreate if a violation is detected, None otherwise.
        """
        if not rule.enabled:
            return None
        if self._is_in_quiet_hours(rule):
            return None

        rule_type = rule.rule_type
        rs_config = rule.config

        # Hostname of the most recent metric point — flows into AlertCreate
        # so toasts/dashboards/alerts table can show which device triggered it.
        # Fall back to rule.source_host (the configured device for this rule)
        # so alerts are attributed correctly even if the latest cached point
        # somehow lacks the hostname tag.
        host = (
            metric_points[-1].hostname
            if metric_points and getattr(metric_points[-1], "hostname", None)
            else getattr(rule, "source_host", None)
        )

        if rule_type == RuleType.THRESHOLD_ABOVE:
            return self._evaluate_threshold_above(rule, current_value, rs_config.threshold or ThresholdConfig(), host)
        if rule_type == RuleType.THRESHOLD_BELOW:
            return self._evaluate_threshold_below(rule, current_value, rs_config.threshold or ThresholdConfig(), host)
        if rule_type == RuleType.THRESHOLD_RANGE:
            return self._evaluate_threshold_range(rule, current_value, rs_config.threshold or ThresholdConfig(), host)
        if rule_type == RuleType.Z_SCORE:
            return self.anomaly_detector.evaluate_z_score_rule(
                rule, current_value, metric_points, rs_config.z_score or ZScoreConfig()
            )
        if rule_type == RuleType.MOVING_AVERAGE:
            return self.anomaly_detector.evaluate_moving_average_rule(
                rule, current_value, metric_points, rs_config.moving_average or MovingAverageConfig()
            )
        if rule_type == RuleType.PERCENTILE:
            return self.anomaly_detector.evaluate_percentile_rule(
                rule, current_value, metric_points, rs_config.percentile or PercentileConfig()
            )
        if rule_type == RuleType.RATE_OF_CHANGE:
            return self.anomaly_detector.evaluate_rate_of_change_rule(
                rule, current_value, metric_points, rs_config.rate_of_change or RateOfChangeConfig()
            )

        logger.warning(f"Unknown rule type: {rule_type}")
        return None

    @staticmethod
    def _resolve_upper(config: ThresholdConfig) -> Optional[float]:
        for v in (config.upper, config.value, config.critical, config.warning):
            if v is not None:
                return v
        return None

    @staticmethod
    def _resolve_lower(config: ThresholdConfig) -> Optional[float]:
        for v in (config.lower, config.value, config.warning, config.critical):
            if v is not None:
                return v
        return None

    @staticmethod
    def _severity_str(rule: AlarmRule) -> str:
        return rule.severity.value if hasattr(rule.severity, "value") else str(rule.severity)

    @staticmethod
    def _fmt(value: float, unit: Optional[str]) -> str:
        """Format a numeric value to 2 decimals with optional unit suffix.

        Avoids the "Value 97.97702056188925" UX wart and surfaces units inline
        (e.g. "97.97%") so alert messages are self-describing.
        """
        u = (unit or "").strip()
        # Percent and degree units sit flush against the number; everything
        # else gets a leading space (e.g. "12.34 Mbps").
        sep = "" if u in ("", "%", "°C", "°F") else " "
        return f"{round(float(value), 2)}{sep}{u}"

    def _evaluate_threshold_above(
        self, rule: AlarmRule, value: float, config: ThresholdConfig, host: Optional[str] = None
    ) -> Optional[AlertCreate]:
        threshold = self._resolve_upper(config)
        if threshold is None or value <= threshold:
            return None
        logger.info(
            "threshold breach (above): rule=%s metric=%s/%s value=%s threshold=%s severity=%s host=%s",
            rule.name, rule.metric_source, rule.metric_name,
            self._fmt(value, config.unit), self._fmt(threshold, config.unit),
            self._severity_str(rule), host,
        )
        return AlertCreate(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            metric_source=rule.metric_source,
            metric_name=rule.metric_name,
            current_value=value,
            threshold_value=threshold,
            severity=self._severity_str(rule),
            message=f"Value {self._fmt(value, config.unit)} exceeds threshold {self._fmt(threshold, config.unit)}",
            source_host=host,
        )

    def _evaluate_threshold_below(
        self, rule: AlarmRule, value: float, config: ThresholdConfig, host: Optional[str] = None
    ) -> Optional[AlertCreate]:
        threshold = self._resolve_lower(config)
        if threshold is None or value >= threshold:
            return None
        logger.info(
            "threshold breach (below): rule=%s metric=%s/%s value=%s threshold=%s severity=%s host=%s",
            rule.name, rule.metric_source, rule.metric_name,
            self._fmt(value, config.unit), self._fmt(threshold, config.unit),
            self._severity_str(rule), host,
        )
        return AlertCreate(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            metric_source=rule.metric_source,
            metric_name=rule.metric_name,
            current_value=value,
            threshold_value=threshold,
            severity=self._severity_str(rule),
            message=f"Value {self._fmt(value, config.unit)} falls below threshold {self._fmt(threshold, config.unit)}",
            source_host=host,
        )

    def _evaluate_threshold_range(
        self, rule: AlarmRule, value: float, config: ThresholdConfig, host: Optional[str] = None
    ) -> Optional[AlertCreate]:
        if config.lower is None or config.upper is None:
            return None
        if config.lower <= value <= config.upper:
            return None
        logger.info(
            "threshold breach (range): rule=%s metric=%s/%s value=%s range=[%s, %s] severity=%s host=%s",
            rule.name, rule.metric_source, rule.metric_name,
            self._fmt(value, config.unit),
            self._fmt(config.lower, config.unit), self._fmt(config.upper, config.unit),
            self._severity_str(rule), host,
        )
        return AlertCreate(
            rule_id=rule.rule_id,
            rule_name=rule.name,
            metric_source=rule.metric_source,
            metric_name=rule.metric_name,
            current_value=value,
            threshold_value=config.lower if value < config.lower else config.upper,
            severity=self._severity_str(rule),
            message=f"Value {self._fmt(value, config.unit)} is outside range [{self._fmt(config.lower, config.unit)}, {self._fmt(config.upper, config.unit)}]",
            source_host=host,
        )

    def _is_in_quiet_hours(self, rule: AlarmRule) -> bool:
        if not rule.quiet_hours_start or not rule.quiet_hours_end:
            return False
        try:
            now = now_utc().time()
            start = datetime.strptime(rule.quiet_hours_start, "%H:%M").time()
            end = datetime.strptime(rule.quiet_hours_end, "%H:%M").time()
        except (ValueError, TypeError):
            return False
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end
