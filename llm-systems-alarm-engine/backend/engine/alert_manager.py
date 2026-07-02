"""Alert manager handles the lifecycle of alerts.

Responsibilities:
- Create new alerts from rule violations
- Update alert status (active -> acknowledged -> closed)
- Manage alert exceptions
- Deduplicate alerts for the same rule
"""

import asyncio
import logging
from typing import Optional
import uuid

from .._time import now_utc
from ..models.alert import (
    Alert,
    AlertCreate,
    AlertStatus,
    AlertUpdate,
)
from ..storage.repositories import RuleRepository, AlertRepository
from config.unified_config import settings

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
        self.ws_broadcast = None

    def _incident_size(self, alert) -> int:
        """Count of ongoing alerts sharing alert's incident_id, min 1."""
        iid = getattr(alert, "incident_id", None)
        if not iid:
            return 1
        try:
            return max(1, sum(1 for a in self.alert_repository.get_active()
                              if getattr(a, "incident_id", None) == iid))
        except Exception:
            return 1

    def _emit_ws_event(self, event: str, alert) -> None:
        """Fire-and-forget alert_* broadcast; no-op without a loop/callback."""
        if self.ws_broadcast is None:
            return
        try:
            payload = alert.to_dict()
            payload["incident_size"] = self._incident_size(alert)
            asyncio.get_running_loop().create_task(self.ws_broadcast(event, payload))
        except RuntimeError:
            logger.debug("ws event %s skipped: no running event loop", event)
        except Exception as e:
            logger.warning("ws event %s emit failed: %s", event, e)

    def _rule_correlation_group(self, rule_id) -> Optional[str]:
        """Look up a rule's correlation_group by id, tolerating fake/legacy repos."""
        if not rule_id:
            return None
        try:
            for r in self.rule_repository.get_all(enabled_only=False):
                if str(r.rule_id) == str(rule_id):
                    return getattr(r, "correlation_group", None) or None
        except Exception as e:
            logger.debug("correlation: rule lookup failed: %s", e)
        return None

    def _assign_incident(self, alert_create: AlertCreate, active: list) -> Optional[str]:
        """Incident to join, or None to self-root: same-host explicit
        correlation_group first, else most-recently-active ongoing alert in window."""
        cfg = getattr(settings.alarm_engine, "correlation", None)
        if not bool(getattr(cfg, "enabled", True)):
            return None
        host = alert_create.source_host or None
        ongoing = [a for a in active
                   if a.is_ongoing and (a.source_host or None) == host]
        if not ongoing:
            return None
        my_group = self._rule_correlation_group(alert_create.rule_id)
        if my_group:
            for a in ongoing:
                if (self._rule_correlation_group(a.rule_id) == my_group
                        and getattr(a, "incident_id", None)):
                    return a.incident_id
        window = float(getattr(cfg, "window_seconds", 60.0) or 60.0)
        now = now_utc()
        for a in sorted(ongoing, key=lambda x: x.last_evaluated_at or x.created_at,
                        reverse=True):
            anchor = a.last_evaluated_at or a.created_at
            if ((now - anchor).total_seconds() <= window
                    and getattr(a, "incident_id", None)):
                return a.incident_id
        return None

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
                alert_create.rule_id,
                getattr(existing_alert, "alert_id", "?"),
                getattr(existing_alert, "trigger_count", 0),
            )
            return None

        # Join an ongoing same-host incident before creating; else self-root.
        incident = self._assign_incident(alert_create, existing)
        if incident:
            alert_create.incident_id = incident

        # Create new alert (repository handles AlertCreate -> Alert conversion)
        alert = self.alert_repository.create(alert_create)
        logger.info(
            "ALERT CREATED: id=%s rule=%s host=%s metric=%s/%s value=%s threshold=%s severity=%s incident=%s",
            alert.alert_id, alert.rule_name or "—",
            alert.source_host or "—",
            alert.metric_source, alert.metric_name,
            alert.current_value, alert.threshold_value, alert.severity,
            alert.incident_id,
        )
        self._emit_ws_event("alert_created", alert)
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
        if result:
            self._emit_ws_event("alert_acknowledged", result)
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
        if result:
            self._emit_ws_event("alert_closed", result)
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
        if result:
            self._emit_ws_event("alert_ignored", result)
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