"""#1273: System Health Recent column — flood audit rows, recent events, process figures."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import auth
import manager_mod as M


@pytest.fixture
def audit_db(monkeypatch):
    """A private in-memory audit_log; get_audit_db returns it for every caller."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, role TEXT,
            ip TEXT, auth TEXT, method TEXT, path TEXT, action TEXT, target TEXT,
            status INTEGER, outcome TEXT, detail TEXT, event TEXT)
    """)
    monkeypatch.setattr(M, "get_audit_db", lambda: conn)
    return conn


@pytest.fixture
def budget(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(auth, "_anon_budget_limit", lambda: 3)
    monkeypatch.setattr(auth, "_ANON_BUDGET", auth._AnonBudget(now=lambda: clock["now"]))
    monkeypatch.setattr(auth, "on_window_refused", M._audit_flood)
    return clock


def _flood_rows(conn):
    return conn.execute("SELECT * FROM audit_log WHERE action='security.flood'").fetchall()


class TestFloodRow:
    def test_a_refused_window_writes_one_flood_row_when_it_rolls(self, audit_db, budget):
        b = auth._ANON_BUDGET
        for _ in range(5):
            b.allow()
        assert _flood_rows(audit_db) == []
        budget["now"] += 60
        b.stats()
        rows = _flood_rows(audit_db)
        assert len(rows) == 1
        r = rows[0]
        assert r["event"] == "security.flood" and r["outcome"] == "denied" and r["status"] == 429
        assert r["auth"] == "internal" and (r["actor"] or "") == ""
        assert json.loads(r["detail"]) == {"requests": 5, "refused": 2, "budget": 3}

    def test_a_quiet_window_writes_nothing(self, audit_db, budget):
        b = auth._ANON_BUDGET
        b.allow(); b.allow()
        budget["now"] += 60
        b.stats()
        assert _flood_rows(audit_db) == []

    def test_a_disabled_event_writes_nothing(self, audit_db, budget, monkeypatch):
        monkeypatch.setitem(M._AUDIT_CFG, "disabled", {"security.flood"})
        b = auth._ANON_BUDGET
        for _ in range(5):
            b.allow()
        budget["now"] += 60
        b.allow()
        assert _flood_rows(audit_db) == []

    def test_the_event_is_in_the_catalog(self):
        keys = {ev["key"] for g in M.AUDIT_EVENT_GROUPS for ev in g["events"]}
        assert "security.flood" in keys
        assert M._audit_label("security.flood") == "Request flood on the open routes"
        assert M._audit_event_for("security.flood") == "security.flood"


@pytest.fixture
def admin(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    monkeypatch.setattr(M, "_alarm_engine_url", "")
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: {"agents": {}, "global": {}})
    monkeypatch.setattr(M, "_custom_manager_tls_files", lambda quiet=True: None)
    monkeypatch.setattr(M, "DATA_DIR", tmp_path)


def _seed(conn, rows):
    """rows: (minutes_ago, actor, auth, action, target, outcome, detail_dict|None)"""
    now = datetime.now(timezone.utc)
    for mins, actor, authk, action, target, outcome, detail in rows:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, role, ip, auth, method, path, action, target, status, outcome, detail, event)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((now - timedelta(minutes=mins)).isoformat(timespec="seconds"), actor, "admin", "10.0.0.5", authk,
             "POST", "/x", action, target, 200 if outcome == "ok" else 500, outcome,
             json.dumps(detail) if isinstance(detail, dict) else detail, M._audit_event_for(action)))
    conn.commit()


class TestRecentEvents:
    def test_newest_six_operations_rows_grouped_and_filtered(self, audit_db, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        _seed(audit_db, [(1 + i, "llmadmin", "session", "agent.self-update", f"id{i}", "ok", {"agent": f"host{i}"}) for i in range(8)]
              + [(3, "llmadmin", "session", "auth.login", "llmadmin", "ok", None),
                 (12, "llmadmin", "session", "config.settings", None, "ok", None),
                 (15, "", "test", "service.restart", "manager", "ok", None),
                 (30, "llmadmin", "session", "service.restart", "alarm_engine", "ok", None),
                 (31, "llmadmin", "session", "service.restart", "manager", "ok", None),
                 (40, "", "internal", "security.flood", None, "denied", {"requests": 3210, "refused": 2610, "budget": 600}),
                 (50, "jobs", "internal", "jobs.failed", "Backup now", "error", {"label": "Backup now", "message": "destination not writable"}),
                 (60, "llmadmin", "session", "backup.run", None, "ok", None),
                 (70, "llmadmin", "session", "forecast.run", None, "ok", None),
                 (25 * 60, "llmadmin", "session", "service.restart", "manager", "ok", None)])
        rows = M._recent_events()
        assert [r["title"] for r in rows] == [
            "8 agents updated", "Alarm Engine restarted", "Manager restarted",
            "Request flood on the open routes", "Job failed", "Backup ran"]
        assert rows[0]["k"] == "ok" and rows[0]["detail"] == "" and rows[0]["actor"] == "llmadmin"
        assert rows[3]["k"] == "warn" and rows[3]["detail"] == "3210/min, 2610 refused" and rows[3]["actor"] == ""
        assert rows[4]["k"] == "warn" and rows[4]["detail"] == "Backup now — destination not writable"
        assert all(r["ts"].endswith("+00:00") for r in rows)


    def test_automated_users_are_skipped(self, audit_db, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        monkeypatch.setitem(M._AUDIT_CFG, "automated_actors", ["smoketestuser"])
        _seed(audit_db, [(1, "smoketestuser", "session", "service.restart", "manager", "ok", None),
                         (2, "llmadmin", "session", "service.restart", "alarm_engine", "ok", None)])
        assert [r["title"] for r in M._recent_events()] == ["Alarm Engine restarted"]

    def test_anonymous_denied_attempts_are_skipped(self, audit_db, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        _seed(audit_db, [(1, "", "bypass", "backup.export", None, "denied", None),
                         (2, "", "", "agent.self-update", "00000000", "denied", {"agent": "00000000"}),
                         (3, "bob", "session", "backup.export", None, "denied", None),
                         (4, "", "internal", "security.flood", None, "denied", {"requests": 9, "refused": 3, "budget": 6})])
        rows = M._recent_events()
        assert [r["title"] for r in rows] == ["Backup exported failed", "Request flood on the open routes"]

    def test_a_gap_over_ten_minutes_splits_a_group(self, audit_db, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        _seed(audit_db, [(1, "a", "session", "agent.self-update", "x", "ok", {"agent": "h1"}),
                         (2, "a", "session", "agent.self-update", "y", "ok", {"agent": "h2"}),
                         (20, "a", "session", "agent.self-update", "z", "ok", {"agent": "h3"})])
        rows = M._recent_events()
        assert [r["title"] for r in rows] == ["2 agents updated", "Agent updated"]
        assert rows[1]["detail"] == "h3"

    def test_repeated_job_failures_read_in_plain_english(self, audit_db, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        _seed(audit_db, [(1, "a", "session", "jobs.failed", None, "error", None),
                         (4, "a", "session", "jobs.failed", None, "error", None)])
        r = M._recent_events()[0]
        assert r["title"] == "2 jobs failed" and r["detail"] == ""

    def test_a_denied_operation_reads_failed(self, audit_db, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        _seed(audit_db, [(1, "bob", "session", "service.restart", "manager", "denied", None)])
        r = M._recent_events()[0]
        assert r["title"] == "Manager restarted failed" and r["k"] == "warn"

    def test_recent_tolerates_bad_detail(self, audit_db, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        _seed(audit_db, [(1, "a", "session", "agent.restart", "x", "ok", "{not json"),
                         (2, "a", "session", "jobs.failed", None, "error", None)])
        rows = M._recent_events()
        assert [r["title"] for r in rows] == ["Agent restarted", "Job failed"]
        assert rows[0]["detail"] == "x" and rows[1]["detail"] == ""

    def test_recent_survives_a_broken_audit_db(self, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        def boom():
            raise sqlite3.OperationalError("locked")
        monkeypatch.setattr(M, "get_audit_db", boom)
        assert M._recent_events() == []

    def test_the_cache_holds_for_five_seconds(self, audit_db, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        _seed(audit_db, [(1, "a", "session", "backup.run", None, "ok", None)])
        assert len(M._recent_events()) == 1
        _seed(audit_db, [(0, "a", "session", "forecast.run", None, "ok", None)])
        assert len(M._recent_events()) == 1
        M._RECENT_CACHE["at"] = 0.0
        assert len(M._recent_events()) == 2


class TestHealthPayload:
    def test_health_carries_recent_process_hostname_and_budget(self, audit_db, admin, monkeypatch):
        monkeypatch.setattr(M, "_RECENT_CACHE", {"at": 0.0, "rows": []})
        monkeypatch.setattr(auth, "_anon_budget_limit", lambda: 600)
        _seed(audit_db, [(1, "llmadmin", "session", "service.restart", "manager", "ok", None)])
        with M.app.test_client() as c:
            with c.session_transaction() as s:
                s["auth_ok"], s["role"] = True, "admin"
            d = c.get("/api/admin/system-health").get_json()
        assert d["recent"][0]["title"] == "Manager restarted"
        assert set(d["manager"]["process"]) == {"cpu_pct", "rss_mb", "ram_pct"}
        assert d["manager"]["process"]["rss_mb"] > 0
        assert isinstance(d["manager"]["hostname"], str) and d["manager"]["hostname"]
        assert d["manager"]["ram_total_mb"] > 0
        assert d["flow"]["anon_budget_per_min"] == 600

    def test_a_psutil_failure_leaves_process_none(self, audit_db, admin, monkeypatch):
        def boom():
            raise RuntimeError("no psutil")
        monkeypatch.setattr(M, "_self_process_stats", boom)
        with M.app.test_client() as c:
            with c.session_transaction() as s:
                s["auth_ok"], s["role"] = True, "admin"
            d = c.get("/api/admin/system-health").get_json()
        assert d["manager"]["process"] is None
