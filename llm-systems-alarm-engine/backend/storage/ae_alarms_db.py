"""SQLite-backed store for alarm-engine alerts.

Alerts are a state machine (active → acknowledged → closed / ignored).
State transitions are UPDATEs in place; closed alerts stay in-table as
history. `refresh()` during a long-running alert is one UPDATE per cycle.
"""

import logging
import sqlite3
from .._best_effort import best_effort
from .._time import now_utc
from pathlib import Path
from typing import Any, Optional

from ..models.alert import AlertStatus
from ._serde import (
    RLock,
    enum_value as _enum_value,
    from_json as _from_json,
    open_sqlite,
    to_iso as _to_iso,
    to_json as _to_json,
)

logger = logging.getLogger(__name__)

# Status values considered "live" (visible on the dashboard / counted in
# active stats). Built from the enum so renames stay coherent.
_LIVE_STATUSES = (AlertStatus.ACTIVE.value, AlertStatus.ACKNOWLEDGED.value)
_LIVE_STATUS_SQL_LIST = "({})".format(
    ", ".join(f"'{s}'" for s in _LIVE_STATUSES)
)

# Non-live statuses (#220) — rows in these statuses are archived out of the
# hot `alerts` table into `alert_history`. Derived as the complement of
# _LIVE_STATUSES so new AlertStatus values default to archiving.
_NON_LIVE_STATUSES = tuple(
    s.value for s in AlertStatus if s.value not in _LIVE_STATUSES
)
_NON_LIVE_STATUS_SQL_LIST = "({})".format(
    ", ".join(f"'{s}'" for s in _NON_LIVE_STATUSES)
)

# Shared column list for alerts <-> alert_history moves (#220).
_ALERT_COLUMNS = (
    "alert_id, rule_id, rule_name, metric_source, metric_name, source_host, "
    "incident_id, current_value, threshold_value, severity, status, message, "
    "trigger_count, acknowledged_by, exception_details, resolution_reason, "
    "resolved_value, notification_channel_ids_json, created_at, "
    "last_evaluated_at, acknowledged_at, closed_at, chart_window_start, chart_window_end"
)

# alert_history: identical column set to alerts, PK alert_id (#220).
_ALERT_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS alert_history (
  alert_id TEXT PRIMARY KEY,
  rule_id TEXT,
  rule_name TEXT,
  metric_source TEXT NOT NULL,
  metric_name TEXT NOT NULL,
  source_host TEXT,
  incident_id TEXT,
  current_value REAL NOT NULL,
  threshold_value REAL NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT,
  trigger_count INTEGER NOT NULL DEFAULT 1,
  acknowledged_by TEXT,
  exception_details TEXT,
  resolution_reason TEXT,
  resolved_value REAL,
  notification_channel_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  last_evaluated_at TEXT,
  acknowledged_at TEXT,
  closed_at TEXT,
  chart_window_start TEXT,
  chart_window_end TEXT
);
CREATE INDEX IF NOT EXISTS alert_history_created_idx ON alert_history(created_at DESC);
CREATE INDEX IF NOT EXISTS alert_history_status_idx ON alert_history(status);
"""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);
INSERT OR IGNORE INTO schema_version VALUES (2);

CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY,
  rule_id TEXT,
  rule_name TEXT,
  metric_source TEXT NOT NULL,
  metric_name TEXT NOT NULL,
  source_host TEXT,
  incident_id TEXT,
  current_value REAL NOT NULL,
  threshold_value REAL NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT,
  trigger_count INTEGER NOT NULL DEFAULT 1,
  acknowledged_by TEXT,
  exception_details TEXT,
  resolution_reason TEXT,
  resolved_value REAL,
  notification_channel_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  last_evaluated_at TEXT,
  acknowledged_at TEXT,
  closed_at TEXT,
  -- Optional explicit chart-window overrides for the click-through
  -- feature. NULL → callers compute defaults from created_at / closed_at
  -- with a sensible buffer.
  chart_window_start TEXT,
  chart_window_end TEXT
);
CREATE INDEX IF NOT EXISTS alerts_status_idx ON alerts(status);
CREATE INDEX IF NOT EXISTS alerts_rule_id_idx ON alerts(rule_id);
CREATE INDEX IF NOT EXISTS alerts_created_at_idx ON alerts(created_at DESC);
""" + _ALERT_HISTORY_DDL


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migration for pre-#215 files (first live migration)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
    if "incident_id" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN incident_id TEXT")
        logger.info("ae_alarms.db migrated: alerts.incident_id added")
    conn.execute("CREATE INDEX IF NOT EXISTS alerts_incident_idx ON alerts(incident_id)")
    conn.execute("INSERT OR IGNORE INTO schema_version VALUES (2)")

    # #220: one-time backfill of non-live rows into alert_history (table
    # itself already exists via _SCHEMA, run by open_sqlite before _migrate).
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            f"INSERT OR IGNORE INTO alert_history ({_ALERT_COLUMNS}) "
            f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE status IN {_NON_LIVE_STATUS_SQL_LIST}"
        )
        conn.execute(f"DELETE FROM alerts WHERE status IN {_NON_LIVE_STATUS_SQL_LIST}")
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (3)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.commit()


class AeAlarmsDB:
    """Thread-safe SQLite wrapper for alerts."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self._lock = RLock()
        self.path = path

    @classmethod
    def open(cls, path: Path) -> "AeAlarmsDB":
        conn = open_sqlite(Path(path), _SCHEMA)
        _migrate(conn)
        logger.info("AeAlarmsDB opened at %s", path)
        return cls(conn, Path(path))

    def close(self) -> None:
        with self._lock:
            with best_effort("close ae_alarms_db connection", log=logger):
                self._conn.close()

    def _archive_non_live(self, alert_id: Optional[str] = None) -> None:
        """Move non-live alert rows into alert_history, one alert or all
        (#220). REPLACE so re-archiving an alert_id overwrites its row."""
        where = f"status IN {_NON_LIVE_STATUS_SQL_LIST}"
        args: list[Any] = []
        if alert_id is not None:
            where += " AND alert_id = ?"
            args.append(str(alert_id))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    f"INSERT OR REPLACE INTO alert_history ({_ALERT_COLUMNS}) "
                    f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE {where}",
                    args,
                )
                self._conn.execute(f"DELETE FROM alerts WHERE {where}", args)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ── Writes ───────────────────────────────────────────────────────────

    def write_alert(self, a: dict[str, Any]) -> None:
        """UPSERT an alert. The Alert model writes through to_dict() and we
        store every field; state changes are subsequent UPDATEs via this
        same method (excluded.* picks up changed fields)."""
        cols = (
            str(a.get("alert_id") or ""),
            str(a.get("rule_id")) if a.get("rule_id") else None,
            a.get("rule_name") or None,
            a.get("metric_source") or "",
            a.get("metric_name") or "",
            a.get("source_host") or None,
            a.get("incident_id") or None,
            float(a.get("current_value") or 0.0),
            float(a.get("threshold_value") or 0.0),
            a.get("severity") or "info",
            _enum_value(a.get("status"), default=AlertStatus.ACTIVE.value),
            a.get("message") or None,
            int(a.get("trigger_count") or 1),
            a.get("acknowledged_by") or None,
            a.get("exception_details") or None,
            a.get("resolution_reason") or None,
            (float(a["resolved_value"]) if a.get("resolved_value") not in (None, "") else None),
            _to_json(a.get("notification_channel_ids") or []),
            _to_iso(a.get("created_at")) or now_utc().isoformat(),
            _to_iso(a.get("last_evaluated_at")),
            _to_iso(a.get("acknowledged_at")),
            _to_iso(a.get("closed_at")),
            _to_iso(a.get("chart_window_start")),
            _to_iso(a.get("chart_window_end")),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO alerts (
                  alert_id, rule_id, rule_name, metric_source, metric_name,
                  source_host, incident_id, current_value, threshold_value, severity,
                  status, message, trigger_count, acknowledged_by,
                  exception_details, resolution_reason, resolved_value,
                  notification_channel_ids_json, created_at,
                  last_evaluated_at, acknowledged_at, closed_at,
                  chart_window_start, chart_window_end
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(alert_id) DO UPDATE SET
                  rule_name=excluded.rule_name,
                  source_host=excluded.source_host,
                  incident_id=excluded.incident_id,
                  current_value=excluded.current_value,
                  threshold_value=excluded.threshold_value,
                  severity=excluded.severity,
                  status=excluded.status,
                  message=excluded.message,
                  trigger_count=excluded.trigger_count,
                  acknowledged_by=excluded.acknowledged_by,
                  exception_details=excluded.exception_details,
                  resolution_reason=excluded.resolution_reason,
                  resolved_value=excluded.resolved_value,
                  notification_channel_ids_json=excluded.notification_channel_ids_json,
                  last_evaluated_at=excluded.last_evaluated_at,
                  acknowledged_at=excluded.acknowledged_at,
                  closed_at=excluded.closed_at,
                  chart_window_start=excluded.chart_window_start,
                  chart_window_end=excluded.chart_window_end
                """,
                cols,
            )
            # #220: non-live write archives the row; live write clears any
            # stale history row so the alert_id lives in exactly one table.
            if cols[10] in _NON_LIVE_STATUSES:
                self._archive_non_live(alert_id=cols[0])
            else:
                self._conn.execute(
                    "DELETE FROM alert_history WHERE alert_id=?", (cols[0],)
                )

    def bump_refresh(self, alert_id: str, current_value: float, when: Optional[str] = None) -> None:
        """Hot-path UPDATE for refresh() — called once per rule-eval cycle
        while an alert is still firing. Single round-trip, no read."""
        ts = when or now_utc().isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE alerts SET current_value = ?, "
                "last_evaluated_at = ?, trigger_count = trigger_count + 1 "
                "WHERE alert_id = ?",
                (float(current_value), ts, str(alert_id)),
            )

    def purge_history_older_than(self, cutoff_iso: str) -> int:
        """Delete alert_history rows whose effective end time is before
        cutoff_iso (#220 retention). Returns the number of rows deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM alert_history "
                "WHERE COALESCE(closed_at, acknowledged_at, created_at) < ?",
                (cutoff_iso,),
            )
            return cur.rowcount or 0

    def delete_alert(self, alert_id: str) -> bool:
        """Delete from both alerts and alert_history (#220) — a row lives
        in exactly one, but callers don't need to know which."""
        with self._lock:
            cur1 = self._conn.execute(
                "DELETE FROM alerts WHERE alert_id=?", (str(alert_id),)
            )
            cur2 = self._conn.execute(
                "DELETE FROM alert_history WHERE alert_id=?", (str(alert_id),)
            )
            return cur1.rowcount > 0 or cur2.rowcount > 0

    # ── Reads ────────────────────────────────────────────────────────────

    def get_alert(self, alert_id: str) -> Optional[dict[str, Any]]:
        """Try the hot `alerts` table, fall back to `alert_history` (#220)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM alerts WHERE alert_id=?", (str(alert_id),)
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT * FROM alert_history WHERE alert_id=?", (str(alert_id),)
                ).fetchone()
        return self._row_to_alert(row) if row else None

    def query_active(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM alerts WHERE status IN {_LIVE_STATUS_SQL_LIST} "
                "ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def query_all(self, limit: int = 10000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def query_filtered(
        self,
        *,
        live_only: bool = False,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        rule_id: Optional[str] = None,
        metric_source: Optional[str] = None,
        metric_name: Optional[str] = None,
        sort_by: str = "created_at",
        sort_desc: bool = True,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Filter + sort + paginate at the SQL layer instead of pulling
        every row and filtering in Python. Whitelisted sort columns;
        anything else falls back to `created_at`. `live_only=False` spans
        `alerts` + `alert_history` via UNION ALL (#220)."""
        where: list[str] = []
        args: list[Any] = []
        if status:
            where.append("status = ?")
            args.append(_enum_value(status))
        if severity:
            where.append("severity = ?")
            args.append(str(severity))
        if rule_id:
            where.append("rule_id = ?")
            args.append(str(rule_id))
        if metric_source:
            where.append("metric_source = ?")
            args.append(str(metric_source))
        if metric_name:
            where.append("metric_name = ?")
            args.append(str(metric_name))
        sort_col = sort_by if sort_by in ("created_at", "severity") else "created_at"
        order = "DESC" if sort_desc else "ASC"
        if live_only:
            live_where = [f"status IN {_LIVE_STATUS_SQL_LIST}"] + where
            sql = "SELECT * FROM alerts WHERE " + " AND ".join(live_where)
            sql += f" ORDER BY {sort_col} {order} LIMIT ? OFFSET ?"
            full_args = args + [int(limit), int(offset)]
        else:
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
            sql = (
                "SELECT * FROM ("
                f"SELECT {_ALERT_COLUMNS} FROM alerts{where_sql} "
                "UNION ALL "
                f"SELECT {_ALERT_COLUMNS} FROM alert_history{where_sql}"
                f") ORDER BY {sort_col} {order} LIMIT ? OFFSET ?"
            )
            full_args = args + args + [int(limit), int(offset)]
        with self._lock:
            rows = self._conn.execute(sql, full_args).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def count_by_status_and_severity(self) -> dict[str, dict[str, int]]:
        """One indexed GROUP BY for AlertManager.get_alert_stats /
        AlertRepository.get_alert_counters, across `alerts` + `alert_history` (#220)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, severity, COUNT(*) AS n FROM ("
                "SELECT status, severity FROM alerts "
                "UNION ALL "
                "SELECT status, severity FROM alert_history"
                ") GROUP BY status, severity"
            ).fetchall()
        by_status: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        total = 0
        for r in rows:
            s = r["status"] or "unknown"
            sev = r["severity"] or "unknown"
            n = int(r["n"])
            by_status[s] = by_status.get(s, 0) + n
            by_severity[sev] = by_severity.get(sev, 0) + n
            total += n
        return {"by_status": by_status, "by_severity": by_severity, "total": total}

    def bulk_update_status(
        self,
        *,
        from_statuses: tuple[str, ...],
        to_status: str,
        closed_at: Optional[str] = None,
        acknowledged_at: Optional[str] = None,
    ) -> int:
        """One UPDATE for close-all / ignore-all instead of N round-trips."""
        to_status_value = _enum_value(to_status)
        sets = ["status = ?"]
        args: list[Any] = [to_status_value]
        if closed_at is not None:
            sets.append("closed_at = ?")
            args.append(closed_at)
        if acknowledged_at is not None:
            sets.append("acknowledged_at = ?")
            args.append(acknowledged_at)
        in_list = "({})".format(", ".join("?" * len(from_statuses)))
        sql = f"UPDATE alerts SET {', '.join(sets)} WHERE status IN {in_list}"
        args.extend(_enum_value(s) for s in from_statuses)
        with self._lock:
            cur = self._conn.execute(sql, args)
            n = cur.rowcount or 0
            # #220: sync-archive rows the bulk update just moved non-live.
            if to_status_value in _NON_LIVE_STATUSES:
                self._archive_non_live()
            return n

    @staticmethod
    def _row_to_alert(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "alert_id": r["alert_id"],
            "rule_id": r["rule_id"],
            "rule_name": r["rule_name"],
            "metric_source": r["metric_source"],
            "metric_name": r["metric_name"],
            "source_host": r["source_host"],
            "incident_id": r["incident_id"],
            "current_value": r["current_value"],
            "threshold_value": r["threshold_value"],
            "severity": r["severity"],
            "status": r["status"],
            "message": r["message"],
            "trigger_count": r["trigger_count"],
            "acknowledged_by": r["acknowledged_by"],
            "exception_details": r["exception_details"],
            "resolution_reason": r["resolution_reason"],
            "resolved_value": r["resolved_value"],
            "notification_channel_ids": _from_json(r["notification_channel_ids_json"], []),
            "created_at": r["created_at"],
            "last_evaluated_at": r["last_evaluated_at"],
            "acknowledged_at": r["acknowledged_at"],
            "closed_at": r["closed_at"],
            "chart_window_start": r["chart_window_start"],
            "chart_window_end": r["chart_window_end"],
        }
