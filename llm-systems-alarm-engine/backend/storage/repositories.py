"""Repository layer for CRUD operations on rules, alerts, channels, and metrics."""

import json
import logging
import uuid
from datetime import datetime, timezone
from .._best_effort import best_effort
from .._time import now_utc
from typing import Any, Optional

from ..models.alarm_rule import AlarmRule, AlarmRuleCreate, AlarmRuleUpdate
from ..models.alert import Alert, AlertCreate, AlertFilter, AlertStatus, AlertUpdate
from ..models.metrics import MetricPoint, MetricSummary
from ..models.notification import (
    NotificationChannel,
    NotificationChannelCreate,
    NotificationChannelUpdate,
    NotificationConfig,
    NotificationConfigCreate,
    NotificationConfigUpdate,
    NotificationDelivery,
    NotificationMethod,
)
from .ae_alarms_db import AeAlarmsDB
from .ae_settings_db import AeSettingsDB
from .cache import MetricCache
from .influxdb_client import InfluxDBClient
from config.unified_config import settings

logger = logging.getLogger(__name__)


class ConfigDeserializationError(Exception):
    """A stored notification config row was found but could not be parsed into a model."""


class RuleRepository:
    """Repository for alarm rules."""

    def __init__(self, cache: MetricCache, settings_db: Optional[AeSettingsDB] = None):
        self.cache = cache
        self.settings_db = settings_db
        # Memoize get_all(). The rule-eval cycle calls this every 15s. SQLite
        # reads are sub-ms, but rebuilding AlarmRule objects (and their nested
        # RuleSpecificConfig) is still cheaper to skip. Invalidated on
        # create/update/delete.
        self._all_cache: dict[bool, list[AlarmRule]] = {}
        self._all_cache_ts: dict[bool, float] = {}
        self._all_cache_ttl: float = float(settings.alarm_engine.caches.rule_repo_ttl_s)

    def _invalidate_all_cache(self) -> None:
        self._all_cache.clear()
        self._all_cache_ts.clear()

    def create(self, rule_create: AlarmRuleCreate) -> AlarmRule:
        """Create a new alarm rule."""
        rule = rule_create.to_alarm_rule()
        self._save_rule(rule)
        self.cache.set(f"rule:{rule.rule_id}", rule.to_dict())
        self._invalidate_all_cache()
        return rule

    def get_by_id(self, rule_id: uuid.UUID) -> Optional[AlarmRule]:
        """Get a rule by ID, checking cache first."""
        cached = self.cache.get(f"rule:{rule_id}")
        if cached:
            with best_effort("deserialize cached rule", log=logger):
                return self._dict_to_rule(cached)

        if self.settings_db is None:
            return None

        try:
            rules = self.settings_db.query_rules()
        except Exception as e:
            logger.warning(f"DB rules query failed: {e}")
            return None

        for r in rules:
            if r.get("rule_id") == str(rule_id):
                try:
                    rule = self._dict_to_rule(r)
                    self.cache.set(f"rule:{rule_id}", rule.to_dict())
                    return rule
                except Exception:
                    return None
        return None

    def _iter_cached_rules(self) -> list[AlarmRule]:
        """Yield all AlarmRule objects currently in the cache."""
        rules: list[AlarmRule] = []
        for key in list(self.cache._cache.keys()):
            if not key.startswith("rule:"):
                continue
            data = self.cache.get(key)
            if not isinstance(data, dict):
                continue
            try:
                rules.append(self._dict_to_rule(data))
            except Exception:
                continue
        return rules

    def get_all(
        self,
        enabled_only: bool = False,
        raise_on_error: bool = False,
    ) -> list[AlarmRule]:
        """Get all rules from the in-memory snapshot or settings DB.

        raise_on_error is kept for API stability (called by
        `_seed_default_rules`) but is now a no-op: SQLite reads either succeed
        or raise immediately, so there's no "transient outage" signal to
        propagate.
        """
        if self.settings_db is None:
            rules = self._iter_cached_rules()
            if enabled_only:
                rules = [r for r in rules if r.enabled]
            return rules
        # The rule-eval cycle re-asks every 15s; even sub-ms SQLite reads
        # add up across the repeated AlarmRule rebuilds.
        import time as _t
        ts = self._all_cache_ts.get(enabled_only, 0.0)
        if enabled_only in self._all_cache and (_t.time() - ts) < self._all_cache_ttl:
            return list(self._all_cache[enabled_only])
        rules_data = self.settings_db.query_rules(enabled_only=enabled_only)
        result: list[AlarmRule] = []
        for r in rules_data:
            try:
                result.append(self._dict_to_rule(r))
            except Exception:
                continue
        self._all_cache[enabled_only] = list(result)
        self._all_cache_ts[enabled_only] = _t.time()
        return result

    def get_by_metric(self, source: str, metric_name: str, enabled_only: bool = True) -> list[AlarmRule]:
        """Get all rules for a specific metric."""
        if self.settings_db is None:
            return [
                r for r in self._iter_cached_rules()
                if r.metric_source == source
                and r.metric_name == metric_name
                and (not enabled_only or r.enabled)
            ]
        rules_data = self.settings_db.query_rules(enabled_only=enabled_only)
        out: list[AlarmRule] = []
        for r in rules_data:
            if r.get("metric_source") == source and r.get("metric_name") == metric_name:
                try:
                    out.append(self._dict_to_rule(r))
                except Exception:
                    continue
        return out

    def update(self, rule_id: uuid.UUID, update: AlarmRuleUpdate) -> Optional[AlarmRule]:
        """Update a rule."""
        rule = self.get_by_id(rule_id)
        if not rule:
            return None

        # Iterate model fields directly so typed values (RuleSpecificConfig,
        # enums, UUIDs) are preserved instead of being flattened to plain dicts.
        # Optional fields (source_host, description, quiet_hours_*) must accept
        # None so users can clear them — e.g. switch a rule from a specific
        # device back to "any device". Required fields skip None to avoid
        # corrupting the rule on a partial-update payload.
        NULLABLE_FIELDS = {
            "source_host", "description",
            "quiet_hours_start", "quiet_hours_end",
        }
        for key in update.model_fields_set:
            value = getattr(update, key)
            if value is None and key not in NULLABLE_FIELDS:
                continue
            setattr(rule, key, value)
        rule.updated_at = now_utc()

        self._save_rule(rule)
        self.cache.set(f"rule:{rule_id}", rule.to_dict())
        self._invalidate_all_cache()
        return rule

    def delete(self, rule_id: uuid.UUID) -> bool:
        """Delete a rule from cache AND persistent store."""
        rule = self.get_by_id(rule_id)
        if not rule:
            return False
        self.cache.delete(f"rule:{rule_id}")
        self._invalidate_all_cache()
        if self.settings_db is not None:
            try:
                self.settings_db.delete_rule(str(rule_id))
            except Exception as e:
                logger.error(f"settings_db delete failed for rule {rule_id}: {e}")
                return False
        return True

    def delete_all(self) -> bool:
        """Admin: wipe every rule from cache + persistent store."""
        with best_effort("clear rule cache entries", log=logger):
            for r in self.get_all():
                self.cache.delete(f"rule:{r.rule_id}")
        self._invalidate_all_cache()
        if self.settings_db is not None:
            try:
                return self.settings_db.delete_all_rules()
            except Exception as e:
                logger.error(f"settings_db delete_all_rules failed: {e}")
                return False
        return True

    def _save_rule(self, rule: AlarmRule) -> None:
        """Persist rule to settings DB."""
        if self.settings_db is not None:
            self.settings_db.write_rule(rule.to_dict())

    @staticmethod
    def _dict_to_rule(data: dict[str, Any]) -> AlarmRule:
        """Convert a dictionary to an AlarmRule."""
        config_raw = data.get("config", {})
        if isinstance(config_raw, str):
            try:
                config_raw = json.loads(config_raw)
            except (ValueError, TypeError):
                config_raw = {}
        if not isinstance(config_raw, dict):
            config_raw = {}

        from ..models.alarm_rule import (
            RuleSpecificConfig,
            MovingAverageConfig,
            PercentileConfig,
            RateOfChangeConfig,
            ThresholdConfig,
            ZScoreConfig,
        )

        # Build a RuleSpecificConfig with whichever sub-configs were stored
        rs_kwargs: dict = {}
        if config_raw.get("threshold") is not None:
            rs_kwargs["threshold"] = ThresholdConfig(**config_raw["threshold"])
        if config_raw.get("moving_average") is not None:
            rs_kwargs["moving_average"] = MovingAverageConfig(**config_raw["moving_average"])
        if config_raw.get("percentile") is not None:
            rs_kwargs["percentile"] = PercentileConfig(**config_raw["percentile"])
        if config_raw.get("rate_of_change") is not None:
            rs_kwargs["rate_of_change"] = RateOfChangeConfig(**config_raw["rate_of_change"])
        if config_raw.get("z_score") is not None:
            rs_kwargs["z_score"] = ZScoreConfig(**config_raw["z_score"])

        rs_config = RuleSpecificConfig(**rs_kwargs)

        # Parse notification_channel_ids whether stored as JSON string or list
        ncids_raw = data.get("notification_channel_ids", [])
        if isinstance(ncids_raw, str):
            try:
                ncids_raw = json.loads(ncids_raw)
            except (ValueError, TypeError):
                ncids_raw = []

        def _parse_dt(v: Any) -> Optional[datetime]:
            if v is None:
                return None
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(v)
            except (ValueError, TypeError):
                return None

        rule_id_raw = data["rule_id"]
        return AlarmRule(
            rule_id=uuid.UUID(rule_id_raw) if isinstance(rule_id_raw, str) else rule_id_raw,
            name=data["name"],
            description=data.get("description"),
            source_host=data.get("source_host") or None,
            metric_source=data["metric_source"],
            metric_name=data["metric_name"],
            rule_type=data["rule_type"],
            config=rs_config,
            severity=data["severity"],
            enabled=data.get("enabled", True),
            notification_channel_ids=[uuid.UUID(i) if isinstance(i, str) else i for i in ncids_raw],
            quiet_hours_start=data.get("quiet_hours_start"),
            quiet_hours_end=data.get("quiet_hours_end"),
            auto_resolve_cycles=int(data.get("auto_resolve_cycles", 2) or 0),
            created_at=_parse_dt(data.get("created_at")) or now_utc(),
            updated_at=_parse_dt(data.get("updated_at")) or now_utc(),
            last_evaluated_at=_parse_dt(data.get("last_evaluated_at")),
            last_alert_at=_parse_dt(data.get("last_alert_at")),
        )


class AlertRepository:
    """Repository for alerts.

    Backed by SQLite (`alarms_db` = `ae_alarms.db`). State changes are
    UPDATEs in place; closed alerts stay in the table as history. The
    in-memory `_active_cache` snapshot exists purely to skip the
    SELECT + Pydantic-validation round-trip on every rule-eval cycle
    (called from `RuleEngine.evaluate_all`); SQLite I/O itself is sub-ms.
    Invalidated on every in-process state change, plus a TTL safety net.
    `refresh()` patches the snapshot in place so trigger-count ticks
    don't bust the cache.
    """

    def __init__(self, alarms_db: Optional["AeAlarmsDB"] = None):
        self.alarms_db = alarms_db
        self._active_cache: Optional[list[Alert]] = None
        self._active_cache_ts: float = 0.0
        self._active_cache_ttl: float = float(settings.alarm_engine.caches.rule_repo_ttl_s)

    def _invalidate_active_cache(self) -> None:
        self._active_cache = None
        self._active_cache_ts = 0.0

    def create(self, alert_create: AlertCreate) -> Alert:
        alert = alert_create.to_alert()
        self._save_alert(alert)
        self._invalidate_active_cache()
        return alert

    def get_by_id(self, alert_id: uuid.UUID) -> Optional[Alert]:
        if self.alarms_db is None:
            return None
        row = self.alarms_db.get_alert(str(alert_id))
        if not row:
            return None
        try:
            return self._dict_to_alert(row)
        except Exception as e:
            logger.warning(f"Failed to deserialize alert {alert_id}: {e}")
            return None

    def get_active(self) -> list[Alert]:
        """Active + acknowledged alerts. Cached for the rule-eval hot path."""
        import time as _t
        if self._active_cache is not None and (_t.time() - self._active_cache_ts) < self._active_cache_ttl:
            return self._active_cache
        if self.alarms_db is None:
            self._active_cache = []
            self._active_cache_ts = _t.time()
            return []
        out: list[Alert] = []
        for row in self.alarms_db.query_active():
            try:
                out.append(self._dict_to_alert(row))
            except Exception:
                continue
        self._active_cache = out
        self._active_cache_ts = _t.time()
        return out

    async def list_alerts(
        self,
        limit: int = 100,
        offset: int = 0,
        status: Optional[AlertStatus] = None,
        severity: Optional[str] = None,
        rule_id: Optional[str] = None,
        metric_name: Optional[str] = None,
        only_active: bool = False,
        include_closed: bool = False,
    ) -> list[Alert]:
        """List alerts with optional filtering. Filtering, sorting, and
        pagination all push down to SQLite. `include_closed` selects the
        full table; otherwise live alerts only."""
        if self.alarms_db is None:
            return []
        status_str: Optional[str] = None
        if status is not None:
            status_str = status.value if isinstance(status, AlertStatus) else str(status)
        rows = self.alarms_db.query_filtered(
            live_only=(only_active or not include_closed),
            status=status_str,
            severity=severity,
            rule_id=rule_id,
            metric_name=metric_name,
            limit=limit,
            offset=offset,
        )
        out: list[Alert] = []
        for r in rows:
            try:
                out.append(self._dict_to_alert(r))
            except Exception:
                continue
        return out

    async def get_alert(self, alert_id: str) -> Optional[Alert]:
        try:
            uid = uuid.UUID(alert_id)
        except ValueError:
            return None
        return self.get_by_id(uid)

    def query(self, filters: AlertFilter, include_closed: bool = False) -> list:
        """Query alerts with filters. Returns dicts (the CSV exporter and
        dashboard list consume the dict shape directly)."""
        if self.alarms_db is None:
            return []
        return self.alarms_db.query_filtered(
            live_only=not include_closed,
            status=filters.status,
            severity=filters.severity,
            rule_id=str(filters.rule_id) if filters.rule_id else None,
            metric_source=filters.metric_source,
            metric_name=filters.metric_name,
            sort_by=filters.sort_by,
            sort_desc=(filters.sort_order == "desc"),
            limit=filters.limit,
            offset=filters.offset,
        )

    def update(self, alert_id: uuid.UUID, update: AlertUpdate) -> Optional[Alert]:
        """Update an alert (acknowledge, close, ignore, exception)."""
        existing = self.get_by_id(alert_id)
        if not existing:
            return None
        alert_data = existing.to_dict()

        update_data = update.model_dump(exclude_none=True)
        for key, value in update_data.items():
            if isinstance(value, AlertStatus):
                alert_data[key] = value.value
            else:
                alert_data[key] = value

        if update.status == AlertStatus.ACKNOWLEDGED and alert_data.get("acknowledged_at") is None:
            alert_data["acknowledged_at"] = now_utc().isoformat()
        elif update.status == AlertStatus.CLOSED and alert_data.get("closed_at") is None:
            alert_data["closed_at"] = now_utc().isoformat()

        alert = self._dict_to_alert(alert_data)
        self._save_alert(alert)
        self._invalidate_active_cache()
        return alert

    def refresh(
        self, alert: Alert, current_value: float, evaluated_at: Optional[datetime] = None
    ) -> Alert:
        """Bump current_value, last_evaluated_at, and trigger_count for a
        still-firing alert. One indexed UPDATE per cycle.

        Membership of the active set is unchanged by refresh — we patch the
        snapshot in place instead of invalidating, so the next get_active()
        doesn't pay even a sub-ms SQLite read for no functional change.
        """
        ts = evaluated_at or now_utc()
        alert.current_value = current_value
        alert.last_evaluated_at = ts
        alert.trigger_count = (alert.trigger_count or 1) + 1
        if self.alarms_db is not None:
            try:
                self.alarms_db.bump_refresh(str(alert.alert_id), current_value, when=ts.isoformat())
            except Exception as e:
                logger.warning(f"alarms_db bump_refresh failed: {e}")
        if self._active_cache is not None:
            for i, a in enumerate(self._active_cache):
                if a.alert_id == alert.alert_id:
                    self._active_cache[i] = alert
                    break
        return alert

    def delete(self, alert_id: uuid.UUID) -> bool:
        """Hard-delete an alert."""
        ok = False
        if self.alarms_db is not None:
            try:
                ok = self.alarms_db.delete_alert(str(alert_id))
            except Exception as e:
                logger.warning(f"alarms_db delete_alert failed: {e}")
        self._invalidate_active_cache()
        return ok

    async def delete_alert(self, alert_id: str) -> bool:
        try:
            uid = uuid.UUID(alert_id)
        except ValueError:
            return False
        return self.delete(uid)

    async def get_alert_counters(self) -> dict:
        if self.alarms_db is None:
            return {"by_status": {}, "by_severity": {}, "total": 0}
        return self.alarms_db.count_by_status_and_severity()

    async def close_all_alerts(self) -> int:
        if self.alarms_db is None:
            return 0
        n = self.alarms_db.bulk_update_status(
            from_statuses=(AlertStatus.ACTIVE.value, AlertStatus.ACKNOWLEDGED.value),
            to_status=AlertStatus.CLOSED.value,
            closed_at=now_utc().isoformat(),
        )
        self._invalidate_active_cache()
        return n

    async def ignore_all_alerts(self, duration_hours: int) -> int:
        if self.alarms_db is None:
            return 0
        n = self.alarms_db.bulk_update_status(
            from_statuses=(AlertStatus.ACTIVE.value, AlertStatus.ACKNOWLEDGED.value),
            to_status=AlertStatus.IGNORED.value,
            acknowledged_at=now_utc().isoformat(),
        )
        self._invalidate_active_cache()
        return n

    def _save_alert(self, alert: Alert) -> None:
        """Persist alert to SQLite. Note: callers handle active-cache
        invalidation themselves, because `refresh()` re-saves on every rule
        eval but does NOT change active-set membership — invalidating here
        would defeat the cache."""
        if self.alarms_db is not None:
            self.alarms_db.write_alert(alert.to_dict())

    @staticmethod
    def _dict_to_alert(data: dict[str, Any]) -> Alert:
        """Convert a dictionary to an Alert."""
        # notification_channel_ids may already be a list, or a JSON string
        ncids = data.get("notification_channel_ids", [])
        if isinstance(ncids, str):
            try:
                ncids = json.loads(ncids)
            except (ValueError, TypeError):
                ncids = []

        def _parse_dt(v: Any) -> Optional[datetime]:
            if v is None:
                return None
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(v)
            except (ValueError, TypeError):
                return None

        return Alert(
            alert_id=uuid.UUID(data["alert_id"]) if isinstance(data["alert_id"], str) else data["alert_id"],
            rule_id=uuid.UUID(data["rule_id"]) if data.get("rule_id") and isinstance(data["rule_id"], str) else data.get("rule_id"),
            rule_name=data.get("rule_name"),
            metric_source=data["metric_source"],
            metric_name=data["metric_name"],
            current_value=float(data["current_value"]),
            threshold_value=float(data["threshold_value"]),
            severity=data["severity"],
            status=AlertStatus(data["status"]) if not isinstance(data["status"], AlertStatus) else data["status"],
            message=data.get("message", ""),
            notification_channel_ids=[uuid.UUID(i) if isinstance(i, str) else i for i in ncids],
            created_at=_parse_dt(data.get("created_at")) or now_utc(),
            last_evaluated_at=_parse_dt(data.get("last_evaluated_at")),
            trigger_count=int(data.get("trigger_count") or 1),
            acknowledged_at=_parse_dt(data.get("acknowledged_at")),
            closed_at=_parse_dt(data.get("closed_at")),
            acknowledged_by=data.get("acknowledged_by"),
            exception_details=data.get("exception_details"),
            source_host=data.get("source_host"),
            resolution_reason=data.get("resolution_reason") or None,
            resolved_value=(
                float(data["resolved_value"])
                if data.get("resolved_value") not in (None, "")
                else None
            ),
        )


class NotificationRepository:
    """Repository for notification channels, configs, and deliveries.

    All three persist to SQLite via `settings_db` (ae_notif_rules.db).
    """

    def __init__(
        self,
        cache: MetricCache,
        settings_db: Optional[AeSettingsDB] = None,
    ):
        self.cache = cache
        self.settings_db = settings_db

    # --- Channel CRUD ---

    def create(self, channel_create: NotificationChannelCreate) -> NotificationChannel:
        """Create a new notification channel."""
        channel = channel_create.to_channel()
        self._save_channel(channel)
        self.cache.set(f"channel:{channel.channel_id}", channel.to_dict())
        return channel

    def get_by_id(self, channel_id: uuid.UUID) -> Optional[NotificationChannel]:
        """Get a channel by ID."""
        cached = self.cache.get(f"channel:{channel_id}")
        if cached:
            return self._dict_to_channel(cached)
        if self.settings_db is None:
            return None
        item = self.settings_db.get_channel(str(channel_id))
        if not item:
            return None
        try:
            ch = self._dict_to_channel(item)
            self.cache.set(f"channel:{channel_id}", item)
            return ch
        except Exception:
            return None

    def list_configs(self) -> list[NotificationConfig]:
        """List all notification configs from SQLite."""
        if self.settings_db is None:
            return []
        results: list[NotificationConfig] = []
        for item in self.settings_db.query_configs():
            try:
                results.append(self._dict_to_config(item))
            except Exception:
                continue
        return results

    @staticmethod
    def _dict_to_config(item: dict[str, Any]) -> NotificationConfig:
        """Convert a stored dict into a NotificationConfig."""
        channels_raw = item.get("channels", [])
        if isinstance(channels_raw, str):
            try:
                channels_raw = json.loads(channels_raw)
            except (ValueError, TypeError):
                channels_raw = []
        channels = [uuid.UUID(c) if isinstance(c, str) else c for c in channels_raw]

        def _parse_dt(v: Any) -> Optional[datetime]:
            if v is None:
                return None
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(v)
            except (ValueError, TypeError):
                return None

        cfg_id_raw = item["config_id"]

        def _list_field(name: str) -> list[str]:
            raw = item.get(name)
            if raw is None:
                return []
            if isinstance(raw, str):
                # InfluxDB string column may be JSON-encoded or a single value.
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    return [raw] if raw else []
                return list(parsed) if isinstance(parsed, list) else []
            if isinstance(raw, (list, tuple)):
                return [str(v) for v in raw]
            return []

        # Empty string min_severity (from Influx default) → None so the model
        # treats it as "no filter" rather than as the literal severity "".
        min_sev = item.get("min_severity")
        if isinstance(min_sev, str) and not min_sev.strip():
            min_sev = None

        return NotificationConfig(
            config_id=uuid.UUID(cfg_id_raw) if isinstance(cfg_id_raw, str) else cfg_id_raw,
            name=item["name"],
            description=item.get("description"),
            channels=channels,
            enabled=item.get("enabled", True),
            auto_dismiss=item.get("auto_dismiss", True),
            created_at=_parse_dt(item.get("created_at")) or now_utc(),
            last_triggered_at=_parse_dt(item.get("last_triggered_at")),
            trigger_count=int(item.get("trigger_count", 0)),
            min_severity=min_sev,
            metric_sources=_list_field("metric_sources"),
            metric_names=_list_field("metric_names"),
            source_hosts=_list_field("source_hosts"),
            repeat_interval_minutes=int(item.get("repeat_interval_minutes") or 30),
            notify_on_clear=bool(item.get("notify_on_clear", False)),
            min_alarm_count=int(item.get("min_alarm_count") or 1),
        )

    def create_config(self, config_create: NotificationConfigCreate) -> NotificationConfig:
        """Create a new notification config."""
        from uuid import uuid4
        config = NotificationConfig(
            config_id=uuid4(),
            name=config_create.name,
            description=config_create.description,
            channels=config_create.channels,
            enabled=config_create.enabled,
            auto_dismiss=config_create.auto_dismiss,
            created_at=now_utc(),
            last_triggered_at=None,
            trigger_count=0,
            min_severity=config_create.min_severity,
            metric_sources=list(config_create.metric_sources or []),
            metric_names=list(config_create.metric_names or []),
            source_hosts=list(config_create.source_hosts or []),
            repeat_interval_minutes=int(config_create.repeat_interval_minutes or 30),
            notify_on_clear=bool(config_create.notify_on_clear),
            min_alarm_count=int(config_create.min_alarm_count or 1),
        )
        if self.settings_db:
            self.settings_db.write_config(config.to_dict())
        self.cache.set(f"config:{config.config_id}", config.to_dict())
        return config

    def get_config(self, config_id: uuid.UUID) -> Optional[NotificationConfig]:
        """Get a notification config by ID."""
        cached = self.cache.get(f"config:{config_id}")
        if cached:
            with best_effort("deserialize cached config", log=logger):
                return self._dict_to_config(cached)
        if self.settings_db:
            item = self.settings_db.get_config(str(config_id))
            if item:
                try:
                    result = self._dict_to_config(item)
                    self.cache.set(f"config:{config_id}", result.to_dict())
                    return result
                except Exception as e:
                    logger.warning("get_config: config %s found but failed to deserialize: %s", config_id, e)
                    raise ConfigDeserializationError(str(e)) from e
        return None

    def update_config(self, config_id: uuid.UUID, update: NotificationConfigUpdate) -> Optional[NotificationConfig]:
        """Update a notification config."""
        data = self.cache.get(f"config:{config_id}")
        if not data and self.settings_db:
            data = self.settings_db.get_config(str(config_id))
        if not data:
            return None
        data = dict(data)
        update_data = update.model_dump(exclude_none=True)
        if "channels" in update_data and update_data["channels"] is not None:
            update_data["channels"] = [str(c) for c in update_data["channels"]]
        for key, value in update_data.items():
            data[key] = value
        try:
            config = self._dict_to_config(data)
        except Exception as e:
            logger.warning("update_config: config %s found but produced an invalid model: %s", config_id, e)
            raise ConfigDeserializationError(str(e)) from e
        if self.settings_db:
            self.settings_db.write_config(config.to_dict())
        self.cache.set(f"config:{config_id}", config.to_dict())
        return config

    def delete_config(self, config_id: uuid.UUID) -> bool:
        """Delete a notification config from both cache and SQLite."""
        cache_hit = self.cache.delete(f"config:{config_id}")
        db_hit = False
        if self.settings_db:
            try:
                db_hit = self.settings_db.delete_config(str(config_id))
            except Exception as e:
                logger.warning("Failed to delete config %s from SQLite: %s", config_id, e)
        return cache_hit or db_hit

    def increment_trigger_count(self, config_id: uuid.UUID) -> None:
        """Increment trigger_count and stamp last_triggered_at on the config.

        Persists to SQLite so the counter survives restarts. The cache
        mirror is also updated when present to keep get_config() in sync.
        """
        now = now_utc().isoformat()
        if self.settings_db is not None:
            try:
                self.settings_db.bump_trigger_count(str(config_id), when=now)
            except Exception as e:
                logger.warning("bump_trigger_count failed for %s: %s", config_id, e)
        data = self.cache.get(f"config:{config_id}")
        if data:
            data["trigger_count"] = data.get("trigger_count", 0) + 1
            data["last_triggered_at"] = now
            self.cache.set(f"config:{config_id}", data)

    # --- Delivery CRUD ---

    def create_delivery(self, delivery: NotificationDelivery) -> NotificationDelivery:
        """Persist a notification delivery record to SQLite."""
        if self.settings_db:
            self.settings_db.write_delivery(delivery.model_dump())
        return delivery

    def record_delivery(
        self,
        *,
        channel_id: Optional[str],
        channel_type: str,
        title: str,
        body: str,
        severity: str,
        recipient: str,
        success: bool,
        error_message: Optional[str] = None,
        config_id: Optional[str] = None,
    ) -> None:
        """Convenience wrapper: build a NotificationDelivery and persist it."""
        from uuid import uuid4
        from ..models.notification import ChannelType, NotificationMethod
        try:
            ch_type = ChannelType(channel_type)
        except ValueError:
            ch_type = ChannelType.TOAST
        try:
            ch_uuid = uuid.UUID(channel_id) if channel_id else None
        except (ValueError, TypeError):
            ch_uuid = None
        try:
            cfg_uuid = uuid.UUID(config_id) if config_id else None
        except (ValueError, TypeError):
            cfg_uuid = None
        delivery = NotificationDelivery(
            delivery_id=uuid4(),
            config_id=cfg_uuid,
            channel_id=ch_uuid,
            channel_type=ch_type,
            method=NotificationMethod.CHANNEL if ch_uuid else NotificationMethod.DIRECT,
            recipient=recipient,
            title=title,
            body=body,
            severity=severity,
            success=success,
            error_message=error_message or "",
            delivered_at=now_utc(),
        )
        self.create_delivery(delivery)

    async def get_delivery_history(self, limit: int = 100, offset: int = 0, channel_type: Optional[str] = None) -> list[NotificationDelivery]:
        """Get notification delivery history from SQLite. Indexed by
        delivered_at desc + an optional channel_type filter — sub-ms even
        with years of records."""
        if self.settings_db is None:
            return []
        ct = str(channel_type) if channel_type else None
        rows = self.settings_db.query_deliveries(
            limit=limit, offset=offset, channel_type=ct,
        )
        result: list[NotificationDelivery] = []
        for item in rows:
            try:
                result.append(NotificationDelivery(**item))
            except Exception:
                continue
        return result

    async def send_notification(
        self,
        title: str,
        body: str,
        severity: str,
        config_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Send a notification and record the delivery."""
        from ..engine.notification_dispatcher import NotificationDispatcher
        from ..models.notification import ChannelType

        dispatcher = NotificationDispatcher()
        result = await dispatcher.send_notification(
            title=title,
            body=body,
            severity=severity,
            config_id=config_id,
            channel_id=channel_id,
            metadata=metadata or {},
        )

        # Record delivery
        if self.settings_db:
            from uuid import uuid4
            channel_type_str = result.get("channel_type", "toast")
            try:
                channel_type = ChannelType(channel_type_str)
            except ValueError:
                channel_type = ChannelType.TOAST

            try:
                cfg_uuid = uuid.UUID(config_id) if config_id else None
            except (ValueError, TypeError):
                cfg_uuid = None
            try:
                ch_uuid = uuid.UUID(channel_id) if channel_id else None
            except (ValueError, TypeError):
                ch_uuid = None

            delivery = NotificationDelivery(
                delivery_id=uuid4(),
                config_id=cfg_uuid,
                channel_id=ch_uuid,
                channel_type=channel_type,
                method=NotificationMethod.CHANNEL if channel_id or config_id else NotificationMethod.DIRECT,
                recipient=result.get("recipient") or "local",
                title=title,
                body=body,
                severity=severity,
                success=result.get("success", False),
                error_message=result.get("error") or None,
                delivered_at=now_utc(),
            )
            self.create_delivery(delivery)

        return result

    def get_delivery_by_id(self, delivery_id: uuid.UUID) -> Optional[NotificationDelivery]:
        """Get a delivery record by ID."""
        cached = self.cache.get(f"delivery:{delivery_id}")
        if cached:
            return NotificationDelivery(**cached)
        return None

    # --- Channel methods (continued) ---

    async def list_channels(self) -> list[NotificationChannel]:
        """List all notification channels from SQLite."""
        if self.settings_db is None:
            return []
        result: list[NotificationChannel] = []
        for item in self.settings_db.query_channels():
            try:
                ch = self._dict_to_channel(item)
                self.cache.set(f"channel:{ch.channel_id}", item)
                result.append(ch)
            except Exception:
                continue
        return result

    async def get_channel(self, channel_id: str) -> Optional[NotificationChannel]:
        """Get a channel by string ID."""
        try:
            uid = uuid.UUID(channel_id)
        except ValueError:
            return None
        cached = self.cache.get(f"channel:{uid}")
        if cached:
            return self._dict_to_channel(cached)
        return None

    async def update_channel(self, channel_id: str, update: NotificationChannelUpdate) -> Optional[NotificationChannel]:
        """Update a channel by string ID."""
        try:
            uid = uuid.UUID(channel_id)
        except ValueError:
            return None
        channel_data = self.cache.get(f"channel:{uid}")
        if not channel_data:
            return None
        update_data = update.model_dump(exclude_none=True)
        for key, value in update_data.items():
            if isinstance(value, uuid.UUID):
                channel_data[key] = str(value)
            elif hasattr(value, "model_dump"):
                channel_data[key] = value.model_dump()
            else:
                channel_data[key] = value
        channel = self._dict_to_channel(channel_data)
        self._save_channel(channel)
        self.cache.set(f"channel:{uid}", channel_data)
        return channel

    async def delete_channel(self, channel_id: str) -> bool:
        """Delete a channel from both cache and SQLite."""
        try:
            uid = uuid.UUID(channel_id)
        except ValueError:
            return False
        cache_hit = self.cache.delete(f"channel:{uid}")
        db_hit = False
        if self.settings_db:
            try:
                db_hit = self.settings_db.delete_channel(str(uid))
            except Exception as e:
                logger.warning("Failed to delete channel %s from SQLite: %s", uid, e)
        return cache_hit or db_hit

    def get_all(self, enabled_only: bool = False) -> list[NotificationChannel]:
        """Get all channels from SQLite."""
        if self.settings_db is None:
            return []
        result: list[NotificationChannel] = []
        for item in self.settings_db.query_channels():
            try:
                ch = self._dict_to_channel(item)
                if enabled_only and not ch.enabled:
                    continue
                result.append(ch)
            except Exception:
                continue
        return result

    def update(self, channel_id: uuid.UUID, update: NotificationChannelUpdate) -> Optional[NotificationChannel]:
        """Update a channel."""
        channel_data = self.cache.get(f"channel:{channel_id}")
        if not channel_data:
            return None

        update_data = update.model_dump(exclude_none=True)
        for key, value in update_data.items():
            if isinstance(value, uuid.UUID):
                channel_data[key] = str(value)
            elif hasattr(value, "model_dump"):
                channel_data[key] = value.model_dump()
            else:
                channel_data[key] = value

        channel = self._dict_to_channel(channel_data)
        self._save_channel(channel)
        self.cache.set(f"channel:{channel_id}", channel_data)
        return channel

    def delete(self, channel_id: uuid.UUID) -> bool:
        """Delete a channel."""
        return self.cache.delete(f"channel:{channel_id}")

    def _save_channel(self, channel: NotificationChannel) -> None:
        """Persist channel to SQLite."""
        if self.settings_db is None:
            return
        self.settings_db.write_channel(channel.to_dict())

    @staticmethod
    def _dict_to_channel(data: dict[str, Any]) -> NotificationChannel:
        """Convert a dictionary to a NotificationChannel."""
        from ..models.notification import (
            ChannelSpecificConfig,
            ChannelType,
        )

        config_raw = data.get("config", {})
        if isinstance(config_raw, str):
            try:
                config_raw = json.loads(config_raw)
            except (ValueError, TypeError):
                config_raw = {}
        if not isinstance(config_raw, dict):
            config_raw = {}

        channel_type = data.get("channel_type", "toast")
        if not isinstance(channel_type, ChannelType):
            try:
                channel_type = ChannelType(channel_type)
            except ValueError:
                channel_type = ChannelType.TOAST

        # Pydantic will validate sub-configs from raw dict
        try:
            channel_config = ChannelSpecificConfig(**config_raw)
        except Exception:
            channel_config = ChannelSpecificConfig()

        rule_ids_raw = data.get("rule_ids", [])
        if isinstance(rule_ids_raw, str):
            try:
                rule_ids_raw = json.loads(rule_ids_raw)
            except (ValueError, TypeError):
                rule_ids_raw = []

        def _parse_dt(v: Any) -> Optional[datetime]:
            if v is None:
                return None
            if isinstance(v, datetime):
                return v
            try:
                return datetime.fromisoformat(v)
            except (ValueError, TypeError):
                return None

        ch_id_raw = data["channel_id"]
        return NotificationChannel(
            channel_id=uuid.UUID(ch_id_raw) if isinstance(ch_id_raw, str) else ch_id_raw,
            name=data["name"],
            description=data.get("description"),
            channel_type=channel_type,
            config=channel_config,
            enabled=data.get("enabled", True),
            rule_ids=[uuid.UUID(i) if isinstance(i, str) else i for i in rule_ids_raw],
            created_at=_parse_dt(data.get("created_at")) or now_utc(),
            last_sent_at=_parse_dt(data.get("last_sent_at")),
            send_count=int(data.get("send_count", 0) or 0),
            fail_count=int(data.get("fail_count", 0) or 0),
        )


class MetricRepository:
    """Repository for metric data points."""

    def __init__(self, cache: MetricCache, db: Optional[InfluxDBClient] = None):
        self.cache = cache
        self.db = db

    def create(self, point: MetricPoint) -> MetricPoint:
        """Store a single metric point."""
        self.cache.add_metric_point(point)
        self._write_to_db(point)
        return point

    def create_batch(self, points: list[MetricPoint]) -> int:
        """Store multiple metric points.

        Uses the bulk cache.add_metric_points() so the per-batch lock
        acquisition cost is paid once instead of N times — under stress
        ingestion this was the dominant source of contention with the
        rule-engine eval cycle.
        """
        self.cache.add_metric_points(points)
        self._write_batch_to_db(points)
        return len(points)

    def get_points(
        self,
        source: str,
        metric_name: str,
        since: Optional[datetime] = None,
        limit: int = 1000,
        hostname: Optional[str] = None,
    ) -> list[MetricPoint]:
        """Get metric points, optionally filtered by hostname.

        Routing rule: if the requested window fits within the cache's TTL,
        serve from the in-memory cache; otherwise query InfluxDB. This keeps
        the default 1-hour view fast (memory) while supporting longer
        historical queries (24h, 7d, 30d) directly from persistent storage.
        """
        now = datetime.now(timezone.utc)
        cache_ttl_seconds = getattr(self.cache, "_metric_ttl", 3600)

        if since is None:
            window_seconds = cache_ttl_seconds
        else:
            # Tolerate either naive (assume UTC) or tz-aware `since` so the
            # subtraction below never raises a tz mismatch.
            since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            window_seconds = max(0, (now - since_aware).total_seconds())

        # 1-second tolerance: callers like the route handler at
        # routes/metrics.py compute `since = now - timedelta(minutes=N)`,
        # then we re-read `now` here a few milliseconds later, so a request
        # for "the last 60 minutes" lands at window_seconds = 3600.005s.
        # Without this slack the boundary request would always miss the
        # cache by a hair and fall through to InfluxDB (and now to the
        # rollup), hiding the most-recent-hour fast-path entirely.
        use_cache = window_seconds <= cache_ttl_seconds + 1.0

        if use_cache:
            cache_points = self.cache.get_metric_points(
                source, metric_name, since=since, limit=limit, hostname=hostname
            )
            # Cache covers the requested window. If the cache has no points,
            # the series isn't reporting — don't waste an InfluxDB query on
            # the hot rule-eval path (each miss costs ~1-2s round-trip when
            # InfluxDB is busy ingesting). Long-range queries (window past
            # cache TTL) still fall through below.
            return cache_points

        if self.db is None:
            return self.cache.get_metric_points(
                source, metric_name, since=since, limit=limit, hostname=hostname
            )

        # Pick a downsampling bucket so a long-window chart returns hundreds
        # of points (not hundreds of thousands). Full resolution is preserved
        # for ≤1h windows (those come from the in-memory cache above and
        # never reach this branch). Tier ladder is configurable via
        # [[alarm_engine.history.downsampling.tiers]] in llm-systems.toml.
        ws = window_seconds
        every: Optional[str] = None
        for tier in settings.alarm_engine.history.downsampling.tiers:
            if ws <= tier.max_window_s:
                every = tier.every
                break
        # ws beyond the last tier → fall through to the largest configured
        # bucket so 30-day-plus requests still get a usable chart.
        if every is None and settings.alarm_engine.history.downsampling.tiers:
            every = settings.alarm_engine.history.downsampling.tiers[-1].every

        try:
            db_points = self.db.query_metrics(
                source, metric_name, start=since, limit=limit, every=every,
            )
        except Exception as e:
            logger.warning(f"DB metric query failed for {source}/{metric_name}: {e}")
            return self.cache.get_metric_points(
                source, metric_name, since=since, limit=limit, hostname=hostname
            )

        result: list[MetricPoint] = []
        for p in db_points:
            ts_raw = p.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else (ts_raw or now)
            except (ValueError, TypeError):
                ts = now
            result.append(
                MetricPoint(
                    source=source,
                    metric_name=metric_name,
                    value=p.get("value", 0),
                    unit=p.get("unit"),
                    timestamp=ts,
                    hostname=p.get("hostname"),
                )
            )
        if hostname:
            result = [p for p in result if p.hostname == hostname]
        return result

    def get_latest(self, source: str, metric_name: str) -> Optional[MetricPoint]:
        """Get the latest metric point."""
        # Try cache first
        cached_points = self.cache.get_metric_points(source, metric_name, limit=1)
        if cached_points:
            return cached_points[-1]

        if self.db is None:
            return None

        try:
            db_point = self.db.query_latest_metric(source, metric_name)
        except Exception as e:
            logger.warning(f"DB latest-metric query failed for {source}/{metric_name}: {e}")
            return None

        if db_point:
            ts_raw = db_point.get("timestamp")
            try:
                ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else (ts_raw or now_utc())
            except (ValueError, TypeError):
                ts = now_utc()
            point = MetricPoint(
                source=source,
                metric_name=metric_name,
                value=db_point["value"],
                unit=db_point.get("unit"),
                timestamp=ts,
            )
            self.cache.add_metric_point(point)
            return point
        return None

    def get_summary(
        self,
        source: str,
        metric_name: str,
        window_minutes: int = 60,
    ) -> Optional[MetricSummary]:
        """Get aggregated metric summary."""
        summary = self.cache.get_metric_summary(source, metric_name, window_minutes=window_minutes)
        if summary:
            return summary

        if self.db is None:
            return None

        # Fallback to DB statistics
        try:
            stats = self.db.query_metric_statistics(source, metric_name, window_minutes)
        except Exception as e:
            logger.warning(f"DB statistics query failed for {source}/{metric_name}: {e}")
            return None
        if stats:
            return MetricSummary(
                source=source,
                metric_name=metric_name,
                unit=None,
                current_value=0,
                min_value=stats.get("min", 0),
                max_value=stats.get("max", 0),
                avg_value=stats.get("mean", 0),
                std_dev=stats.get("stddev", 0),
                p90=0,
                p95=0,
                p99=0,
                data_points=0,
                last_updated=now_utc(),
            )
        return None

    def _write_to_db(self, point: MetricPoint) -> None:
        """Write a single metric point to InfluxDB."""
        if self.db is None:
            return
        tags = {
            "source": point.source,
            "metric_name": point.metric_name,
            "unit": point.unit or "",
        }
        if point.hostname:
            tags["hostname"] = point.hostname
        record = {
            "measurement": "metrics",
            "tags": tags,
            "fields": {
                "value": point.value,
            },
            "time": int(point.timestamp.timestamp() * 1e9),
        }
        self.db.write_metric(record)

    def _write_batch_to_db(self, points: list[MetricPoint]) -> None:
        """Write multiple metric points to InfluxDB."""
        if self.db is None:
            return
        records = []
        for point in points:
            tags = {
                "source": point.source,
                "metric_name": point.metric_name,
                "unit": point.unit or "",
            }
            if point.hostname:
                tags["hostname"] = point.hostname
            records.append({
                "measurement": "metrics",
                "tags": tags,
                "fields": {
                    "value": point.value,
                },
                "time": int(point.timestamp.timestamp() * 1e9),
            })
        self.db.write_metrics_batch(records)
