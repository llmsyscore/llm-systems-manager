"""#215: additive column migrations on pre-existing v1 databases."""
import sqlite3

from backend.storage.ae_alarms_db import _ALERT_COLUMNS, AeAlarmsDB
from backend.storage.ae_settings_db import AeSettingsDB

# Byte-faithful v1 alerts schema (pre-#215) to migrate from.
_V1_ALERTS = """
CREATE TABLE alerts (
  alert_id TEXT PRIMARY KEY, rule_id TEXT, rule_name TEXT,
  metric_source TEXT NOT NULL, metric_name TEXT NOT NULL, source_host TEXT,
  current_value REAL NOT NULL, threshold_value REAL NOT NULL,
  severity TEXT NOT NULL, status TEXT NOT NULL, message TEXT,
  trigger_count INTEGER NOT NULL DEFAULT 1, acknowledged_by TEXT,
  exception_details TEXT, resolution_reason TEXT, resolved_value REAL,
  notification_channel_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, last_evaluated_at TEXT, acknowledged_at TEXT,
  closed_at TEXT, chart_window_start TEXT, chart_window_end TEXT
);
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1);
"""

# Byte-faithful v1 rules schema (pre-#215) to migrate from.
_V1_RULES = """
CREATE TABLE rules (
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
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1);
"""


def _make_v1(path):
    conn = sqlite3.connect(path)
    conn.executescript(_V1_ALERTS)
    conn.execute(
        "INSERT INTO alerts (alert_id, metric_source, metric_name, current_value,"
        " threshold_value, severity, status, created_at)"
        " VALUES ('old-1','gpu','temp',90,85,'warning','closed','2026-01-01T00:00:00')")
    conn.commit()
    conn.close()


def _make_v1_rules(path):
    conn = sqlite3.connect(path)
    conn.executescript(_V1_RULES)
    conn.execute(
        "INSERT INTO rules (rule_id, name, metric_source, metric_name, rule_type,"
        " severity, created_at, updated_at)"
        " VALUES ('old-rule-1','Old Rule','gpu','temp','threshold',"
        "'warning','2026-01-01T00:00:00','2026-01-01T00:00:00')")
    conn.commit()
    conn.close()


def test_v1_alerts_db_gains_incident_id(tmp_path):
    p = tmp_path / "ae_alarms.db"
    _make_v1(p)
    db = AeAlarmsDB.open(p)
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(alerts)")}
        assert "incident_id" in cols
        old = db.get_alert("old-1")
        assert old is not None and old["incident_id"] is None
    finally:
        db.close()


def test_migration_is_idempotent(tmp_path):
    p = tmp_path / "ae_alarms.db"
    _make_v1(p)
    AeAlarmsDB.open(p).close()
    db = AeAlarmsDB.open(p)  # second open must not raise on duplicate column
    db.close()


def test_fresh_db_has_incident_id(tmp_path):
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(alerts)")}
        assert "incident_id" in cols
    finally:
        db.close()


def test_v1_rules_db_gains_correlation_group(tmp_path):
    p = tmp_path / "ae_settings.db"
    _make_v1_rules(p)
    db = AeSettingsDB.open(p)
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(rules)")}
        assert "correlation_group" in cols
        rules = db.query_rules()
        old = next(r for r in rules if r["rule_id"] == "old-rule-1")
        assert old["correlation_group"] is None
    finally:
        db.close()


def test_rules_migration_is_idempotent(tmp_path):
    p = tmp_path / "ae_settings.db"
    _make_v1_rules(p)
    AeSettingsDB.open(p).close()
    db = AeSettingsDB.open(p)  # second open must not raise on duplicate column
    db.close()


def test_fresh_settings_db_has_correlation_group(tmp_path):
    db = AeSettingsDB.open(tmp_path / "fresh_settings.db")
    try:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(rules)")}
        assert "correlation_group" in cols
    finally:
        db.close()


def _colnames(db, table):
    return {r[1] for r in db._conn.execute(f"PRAGMA table_info({table})")}


def test_alerts_and_alert_history_columns_match_on_fresh_db(tmp_path):
    """Pins the hand-copied alert_history DDL against drift from _ALERT_COLUMNS."""
    db = AeAlarmsDB.open(tmp_path / "fresh.db")
    try:
        expected = {c.strip() for c in _ALERT_COLUMNS.split(",")}
        assert _colnames(db, "alerts") == expected
        assert _colnames(db, "alert_history") == expected
    finally:
        db.close()


def test_alerts_and_alert_history_columns_match_on_migrated_v1_db(tmp_path):
    """Same guard on a migrated v1 fixture, where physical column order differs."""
    p = tmp_path / "ae_alarms.db"
    _make_v1(p)
    db = AeAlarmsDB.open(p)
    try:
        expected = {c.strip() for c in _ALERT_COLUMNS.split(",")}
        assert _colnames(db, "alerts") == expected
        assert _colnames(db, "alert_history") == expected
    finally:
        db.close()
