"""Alarm rule data models."""

import re
from datetime import datetime
from .._time import now_utc
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

# Consecutive sub-threshold eval cycles before an alert auto-resolves.
DEFAULT_AUTO_RESOLVE_CYCLES = 2

# Allowed charset for metric_source / metric_name — the same alphabet the
# metrics read API enforces on its source/metric_name params.
TAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:\- ]{1,128}\Z")


def _validate_metric_tag(value: str) -> str:
    if not TAG_VALUE_RE.match(value):
        raise ValueError("must be 1-128 chars of [A-Za-z0-9_.:- ] and space")
    return value


class RuleType(str, Enum):
    """Types of alarm rules."""
    THRESHOLD_ABOVE = "threshold_above"
    THRESHOLD_BELOW = "threshold_below"
    THRESHOLD_RANGE = "threshold_range"
    RATE_OF_CHANGE = "rate_of_change"
    Z_SCORE = "z_score"
    MOVING_AVERAGE = "moving_average"
    PERCENTILE = "percentile"


class Severity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ThresholdConfig(BaseModel):
    """Threshold configuration for a rule.

    Supports both single-threshold (above/below) and range rules:
      - threshold_above: alert when value > upper (or value, or critical)
      - threshold_below: alert when value < lower (or value, or warning)
      - threshold_range: alert when value < lower or value > upper
    """
    value: Optional[float] = Field(default=None, description="Single threshold value")
    lower: Optional[float] = Field(default=None, description="Lower bound for range / below")
    upper: Optional[float] = Field(default=None, description="Upper bound for range / above")
    warning: Optional[float] = Field(default=None, description="Warning-level threshold")
    critical: Optional[float] = Field(default=None, description="Critical-level threshold")
    unit: Optional[str] = Field(default=None, description="Metric unit (e.g., '°C', '%', 'Mbps')")


class MovingAverageConfig(BaseModel):
    """Moving average configuration."""
    window_minutes: int = Field(default=15, ge=1, description="Window size in minutes")
    deviation_factor: float = Field(default=2.0, ge=0, description="Number of standard deviations for anomaly")
    min_data_points: int = Field(default=5, ge=1, description="Minimum data points for calculation")


class PercentileConfig(BaseModel):
    """Percentile configuration."""
    percentile: float = Field(default=95.0, ge=50, le=99.9, description="Percentile (50-99.9)")
    window_minutes: int = Field(default=60, ge=1, description="Historical window in minutes")
    min_data_points: int = Field(default=10, ge=1, description="Minimum data points for calculation")


class RateOfChangeConfig(BaseModel):
    """Rate of change configuration."""
    max_change_per_minute: float = Field(default=10.0, ge=0, description="Max allowed change per minute")
    window_minutes: int = Field(default=5, ge=1, description="Evaluation window in minutes")
    min_data_points: int = Field(default=2, ge=2, description="Minimum data points for calculation")


class ZScoreConfig(BaseModel):
    """Z-score anomaly detection configuration."""
    threshold: float = Field(default=2.0, ge=0, description="Z-score threshold")
    window_minutes: int = Field(default=60, ge=1, description="Historical window for baseline calculation")
    min_data_points: int = Field(default=10, ge=1, description="Minimum data points for calculation")


class RuleSpecificConfig(BaseModel):
    """Rule-specific configuration that varies by rule type."""
    threshold: Optional[ThresholdConfig] = None
    moving_average: Optional[MovingAverageConfig] = None
    percentile: Optional[PercentileConfig] = None
    rate_of_change: Optional[RateOfChangeConfig] = None
    z_score: Optional[ZScoreConfig] = None


class AlarmRuleCreate(BaseModel):
    """Schema for creating a new alarm rule."""
    name: str = Field(min_length=1, max_length=255, description="Rule name")
    description: Optional[str] = Field(default=None, max_length=1000, description="Rule description")
    source_host: Optional[str] = Field(default=None, description="Host that produces this metric (None = any host)")
    metric_source: str = Field(description="Metric source (gpu, cpu, ram, disk, network, psu)")
    metric_name: str = Field(description="Metric name to monitor")
    rule_type: RuleType = Field(description="Type of rule")
    config: RuleSpecificConfig = Field(description="Rule-specific configuration")
    severity: Severity = Field(default=Severity.WARNING, description="Default severity for this rule")
    enabled: bool = Field(default=True, description="Whether the rule is enabled")
    notification_channel_ids: list[UUID] = Field(default_factory=list, description="Notification channels to use")
    quiet_hours_start: Optional[str] = Field(default=None, description="Quiet hours start (HH:MM)")
    quiet_hours_end: Optional[str] = Field(default=None, description="Quiet hours end (HH:MM)")
    auto_resolve_cycles: int = Field(
        default=DEFAULT_AUTO_RESOLVE_CYCLES,
        ge=0,
        description="Close active alerts once metric stays below threshold for this many "
                    "consecutive eval cycles (0 = never auto-resolve, manual close only)",
    )
    correlation_group: Optional[str] = Field(default=None, description="Group key for correlating alerts across rules")

    @field_validator("metric_source", "metric_name")
    @classmethod
    def _check_metric_tags(cls, v: str) -> str:
        return _validate_metric_tag(v)

    def to_alarm_rule(self, rule_id: Optional[UUID] = None) -> "AlarmRule":
        """Create an AlarmRule from the create schema."""
        return AlarmRule(
            rule_id=rule_id or uuid4(),
            name=self.name,
            description=self.description,
            source_host=self.source_host,
            metric_source=self.metric_source,
            metric_name=self.metric_name,
            rule_type=self.rule_type,
            config=self.config,
            severity=self.severity,
            enabled=self.enabled,
            notification_channel_ids=self.notification_channel_ids,
            quiet_hours_start=self.quiet_hours_start,
            quiet_hours_end=self.quiet_hours_end,
            auto_resolve_cycles=self.auto_resolve_cycles,
            correlation_group=self.correlation_group,
            created_at=now_utc(),
            updated_at=now_utc(),
            last_evaluated_at=None,
            last_alert_at=None,
        )


class AlarmRuleUpdate(BaseModel):
    """Schema for updating an existing alarm rule."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=1000)
    source_host: Optional[str] = None
    metric_source: Optional[str] = None
    metric_name: Optional[str] = None
    rule_type: Optional[RuleType] = None
    config: Optional[RuleSpecificConfig] = None
    severity: Optional[Severity] = None
    enabled: Optional[bool] = None
    notification_channel_ids: Optional[list[UUID]] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    auto_resolve_cycles: Optional[int] = Field(default=None, ge=0)
    correlation_group: Optional[str] = None

    @field_validator("metric_source", "metric_name")
    @classmethod
    def _check_metric_tags(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _validate_metric_tag(v)


class AlarmRule(BaseModel):
    """Complete alarm rule with all metadata."""
    rule_id: UUID
    name: str
    description: Optional[str]
    source_host: Optional[str] = None
    metric_source: str
    metric_name: str
    rule_type: RuleType
    config: RuleSpecificConfig
    severity: Severity
    enabled: bool
    notification_channel_ids: list[UUID]
    quiet_hours_start: Optional[str]
    quiet_hours_end: Optional[str]
    auto_resolve_cycles: int = DEFAULT_AUTO_RESOLVE_CYCLES
    correlation_group: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_evaluated_at: Optional[datetime]
    last_alert_at: Optional[datetime]

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary (for InfluxDB storage)."""
        return {
            "rule_id": str(self.rule_id),
            "name": self.name,
            "description": self.description,
            "source_host": self.source_host,
            "metric_source": self.metric_source,
            "metric_name": self.metric_name,
            "rule_type": self.rule_type.value,
            "config": self.config.model_dump(),
            "severity": self.severity.value,
            "enabled": self.enabled,
            "notification_channel_ids": [str(id) for id in self.notification_channel_ids],
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "auto_resolve_cycles": self.auto_resolve_cycles,
            "correlation_group": self.correlation_group,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_evaluated_at": self.last_evaluated_at.isoformat() if self.last_evaluated_at else None,
            "last_alert_at": self.last_alert_at.isoformat() if self.last_alert_at else None,
        }