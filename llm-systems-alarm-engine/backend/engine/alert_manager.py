"""Alert manager handles the lifecycle of alerts.

Responsibilities:
- Create new alerts from rule violations
- Update alert status (active -> acknowledged -> closed)
- Manage alert exceptions
- Deduplicate alerts for the same rule
"""

import logging
from typing import Optional
import uuid

from ..models.alert import (
    Alert,
    AlertCreate,
    AlertStatus,
    AlertUpdate,
)
from ..storage.repositories import RuleRepository, AlertRepository

logger = logging.getLogger(__name__)


class AlertManager:
    """Manages alert lifecycle."""

    def __init__(
        self,
        alert_repository: AlertRepository,
        rule_repository: RuleRepository,
    ):
        self.alert_repository = alert_repository
        self.rule_repository = rule_repository

    def process_alert(self, alert_create: AlertCreate) -> Optional[Alert]:
        """Process a new alert - create or deduplicate.

        Returns the created Alert, or None if deduplicated.
        """
        # Check for existing active alert on the same rule
        existing = self.alert_repository.get_active()
        matching = [
            a for a in existing
            if str(a.rule_id) == str(alert_create.rule_id)
            and a.status in (AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED)
        ]

        if matching:
            # Refresh the existing alert: bump last_evaluated_at and trigger_count,
            # update current_value to the latest. This keeps the UI "ticking" while
            # the same condition keeps re-firing (e.g. an external system pushing
            # the same InfluxDB/Grafana alert each interval) instead of going stale.
            existing_alert = matching[0]
            try:
                self.alert_repository.refresh(
                    existing_alert,
                    current_value=alert_create.current_value,
                )
            except Exception as e:
                logger.warning(
                    "Failed to refresh existing alert %s: %s", existing_alert.alert_id, e
                )
            logger.debug(
                "Deduplicated alert for rule %s: refreshed existing alert %s "
                "(trigger_count=%d)",
                alert_create.rule_id, existing_alert.alert_id, existing_alert.trigger_count,
            )
            return None

        # Create new alert (repository handles AlertCreate -> Alert conversion)
        alert = self.alert_repository.create(alert_create)
        logger.info(
            "ALERT CREATED: id=%s rule=%s host=%s metric=%s/%s value=%s threshold=%s severity=%s",
            alert.alert_id, alert.rule_name or "—",
            alert.source_host or "—",
            alert.metric_source, alert.metric_name,
            alert.current_value, alert.threshold_value, alert.severity,
        )
        return alert

    def acknowledge_alert(self, alert_id: str) -> Optional[Alert]:
        """Acknowledge an alert."""
        try:
            uid = uuid.UUID(alert_id)
        except ValueError:
            logger.warning(f"Invalid alert ID: {alert_id}")
            return None

        alert = self.alert_repository.get_by_id(uid)
        if alert is None:
            logger.warning(f"Alert not found: {alert_id}")
            return None

        update = AlertUpdate(status=AlertStatus.ACKNOWLEDGED)
        result = self.alert_repository.update(uid, update)
        logger.info(
            "ALERT ACKNOWLEDGED: id=%s rule=%s host=%s",
            alert_id, alert.rule_name or "—", alert.source_host or "—",
        )
        return result

    def close_alert(
        self,
        alert_id: str,
        reason: Optional[str] = None,
        resolved_value: Optional[float] = None,
    ) -> Optional[Alert]:
        """Close (resolve) an alert. reason should be 'auto' (threshold
        recovered) or 'manual' (operator closed); resolved_value is the
        metric value observed at the moment of resolution and shows up in
        the UI's 'cleared' chip."""
        try:
            uid = uuid.UUID(alert_id)
        except ValueError:
            logger.warning(f"Invalid alert ID: {alert_id}")
            return None

        alert = self.alert_repository.get_by_id(uid)
        if alert is None:
            logger.warning(f"Alert not found: {alert_id}")
            return None

        update = AlertUpdate(
            status=AlertStatus.CLOSED,
            resolution_reason=reason,
            resolved_value=resolved_value,
        )
        result = self.alert_repository.update(uid, update)
        logger.info(
            "ALERT CLOSED: id=%s rule=%s host=%s reason=%s value=%s",
            alert_id, alert.rule_name or "—", alert.source_host or "—",
            reason or "—",
            f"{resolved_value:.2f}" if isinstance(resolved_value, (int, float)) else "—",
        )
        return result

    def delete_alert(self, alert_id: str) -> bool:
        """Delete/close an alert (remove entirely)."""
        try:
            uid = uuid.UUID(alert_id)
        except ValueError:
            logger.warning(f"Invalid alert ID: {alert_id}")
            return False

        result = self.alert_repository.delete(uid)
        logger.info(f"Alert deleted: {alert_id}")
        return result

    def ignore_alert(self, alert_id: str) -> Optional[Alert]:
        """Ignore an alert (mark as ignored)."""
        try:
            uid = uuid.UUID(alert_id)
        except ValueError:
            logger.warning(f"Invalid alert ID: {alert_id}")
            return None

        alert = self.alert_repository.get_by_id(uid)
        if alert is None:
            logger.warning(f"Alert not found: {alert_id}")
            return None

        update = AlertUpdate(status=AlertStatus.IGNORED)
        result = self.alert_repository.update(uid, update)
        logger.info(f"Alert ignored: {alert_id}")
        return result

    def mark_as_read(self, alert_id: str) -> Optional[Alert]:
        """Alias for acknowledge_alert."""
        return self.acknowledge_alert(alert_id)

    def get_active_alerts(self) -> list[Alert]:
        """Get all active alerts."""
        return self.alert_repository.get_active()

    def get_alert_stats(self) -> dict:
        """Stats across all alerts (active, ack, closed, ignored). One
        indexed GROUP BY in SQLite; no Python-side row materialization."""
        db = getattr(self.alert_repository, "alarms_db", None)
        if db is None:
            return {"total": 0, "active": 0, "acknowledged": 0, "closed": 0, "ignored": 0, "by_severity": {}}
        agg = db.count_by_status_and_severity()
        by_status = agg["by_status"]
        return {
            "total": agg["total"],
            "active": by_status.get("active", 0),
            "acknowledged": by_status.get("acknowledged", 0),
            "closed": by_status.get("closed", 0),
            "ignored": by_status.get("ignored", 0),
            "by_severity": agg["by_severity"],
        }