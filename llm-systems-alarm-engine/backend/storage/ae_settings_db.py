"""SQLite-backed store for alarm-engine rules, notification channels, and
notification configs.

Replaces the `alarm_engine_settings` InfluxDB bucket. Time-series storage was
the wrong model for transactional config: the read window (last 30d) silently
dropped untouched records and the Flux pivots made the Notifications tab
needlessly slow. Single SQLite file, three tables, sub-ms reads.
"""

import logging
import sqlite3
from .._time import now_utc
from pathlib import Path
from typing import Any, Optional

from ._serde import (
    RLock,
    enum_value as _enum_value,
    from_json as _from_json,
    open_sqlite,
    to_iso as _to_iso,
    to_json as _to_json,
)

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);

CREATE TABLE IF NOT EXISTS rules (
  rule_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  source_host TEXT,
  metric_source TEXT NOT NULL,
  metric_name TEXT NOT NULL,
  rule_type TEXT NOT NULL,
  config_json TEXT NOT NULL DEFAULT '{}',
  severity TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  notification_channel_ids_json TEXT NOT NULL DEFAULT '[]',
  quiet_hours_start TEXT,
  quiet_hours_end TEXT,
  auto_resolve_cycles INTEGER NOT NULL DEFAULT 2,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_evaluated_at TEXT,
  last_alert_at TEXT
);
CREATE INDEX IF NOT EXISTS rules_enabled_idx ON rules(enabled);

CREATE TABLE IF NOT EXISTS channels (
  channel_id TEXT PRIMARY KEY,
  channel_type TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT,
  config_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  rule_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  last_sent_at TEXT,
  send_count INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS configs (
  config_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  channels_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  auto_dismiss INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_triggered_at TEXT,
  trigger_count INTEGER NOT NULL DEFAULT 0,
  min_severity TEXT,
  metric_sources_json TEXT NOT NULL DEFAULT '[]',
  metric_names_json TEXT NOT NULL DEFAULT '[]',
  source_hosts_json TEXT NOT NULL DEFAULT '[]',
  repeat_interval_minutes INTEGER NOT NULL DEFAULT 30,
  notify_on_clear INTEGER NOT NULL DEFAULT 0,
  min_alarm_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,
  config_id TEXT,
  channel_id TEXT,
  channel_type TEXT NOT NULL,
  method TEXT NOT NULL,
  recipient TEXT,
  title TEXT,
  body TEXT,
  severity TEXT,
  success INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  delivered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deliveries_delivered_at_idx ON deliveries(delivered_at DESC);
CREATE INDEX IF NOT EXISTS deliveries_config_id_idx ON deliveries(config_id);
"""


class AeSettingsDB:
    """Thread-safe SQLite wrapper for rules/channels/configs."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._lock = RLock()
        self.path = path

    @classmethod
    def open(cls, path: Path) -> "AeSettingsDB":
        conn = open_sqlite(Path(path), _SCHEMA)
        logger.info("AeSettingsDB opened at %s", path)
        return cls(conn, Path(path))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ── Rules ────────────────────────────────────────────────────────────

    def write_rule(self, rule: dict[str, Any]) -> None:
        cols = (
            rule.get("rule_id"),
            rule.get("name") or "",
            rule.get("description") or None,
            rule.get("source_host") or None,
            rule.get("metric_source") or "",
            rule.get("metric_name") or "",
            rule.get("rule_type") or "",
            _to_json(rule.get("config") or {}, default="{}"),
            rule.get("severity") or "",
            1 if rule.get("enabled", True) else 0,
            _to_json(rule.get("notification_channel_ids") or []),
            rule.get("quiet_hours_start") or None,
            rule.get("quiet_hours_end") or None,
            int(rule.get("auto_resolve_cycles") or 2),
            _to_iso(rule.get("created_at")) or now_utc().isoformat(),
            _to_iso(rule.get("updated_at")) or now_utc().isoformat(),
            _to_iso(rule.get("last_evaluated_at")),
            _to_iso(rule.get("last_alert_at")),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rules (
                  rule_id, name, description, source_host,
                  metric_source, metric_name, rule_type, config_json,
                  severity, enabled, notification_channel_ids_json,
                  quiet_hours_start, quiet_hours_end, auto_resolve_cycles,
                  created_at, updated_at, last_evaluated_at, last_alert_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(rule_id) DO UPDATE SET
                  name=excluded.name,
                  description=excluded.description,
                  source_host=excluded.source_host,
                  metric_source=excluded.metric_source,
                  metric_name=excluded.metric_name,
                  rule_type=excluded.rule_type,
                  config_json=excluded.config_json,
                  severity=excluded.severity,
                  enabled=excluded.enabled,
                  notification_channel_ids_json=excluded.notification_channel_ids_json,
                  quiet_hours_start=excluded.quiet_hours_start,
                  quiet_hours_end=excluded.quiet_hours_end,
                  auto_resolve_cycles=excluded.auto_resolve_cycles,
                  updated_at=excluded.updated_at,
                  last_evaluated_at=excluded.last_evaluated_at,
                  last_alert_at=excluded.last_alert_at
                """,
                cols,
            )

    def delete_rule(self, rule_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM rules WHERE rule_id=?", (rule_id,)
            )
            return cur.rowcount > 0

    def delete_all_rules(self) -> bool:
        with self._lock:
            self._conn.execute("DELETE FROM rules")
        return True

    def query_rules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rules"
        if enabled_only:
            sql += " WHERE enabled=1"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._row_to_rule(r) for r in rows]

    @staticmethod
    def _row_to_rule(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": r["rule_id"],
            "name": r["name"],
            "description": r["description"],
            "source_host": r["source_host"],
            "metric_source": r["metric_source"],
            "metric_name": r["metric_name"],
            "rule_type": r["rule_type"],
            "config": _from_json(r["config_json"], {}),
            "severity": r["severity"],
            "enabled": bool(r["enabled"]),
            "notification_channel_ids": _from_json(r["notification_channel_ids_json"], []),
            "quiet_hours_start": r["quiet_hours_start"],
            "quiet_hours_end": r["quiet_hours_end"],
            "auto_resolve_cycles": r["auto_resolve_cycles"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "last_evaluated_at": r["last_evaluated_at"],
            "last_alert_at": r["last_alert_at"],
        }

    # ── Channels ─────────────────────────────────────────────────────────

    def write_channel(self, ch: dict[str, Any]) -> None:
        cols = (
            str(ch.get("channel_id") or ""),
            _enum_value(ch.get("channel_type")),
            ch.get("name") or "",
            ch.get("description") or None,
            _to_json(ch.get("config") or {}, default="{}"),
            1 if ch.get("enabled", True) else 0,
            _to_json(ch.get("rule_ids") or []),
            _to_iso(ch.get("created_at")) or now_utc().isoformat(),
            _to_iso(ch.get("last_sent_at")),
            int(ch.get("send_count") or 0),
            int(ch.get("fail_count") or 0),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO channels (
                  channel_id, channel_type, name, description, config_json,
                  enabled, rule_ids_json, created_at, last_sent_at,
                  send_count, fail_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel_id) DO UPDATE SET
                  channel_type=excluded.channel_type,
                  name=excluded.name,
                  description=excluded.description,
                  config_json=excluded.config_json,
                  enabled=excluded.enabled,
                  rule_ids_json=excluded.rule_ids_json,
                  last_sent_at=excluded.last_sent_at,
                  send_count=excluded.send_count,
                  fail_count=excluded.fail_count
                """,
                cols,
            )

    def delete_channel(self, channel_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM channels WHERE channel_id=?", (channel_id,)
            )
            return cur.rowcount > 0

    def query_channels(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM channels").fetchall()
        return [self._row_to_channel(r) for r in rows]

    def get_channel(self, channel_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
        return self._row_to_channel(row) if row else None

    @staticmethod
    def _row_to_channel(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "channel_id": r["channel_id"],
            "channel_type": r["channel_type"],
            "name": r["name"],
            "description": r["description"],
            "config": _from_json(r["config_json"], {}),
            "enabled": bool(r["enabled"]),
            "rule_ids": _from_json(r["rule_ids_json"], []),
            "created_at": r["created_at"],
            "last_sent_at": r["last_sent_at"],
            "send_count": r["send_count"],
            "fail_count": r["fail_count"],
        }

    # ── Configs (notification policies) ──────────────────────────────────

    def write_config(self, cfg: dict[str, Any]) -> None:
        cols = (
            str(cfg.get("config_id") or ""),
            cfg.get("name") or "",
            cfg.get("description") or None,
            _to_json(cfg.get("channels") or []),
            1 if cfg.get("enabled", True) else 0,
            1 if cfg.get("auto_dismiss", True) else 0,
            _to_iso(cfg.get("created_at")) or now_utc().isoformat(),
            _to_iso(cfg.get("last_triggered_at")),
            int(cfg.get("trigger_count") or 0),
            cfg.get("min_severity") or None,
            _to_json(cfg.get("metric_sources") or []),
            _to_json(cfg.get("metric_names") or []),
            _to_json(cfg.get("source_hosts") or []),
            int(cfg.get("repeat_interval_minutes") or 30),
            1 if cfg.get("notify_on_clear") else 0,
            int(cfg.get("min_alarm_count") or 1),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO configs (
                  config_id, name, description, channels_json,
                  enabled, auto_dismiss, created_at, last_triggered_at,
                  trigger_count, min_severity, metric_sources_json,
                  metric_names_json, source_hosts_json,
                  repeat_interval_minutes, notify_on_clear, min_alarm_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(config_id) DO UPDATE SET
                  name=excluded.name,
                  description=excluded.description,
                  channels_json=excluded.channels_json,
                  enabled=excluded.enabled,
                  auto_dismiss=excluded.auto_dismiss,
                  last_triggered_at=excluded.last_triggered_at,
                  trigger_count=excluded.trigger_count,
                  min_severity=excluded.min_severity,
                  metric_sources_json=excluded.metric_sources_json,
                  metric_names_json=excluded.metric_names_json,
                  source_hosts_json=excluded.source_hosts_json,
                  repeat_interval_minutes=excluded.repeat_interval_minutes,
                  notify_on_clear=excluded.notify_on_clear,
                  min_alarm_count=excluded.min_alarm_count
                """,
                cols,
            )

    def delete_config(self, config_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM configs WHERE config_id=?", (config_id,)
            )
            return cur.rowcount > 0

    def query_configs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM configs").fetchall()
        return [self._row_to_config(r) for r in rows]

    def get_config(self, config_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM configs WHERE config_id=?", (config_id,)
            ).fetchone()
        return self._row_to_config(row) if row else None

    def bump_trigger_count(self, config_id: str, *, when: Optional[str] = None) -> None:
        ts = when or now_utc().isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE configs SET trigger_count = trigger_count + 1, "
                "last_triggered_at = ? WHERE config_id = ?",
                (ts, config_id),
            )

    @staticmethod
    def _row_to_config(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "config_id": r["config_id"],
            "name": r["name"],
            "description": r["description"],
            "channels": _from_json(r["channels_json"], []),
            "enabled": bool(r["enabled"]),
            "auto_dismiss": bool(r["auto_dismiss"]),
            "created_at": r["created_at"],
            "last_triggered_at": r["last_triggered_at"],
            "trigger_count": r["trigger_count"],
            "min_severity": r["min_severity"],
            "metric_sources": _from_json(r["metric_sources_json"], []),
            "metric_names": _from_json(r["metric_names_json"], []),
            "source_hosts": _from_json(r["source_hosts_json"], []),
            "repeat_interval_minutes": r["repeat_interval_minutes"],
            "notify_on_clear": bool(r["notify_on_clear"]),
            "min_alarm_count": r["min_alarm_count"],
        }

    # ── Deliveries (notification send records) ───────────────────────────

    def write_delivery(self, d: dict[str, Any]) -> None:
        cols = (
            str(d.get("delivery_id") or ""),
            str(d.get("config_id")) if d.get("config_id") else None,
            str(d.get("channel_id")) if d.get("channel_id") else None,
            _enum_value(d.get("channel_type")),
            _enum_value(d.get("method")),
            d.get("recipient") or None,
            d.get("title") or None,
            d.get("body") or None,
            d.get("severity") or None,
            1 if d.get("success") else 0,
            d.get("error_message") or None,
            _to_iso(d.get("delivered_at")) or now_utc().isoformat(),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO deliveries (
                  delivery_id, config_id, channel_id, channel_type, method,
                  recipient, title, body, severity, success, error_message,
                  delivered_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(delivery_id) DO NOTHING
                """,
                cols,
            )

    def query_deliveries(
        self,
        limit: int = 100,
        offset: int = 0,
        channel_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        # Latest-first; the Notifications tab shows recent history.
        sql = "SELECT * FROM deliveries"
        args: list = []
        if channel_type:
            sql += " WHERE channel_type = ?"
            args.append(str(channel_type))
        sql += " ORDER BY delivered_at DESC LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_delivery(r) for r in rows]

    def get_delivery(self, delivery_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
        return self._row_to_delivery(row) if row else None

    def delete_deliveries_older_than(self, iso_cutoff: str) -> int:
        """Optional retention helper for an operator cron."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM deliveries WHERE delivered_at < ?", (iso_cutoff,)
            )
            return cur.rowcount

    @staticmethod
    def _row_to_delivery(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "delivery_id": r["delivery_id"],
            "config_id": r["config_id"],
            "channel_id": r["channel_id"],
            "channel_type": r["channel_type"],
            "method": r["method"],
            "recipient": r["recipient"],
            "title": r["title"],
            "body": r["body"],
            "severity": r["severity"],
            "success": bool(r["success"]),
            "error_message": r["error_message"],
            "delivered_at": r["delivered_at"],
        }
