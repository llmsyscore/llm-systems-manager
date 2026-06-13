"""Alert data models."""

from datetime import datetime
from .._time import now_utc
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class AlertStatus(str, Enum):
    """Alert lifecycle statuses."""
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    CLOSED = "closed"
    IGNORED = "ignored"
    EXCEPTION = "exception"


class AlertCreate(BaseModel):
    """Schema for creating an alert (manually or programmatically)."""
    rule_id: Optional[UUID] = Field(default=None, description="Associated rule ID")
    rule_name: Optional[str] = Field(default=None, description="Rule name (for display in notifications)")
    metric_source: str = Field(description="Metric source (gpu, cpu, ram, etc.)")
    metric_name: str = Field(description="Metric name")
    current_value: float = Field(description="Current metric value")
    threshold_value: float = Field(description="Threshold that was breached")
    severity: str = Field(default="warning", description="Alert severity")
    message: str = Field(description="Alert message")
    notification_channel_ids: list[UUID] = Field(default_factory=list, description="Channels to notify")
    source_host: Optional[str] = Field(default=None, description="Server/device hostname that produced the metric")

    def to_alert(self, alert_id: Optional[UUID] = None) -> "Alert":
        """Create an Alert from the create schema."""
        now = now_utc()
        return Alert(
            alert_id=alert_id or uuid4(),
            rule_id=self.rule_id,
            rule_name=self.rule_name,
            metric_source=self.metric_source,
            metric_name=self.metric_name,
            current_value=self.current_value,
            threshold_value=self.threshold_value,
            severity=self.severity,
            status=AlertStatus.ACTIVE,
            message=self.message,
            notification_channel_ids=self.notification_channel_ids,
            created_at=now,
            last_evaluated_at=now,
            trigger_count=1,
            acknowledged_at=None,
            closed_at=None,
            acknowledged_by=None,
            exception_details=None,
            source_host=self.source_host,
        )


class AlertUpdate(BaseModel):
    """Schema for updating an alert."""
    status: Optional[AlertStatus] = None
    acknowledged_by: Optional[str] = None
    exception_details: Optional[str] = None
    new_threshold: Optional[float] = None
    resolution_reason: Optional[str] = None
    resolved_value: Optional[float] = None


class AlertFilter(BaseModel):
    """Filters for querying alerts."""
    severity: Optional[str] = None
    status: Optional[str] = None
    metric_source: Optional[str] = None
    metric_name: Optional[str] = None
    rule_id: Optional[UUID] = None
    limit: int = Field(default=100, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)
    sort_by: str = Field(default="created_at", description="Sort field")
    sort_order: str = Field(default="desc", description="desc or asc")


class Alert(BaseModel):
    """Complete alert with lifecycle tracking."""
    alert_id: UUID
    rule_id: Optional[UUID] = None
    rule_name: Optional[str] = None
    metric_source: str
    metric_name: str
    current_value: float
    threshold_value: float
    severity: str
    status: AlertStatus
    message: str
    notification_channel_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime
    last_evaluated_at: Optional[datetime] = None
    trigger_count: int = 1
    acknowledged_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    exception_details: Optional[str] = None
    source_host: Optional[str] = None

    # Resolution metadata (set when the alert transitions to CLOSED).
    # resolution_reason: "auto" (hysteresis threshold cleared), "manual"
    # (operator clicked Close), or None for legacy alerts closed before
    # the field was added.
    resolution_reason: Optional[str] = None
    # resolved_value: the metric value observed at the moment of resolution
    # so the UI can show "cleared at 72.3%".
    resolved_value: Optional[float] = None

    def to_dict(self) -> dict:
        """Convert to plain dictionary for InfluxDB storage."""
        return {
            "alert_id": str(self.alert_id),
            "rule_id": str(self.rule_id) if self.rule_id else None,
            "rule_name": self.rule_name,
            "metric_source": self.metric_source,
            "metric_name": self.metric_name,
            "current_value": self.current_value,
            "threshold_value": self.threshold_value,
            "severity": self.severity,
            "status": self.status.value if isinstance(self.status, AlertStatus) else self.status,
            "message": self.message,
            "notification_channel_ids": [str(id) for id in self.notification_channel_ids],
            "created_at": self.created_at.isoformat(),
            "last_evaluated_at": self.last_evaluated_at.isoformat() if self.last_evaluated_at else None,
            "trigger_count": self.trigger_count,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "acknowledged_by": self.acknowledged_by,
            "exception_details": self.exception_details,
            "source_host": self.source_host,
            "resolution_reason": self.resolution_reason,
            "resolved_value": self.resolved_value,
        }

    @property
    def is_ongoing(self) -> bool:
        """Check if the alert is still active or acknowledged."""
        return self.status in (AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED)