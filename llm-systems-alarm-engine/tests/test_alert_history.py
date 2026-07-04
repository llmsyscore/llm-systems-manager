"""#220: alert_history table, v3 migration + synchronous archive."""
import sqlite3

from backend.storage.ae_alarms_db import AeAlarmsDB

# Byte-faithful v2 alerts schema (pre-#220, post-#215) to migrate from.
_V2_ALERTS = """
CREATE TABLE alerts (
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
CREATE INDEX alerts_status_idx ON alerts(status);
CREATE INDEX alerts_rule_id_idx ON alerts(rule_id);
CREATE INDEX alerts_created_at_idx ON alerts(created_at DESC);
CREATE INDEX alerts_incident_idx ON alerts(incident_id);
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1);
INSERT INTO schema_version VALUES (2);
"""


def _make_v2(path):
    conn = sqlite3.connect(path)
    conn.executescript(_V2_ALERTS)
    for alert_id, status in (
        ("active-1", "active"),
        ("closed-1", "closed"),
        ("ignored-1", "ignored"),
    ):
        conn.execute(
            "INSERT INTO alerts (alert_id, metric_source, metric_name, current_value,"
            " threshold_value, severity, status, created_at)"
            " VALUES (?, 'gpu', 'temp', 90, 85, 'warning', ?, '2026-01-01T00:00:00')",
            (alert_id, status),
        )
    conn.commit()
    conn.close()


def _history_alert(db, alert_id):
    """Fetch a row from alert_history directly (get_alert is live-table-only until Task 3)."""
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM alert_history WHERE alert_id=?", (alert_id,)
        ).fetchone()
    return db._row_to_alert(row) if row else None


def _tables(db):
    return {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_v2_db_gains_alert_history_table(tmp_path):
    p = tmp_path / "ae_alarms.db"
    _make_v2(p)
    db = AeAlarmsDB.open(p)
    try:
        assert "alert_history" in _tables(db)
    finally:
        db.close()


def test_v2_migration_moves_non_live_rows_to_history(tmp_path):
    p = tmp_path / "ae_alarms.db"
    _make_v2(p)
    db = AeAlarmsDB.open(p)
    try:
        alert_ids = {r["alert_id"] for r in db._conn.execute("SELECT alert_id FROM alerts")}
        assert alert_ids == {"active-1"}
        assert db.get_alert("active-1") is not None
        assert _history_alert(db, "closed-1") is not None
        assert _history_alert(db, "ignored-1") is not None
    finally:
        db.close()


def test_v2_migration_is_idempotent(tmp_path):
    p = tmp_path / "ae_alarms.db"
    _make_v2(p)
    AeAlarmsDB.open(p).close()
    db = AeAlarmsDB.open(p)  # second open must not raise or re-duplicate rows
    try:
        alert_ids = {r["alert_id"] for r in db._conn.execute("SELECT alert_id FROM alerts")}
        assert alert_ids == {"active-1"}
        history_ids = {r["alert_id"] for r in db._conn.execute("SELECT alert_id FROM alert_history")}
        assert history_ids == {"closed-1", "ignored-1"}
    finally:
        db.close()


def test_fresh_db_has_alert_history_table(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        assert "alert_history" in _tables(db)
    finally:
        db.close()


def test_write_alert_with_closed_status_archives_immediately(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        db.write_alert({
            "alert_id": "a1", "metric_source": "gpu", "metric_name": "temp",
            "current_value": 90, "threshold_value": 85, "severity": "warning",
            "status": "closed", "created_at": "2026-01-01T00:00:00",
        })
        assert db.get_alert("a1") is None
        assert _history_alert(db, "a1") is not None
    finally:
        db.close()


def test_write_alert_with_active_status_stays_live(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        db.write_alert({
            "alert_id": "a1", "metric_source": "gpu", "metric_name": "temp",
            "current_value": 90, "threshold_value": 85, "severity": "warning",
            "status": "active", "created_at": "2026-01-01T00:00:00",
        })
        assert db.get_alert("a1") is not None
        assert _history_alert(db, "a1") is None
    finally:
        db.close()


def test_bulk_update_status_to_closed_archives_rows(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        db.write_alert({
            "alert_id": "a1", "metric_source": "gpu", "metric_name": "temp",
            "current_value": 90, "threshold_value": 85, "severity": "warning",
            "status": "active", "created_at": "2026-01-01T00:00:00",
        })
        n = db.bulk_update_status(from_statuses=("active", "acknowledged"), to_status="closed")
        assert n == 1
        assert db.get_alert("a1") is None
        assert _history_alert(db, "a1") is not None
    finally:
        db.close()


def test_delete_alert_deletes_from_history(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        db.write_alert({
            "alert_id": "a1", "metric_source": "gpu", "metric_name": "temp",
            "current_value": 90, "threshold_value": 85, "severity": "warning",
            "status": "closed", "created_at": "2026-01-01T00:00:00",
        })
        assert _history_alert(db, "a1") is not None
        deleted = db.delete_alert("a1")
        assert deleted is True
        assert _history_alert(db, "a1") is None
    finally:
        db.close()


def test_delete_alert_returns_false_when_missing(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        assert db.delete_alert("nope") is False
    finally:
        db.close()
