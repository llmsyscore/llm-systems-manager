"""#220: alert_history table, v3 migration + synchronous archive."""
import asyncio
import sqlite3
import uuid

from backend.models.alert import AlertStatus
from backend.storage.ae_alarms_db import AeAlarmsDB
from backend.storage.repositories import AlertRepository

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
    """Fetch a row from alert_history directly, bypassing get_alert's fallback."""
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
        alert_ids = {r["alert_id"] for r in db._conn.execute("SELECT alert_id FROM alerts")}
        assert "a1" not in alert_ids
        assert db.get_alert("a1") is not None  # Task 3: get_alert falls back to alert_history.
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
        alert_ids = {r["alert_id"] for r in db._conn.execute("SELECT alert_id FROM alerts")}
        assert "a1" not in alert_ids
        assert db.get_alert("a1") is not None  # Task 3: get_alert falls back to alert_history.
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


def _write(db, alert_id, status, created_at="2026-01-01T00:00:00", severity="warning"):
    db.write_alert({
        "alert_id": alert_id, "metric_source": "gpu", "metric_name": "temp",
        "current_value": 90, "threshold_value": 85, "severity": severity,
        "status": status, "created_at": created_at, "message": "test",
    })


# ── Task 3: dual-table reads ────────────────────────────────────────────


def test_get_alert_falls_back_to_history(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        _write(db, "c1", status="closed")
        row = db.get_alert("c1")
        assert row is not None
        assert row["alert_id"] == "c1"
        assert row["status"] == "closed"
    finally:
        db.close()


def test_query_filtered_live_only_false_spans_both_tables_ordered_and_limited(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        _write(db, "h-old", status="closed", created_at="2026-01-01T00:00:00")
        _write(db, "a-mid", status="active", created_at="2026-01-02T00:00:00")
        _write(db, "h-new", status="closed", created_at="2026-01-03T00:00:00")
        rows = db.query_filtered(live_only=False, limit=2)
        assert [r["alert_id"] for r in rows] == ["h-new", "a-mid"]
    finally:
        db.close()


def test_query_filtered_live_only_false_status_closed_returns_only_archived_rows(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        _write(db, "a1", status="active")
        _write(db, "c1", status="closed")
        _write(db, "i1", status="ignored")
        rows = db.query_filtered(live_only=False, status="closed")
        assert {r["alert_id"] for r in rows} == {"c1"}
    finally:
        db.close()


def test_count_by_status_and_severity_aggregates_both_tables(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        _write(db, "a1", status="active", severity="critical")
        _write(db, "a2", status="acknowledged", severity="warning")
        _write(db, "c1", status="closed", severity="critical")
        _write(db, "i1", status="ignored", severity="warning")
        counters = db.count_by_status_and_severity()
        assert counters["by_status"] == {"active": 1, "acknowledged": 1, "closed": 1, "ignored": 1}
        assert counters["by_severity"] == {"critical": 2, "warning": 2}
        assert counters["total"] == 4
    finally:
        db.close()


def test_list_alerts_status_closed_without_include_closed_returns_archived_rows(tmp_path):
    """The Alerts-tab Closed/Ignored dropdown fix (#220)."""
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        alert_id = str(uuid.uuid4())
        _write(db, alert_id, status="closed")
        repo = AlertRepository(alarms_db=db)
        alerts = asyncio.run(
            repo.list_alerts(status=AlertStatus.CLOSED, include_closed=False)
        )
        assert [str(a.alert_id) for a in alerts] == [alert_id]
        assert alerts[0].status == AlertStatus.CLOSED
    finally:
        db.close()


def test_list_alerts_include_closed_true_spans_both_tables(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        _write(db, str(uuid.uuid4()), status="active")
        _write(db, str(uuid.uuid4()), status="closed")
        repo = AlertRepository(alarms_db=db)
        alerts = asyncio.run(repo.list_alerts(include_closed=True, limit=100))
        assert len(alerts) == 2
    finally:
        db.close()
