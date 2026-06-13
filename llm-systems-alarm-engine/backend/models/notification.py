"""Notification channel data models."""

from datetime import datetime
from .._time import now_utc
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ChannelType(str, Enum):
    """Notification channel types."""
    TOAST = "toast"
    SMS = "sms"
    EMAIL = "email"
    WEBHOOK = "webhook"
    DISCORD = "discord"


# Alias for backwards compatibility
NotificationChannelType = ChannelType


class ToastConfig(BaseModel):
    """Browser toast notification config."""
    enabled: bool = True


class SmsConfig(BaseModel):
    """SMS notification config via Twilio."""
    enabled: bool = True
    to_number: str = Field(description="Phone number to send to")


class EmailConfig(BaseModel):
    """Email notification config via SMTP."""
    enabled: bool = True
    to_email: str = Field(description="Email address to send to")
    subject_prefix: str = Field(default="[ALARM]", description="Prefix for email subject")


class WebhookConfig(BaseModel):
    """HTTP webhook notification config."""
    enabled: bool = True
    url: str = Field(description="Webhook URL")
    method: str = Field(default="POST", description="HTTP method")
    headers: dict[str, str] = Field(default_factory=dict, description="Custom headers")
    secret: Optional[str] = Field(default=None, description="Secret for signing requests")


class DiscordConfig(BaseModel):
    """Discord webhook notification config."""
    enabled: bool = True
    webhook_url: str = Field(description="Discord webhook URL")
    username: Optional[str] = Field(default=None, description="Override bot username")
    avatar_url: Optional[str] = Field(default=None, description="Override bot avatar")


class ChannelSpecificConfig(BaseModel):
    """Channel-specific configuration."""
    toast: Optional[ToastConfig] = None
    sms: Optional[SmsConfig] = None
    email: Optional[EmailConfig] = None
    webhook: Optional[WebhookConfig] = None
    discord: Optional[DiscordConfig] = None


class NotificationChannelCreate(BaseModel):
    """Schema for creating a notification channel."""
    name: str = Field(min_length=1, max_length=255, description="Channel name")
    description: Optional[str] = Field(default=None, max_length=1000)
    channel_type: ChannelType = Field(description="Channel type")
    config: ChannelSpecificConfig = Field(description="Channel-specific configuration")
    enabled: bool = Field(default=True, description="Whether the channel is enabled")
    rule_ids: list[UUID] = Field(default_factory=list, description="Rules to send to this channel")

    def to_channel(self, channel_id: Optional[UUID] = None) -> "NotificationChannel":
        """Create a NotificationChannel from the create schema."""
        return NotificationChannel(
            channel_id=channel_id or uuid4(),
            name=self.name,
            description=self.description,
            channel_type=self.channel_type,
            config=self.config,
            enabled=self.enabled,
            rule_ids=self.rule_ids,
            created_at=now_utc(),
            last_sent_at=None,
            send_count=0,
            fail_count=0,
        )


class NotificationChannelUpdate(BaseModel):
    """Schema for updating a notification channel."""
    name: Optional[str] = None
    description: Optional[str] = None
    channel_type: Optional[ChannelType] = None
    config: Optional[ChannelSpecificConfig] = None
    enabled: Optional[bool] = None
    rule_ids: Optional[list[UUID]] = None


class NotificationChannel(BaseModel):
    """Complete notification channel with metadata."""
    channel_id: UUID
    name: str
    description: Optional[str]
    channel_type: ChannelType
    config: ChannelSpecificConfig
    enabled: bool
    rule_ids: list[UUID]
    created_at: datetime
    last_sent_at: Optional[datetime]
    send_count: int
    fail_count: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to plain dictionary for InfluxDB storage."""
        return {
            "channel_id": str(self.channel_id),
            "name": self.name,
            "description": self.description,
            "channel_type": self.channel_type.value,
            "config": self.config.model_dump(),
            "enabled": self.enabled,
            "rule_ids": [str(id) for id in self.rule_ids],
            "created_at": self.created_at.isoformat(),
            "last_sent_at": self.last_sent_at.isoformat() if self.last_sent_at else None,
            "send_count": self.send_count,
            "fail_count": self.fail_count,
        }


class NotificationPayload(BaseModel):
    """Payload sent to a notification channel."""
    channel_id: UUID
    channel_type: ChannelType
    alert_message: str
    alert_severity: str
    alert_metric_source: str
    alert_metric_name: str
    alert_current_value: float
    alert_threshold_value: float
    rule_name: Optional[str] = None


class NotificationMethod(str, Enum):
    """Notification delivery methods."""
    CHANNEL = "channel"
    DIRECT = "direct"


# Severity rank used by min_severity filtering. Ordered low → high.
# Alerts at a level below the policy's min_severity are skipped.
SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}


class NotificationConfigCreate(BaseModel):
    """Schema for creating a notification config (a.k.a. alarm policy)."""
    name: str = Field(min_length=1, max_length=255, description="Config name")
    description: Optional[str] = Field(default=None, max_length=1000)
    channels: list[UUID] = Field(default_factory=list, description="Channel IDs to use")
    enabled: bool = Field(default=True, description="Whether the config is enabled")
    auto_dismiss: bool = Field(default=True, description="Toast auto-dismisses after timeout; if false, sticky until clicked")

    # Alert filters — all default to permissive (no filter). An alert matches
    # the policy when EVERY filter clause passes (logical AND).
    min_severity: Optional[str] = Field(
        default=None,
        description="Minimum severity to route through this policy (info|warning|critical). None = no filter.",
    )
    metric_sources: list[str] = Field(
        default_factory=list,
        description="Whitelist of alert.metric_source values. Empty = match all sources.",
    )
    metric_names: list[str] = Field(
        default_factory=list,
        description="Whitelist of alert.metric_name values. Empty = match all metric names.",
    )
    source_hosts: list[str] = Field(
        default_factory=list,
        description="Whitelist of alert.source_host values. Empty = match all hosts.",
    )

    # Delivery cadence / clear-event knobs.
    repeat_interval_minutes: int = Field(
        default=30, ge=0,
        description="Minimum minutes between consecutive notifications for the same alert. 0 = no rate limit.",
    )
    notify_on_clear: bool = Field(
        default=False,
        description="If true, send a notification when a previously-fired alert resolves.",
    )
    min_alarm_count: int = Field(
        default=1, ge=1,
        description="Number of consecutive rule firings required before the policy dispatches. 1 = fire on first breach.",
    )


class NotificationConfigUpdate(BaseModel):
    """Schema for updating a notification config."""
    name: Optional[str] = None
    description: Optional[str] = None
    channels: Optional[list[UUID]] = None
    enabled: Optional[bool] = None
    auto_dismiss: Optional[bool] = None
    min_severity: Optional[str] = None
    metric_sources: Optional[list[str]] = None
    metric_names: Optional[list[str]] = None
    source_hosts: Optional[list[str]] = None
    repeat_interval_minutes: Optional[int] = Field(default=None, ge=0)
    notify_on_clear: Optional[bool] = None
    min_alarm_count: Optional[int] = Field(default=None, ge=1)


class NotificationConfig(BaseModel):
    """Complete notification config with metadata."""
    config_id: UUID
    name: str
    description: Optional[str]
    channels: list[UUID]
    enabled: bool
    auto_dismiss: bool = True
    created_at: datetime
    last_triggered_at: Optional[datetime]
    trigger_count: int

    # Policy filters — same semantics as NotificationConfigCreate.
    min_severity: Optional[str] = None
    metric_sources: list[str] = Field(default_factory=list)
    metric_names: list[str] = Field(default_factory=list)
    source_hosts: list[str] = Field(default_factory=list)

    # Delivery cadence / clear-event knobs.
    repeat_interval_minutes: int = 30
    notify_on_clear: bool = False
    min_alarm_count: int = 1

    def matches_alert(self, alert: Any) -> bool:
        """Return True if this policy's filters all pass for the given alert.

        An empty list filter is permissive (matches anything). min_severity
        of None is also permissive. Filters are AND-joined.
        """
        # Severity floor.
        if self.min_severity:
            floor = SEVERITY_RANK.get(self.min_severity.lower())
            level = SEVERITY_RANK.get(str(getattr(alert, "severity", "")).lower())
            if floor is not None and (level is None or level < floor):
                return False
        # Whitelist filters: present-and-non-empty means restrictive.
        if self.metric_sources:
            if str(getattr(alert, "metric_source", "")) not in self.metric_sources:
                return False
        if self.metric_names:
            if str(getattr(alert, "metric_name", "")) not in self.metric_names:
                return False
        if self.source_hosts:
            host = str(getattr(alert, "source_host", "") or "")
            if host not in self.source_hosts:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Convert to plain dictionary for storage."""
        return {
            "config_id": str(self.config_id),
            "name": self.name,
            "description": self.description,
            "channels": [str(id) for id in self.channels],
            "enabled": self.enabled,
            "auto_dismiss": self.auto_dismiss,
            "created_at": self.created_at.isoformat(),
            "last_triggered_at": self.last_triggered_at.isoformat() if self.last_triggered_at else None,
            "trigger_count": self.trigger_count,
            "min_severity": self.min_severity,
            "metric_sources": list(self.metric_sources),
            "metric_names": list(self.metric_names),
            "source_hosts": list(self.source_hosts),
            "repeat_interval_minutes": int(self.repeat_interval_minutes),
            "notify_on_clear": bool(self.notify_on_clear),
            "min_alarm_count": int(self.min_alarm_count),
        }


class NotificationDelivery(BaseModel):
    """Record of a notification delivery attempt."""
    delivery_id: UUID
    config_id: Optional[UUID]
    channel_id: Optional[UUID]
    channel_type: ChannelType
    method: NotificationMethod
    recipient: str
    title: str
    body: str
    severity: str
    success: bool
    error_message: Optional[str]
    delivered_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
