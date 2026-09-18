"""manager.db rename + audit/energy split migrations (#1036, #1037)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import energy
import manager_db
import manager_mod as M
import pytest


def _audit_schema(conn):
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, role TEXT,"
                 " ip TEXT, method TEXT, path TEXT, action TEXT, target TEXT, status INTEGER, outcome TEXT,"
                 " auth TEXT, detail TEXT, event TEXT)")
    conn.commit()


# ── resolve_paths ────────────────────────────────────────────────────────────

def test_resolve_paths_defaults_beside_manager_db(tmp_path):
    p = manager_db.resolve_paths(tmp_path, env={})
    assert p == {"manager": tmp_path / "manager.db", "audit": tmp_path / "audit.db",
                 "energy": tmp_path / "energy.db"}


def test_resolve_paths_honours_legacy_env_alias(tmp_path):
    p = manager_db.resolve_paths(tmp_path, env={"LLMSYS_METRICS_DB": "/x/y/main.db"})
    assert p["manager"] == Path("/x/y/main.db")
    assert p["audit"] == Path("/x/y/audit.db")
    p = manager_db.resolve_paths(tmp_path, env={"LLMSYS_MANAGER_DB": "/a/m.db",
                                                "LLMSYS_METRICS_DB": "/x/y/main.db",
                                                "LLMSYS_ENERGY_DB": "/e/e.db"})
    assert (p["manager"], p["audit"], p["energy"]) == (Path("/a/m.db"), Path("/a/audit.db"), Path("/e/e.db"))


# ── rename_legacy_file ───────────────────────────────────────────────────────

def test_rename_moves_db_and_sidecars(tmp_path):
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"metrics.db{suffix}").write_bytes(b"x" + suffix.encode())
    main = tmp_path / "manager.db"
    assert manager_db.rename_legacy_file(main) is True
    assert not (tmp_path / "metrics.db").exists()
    assert not (tmp_path / "metrics.db-wal").exists()
    assert (tmp_path / "manager.db-wal").read_bytes() == b"x-wal"
    assert main.read_bytes() == b"x"


def test_rename_skips_when_target_exists_or_legacy_absent(tmp_path):
    main = tmp_path / "manager.db"
    assert manager_db.rename_legacy_file(main) is False
    (tmp_path / "metrics.db").write_bytes(b"old")
    main.write_bytes(b"new")
    assert manager_db.rename_legacy_file(main) is False
    assert (tmp_path / "metrics.db").exists() and main.read_bytes() == b"new"


def test_rename_resumes_after_a_partial_move(tmp_path):
    # A crash after the sidecars moved but before the main file did: the retry finishes it.
    (tmp_path / "metrics.db").write_bytes(b"db")
    (tmp_path / "manager.db-wal").write_bytes(b"wal")
    main = tmp_path / "manager.db"
    assert manager_db.rename_legacy_file(main) is True
    assert main.read_bytes() == b"db" and (tmp_path / "manager.db-wal").read_bytes() == b"wal"
    assert manager_db.SIDECARS[-1] == ""


def test_rename_skips_when_target_is_named_metrics_db(tmp_path):
    (tmp_path / "metrics.db").write_bytes(b"old")
    assert manager_db.rename_legacy_file(tmp_path / "metrics.db") is False


# ── split_table ──────────────────────────────────────────────────────────────

def _seed_audit(main, n):
    conn = sqlite3.connect(main)
    _audit_schema(conn)
    conn.executemany("INSERT INTO audit_log (ts, actor, action) VALUES (?,?,?)",
                     [(f"2026-09-01T00:00:{i:02d}+00:00", f"u{i}", "a") for i in range(n)])
    conn.commit(); conn.close()


def test_split_audit_appends_rows_and_drops_source(tmp_path, caplog):
    main, dst = tmp_path / "manager.db", tmp_path / "audit.db"
    _seed_audit(main, 5)
    d = sqlite3.connect(dst); _audit_schema(d)
    d.execute("INSERT INTO audit_log (ts, actor) VALUES ('2026-08-01T00:00:00+00:00', 'pre')"); d.commit(); d.close()
    assert manager_db.split_table(main, dst, "audit_log", key=("id",), same=manager_db.AUDIT_SAME) == 5
    d = sqlite3.connect(dst)
    rows = d.execute("SELECT id, actor FROM audit_log ORDER BY id").fetchall()
    assert [r[1] for r in rows] == ["pre", "u0", "u1", "u2", "u3", "u4"]
    assert [r[0] for r in rows] == list(range(1, 7))
    m = sqlite3.connect(main)
    assert m.execute("SELECT 1 FROM sqlite_master WHERE name='audit_log'").fetchone() is None
    # Idempotent: second run finds nothing to move.
    assert manager_db.split_table(main, dst, "audit_log", key=("id",), same=manager_db.AUDIT_SAME) is None


def test_split_audit_first_migration_keeps_ids_and_retry_is_idempotent(tmp_path):
    main, dst = tmp_path / "manager.db", tmp_path / "audit.db"
    _seed_audit(main, 4)
    m = sqlite3.connect(main); m.execute("DELETE FROM audit_log WHERE id = 2"); m.commit(); m.close()
    d = sqlite3.connect(dst); _audit_schema(d); d.close()
    assert manager_db.split_table(main, dst, "audit_log", key=("id",), same=manager_db.AUDIT_SAME) == 3
    d = sqlite3.connect(dst)
    assert [r[0] for r in d.execute("SELECT id FROM audit_log ORDER BY id")] == [1, 3, 4]
    # Crash between the dst commit and the source drop: the source table is still there.
    _seed_audit(main, 4)
    m = sqlite3.connect(main); m.execute("DELETE FROM audit_log WHERE id = 2"); m.commit(); m.close()
    assert manager_db.split_table(main, dst, "audit_log", key=("id",), same=manager_db.AUDIT_SAME) == 3
    assert d.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 3


def test_split_audit_tolerates_old_source_schema(tmp_path):
    main, dst = tmp_path / "manager.db", tmp_path / "audit.db"
    m = sqlite3.connect(main)
    m.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT)")
    m.execute("INSERT INTO audit_log (ts, actor, action) VALUES ('t', 'a', 'x')"); m.commit(); m.close()
    d = sqlite3.connect(dst); _audit_schema(d); d.close()
    assert manager_db.split_table(main, dst, "audit_log", key=("id",), same=manager_db.AUDIT_SAME) == 1
    d = sqlite3.connect(dst)
    assert d.execute("SELECT actor, event FROM audit_log").fetchone() == ("a", None)


def test_split_energy_source_wins_on_key_conflict(tmp_path):
    main, dst = tmp_path / "manager.db", tmp_path / "energy.db"
    m = sqlite3.connect(main); energy.init_table(m)
    m.executemany("INSERT INTO energy_hourly (hour_ts, agent_id, energy_wh) VALUES (?,?,?)",
                  [(100, "a", 5.0), (200, "a", 6.0)]); m.commit(); m.close()
    d = sqlite3.connect(dst); energy.init_table(d)
    d.executemany("INSERT INTO energy_hourly (hour_ts, agent_id, energy_wh) VALUES (?,?,?)",
                  [(100, "a", 1.0), (300, "b", 7.0)]); d.commit(); d.close()
    assert manager_db.split_table(main, dst, "energy_hourly", key=manager_db.ENERGY_KEY) == 2
    d = sqlite3.connect(dst)
    got = d.execute("SELECT hour_ts, agent_id, energy_wh FROM energy_hourly ORDER BY hour_ts").fetchall()
    assert got == [(100, "a", 5.0), (200, "a", 6.0), (300, "b", 7.0)]
    m = sqlite3.connect(main)
    assert m.execute("SELECT 1 FROM sqlite_master WHERE name='energy_hourly'").fetchone() is None


def test_split_missing_destination_table_leaves_source_intact(tmp_path):
    main, dst = tmp_path / "manager.db", tmp_path / "audit.db"
    _seed_audit(main, 3)
    sqlite3.connect(dst).close()
    with pytest.raises(RuntimeError):
        manager_db.split_table(main, dst, "audit_log", key=("id",), same=manager_db.AUDIT_SAME)
    m = sqlite3.connect(main)
    assert m.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 3


def test_split_write_heavy_tables_logs_and_continues_on_failure(tmp_path, caplog):
    main = tmp_path / "manager.db"
    _seed_audit(main, 2)
    m = sqlite3.connect(main); energy.init_table(m)
    m.execute("INSERT INTO energy_hourly (hour_ts, agent_id) VALUES (1, 'a')"); m.commit(); m.close()
    paths = {"manager": main, "audit": tmp_path / "audit.db", "energy": tmp_path / "energy.db"}
    sqlite3.connect(paths["audit"]).close()          # no audit_log table → that split fails
    e = sqlite3.connect(paths["energy"]); energy.init_table(e); e.close()
    with caplog.at_level("ERROR"):
        manager_db.split_write_heavy_tables(paths)
    assert "split of audit_log" in caplog.text
    m = sqlite3.connect(main)
    assert m.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2
    assert m.execute("SELECT 1 FROM sqlite_master WHERE name='energy_hourly'").fetchone() is None
    assert sqlite3.connect(paths["energy"]).execute("SELECT COUNT(*) FROM energy_hourly").fetchone()[0] == 1


# ── manager wiring ───────────────────────────────────────────────────────────

def test_manager_uses_three_files_and_audit_lives_apart():
    assert M.DB_PATH.name == "manager.db"
    assert M.AUDIT_DB_PATH == M.DB_PATH.parent / "audit.db"
    assert M.ENERGY_DB_PATH == M.DB_PATH.parent / "energy.db"
    assert M.get_audit_db().execute("SELECT 1 FROM sqlite_master WHERE name='audit_log'").fetchone()
    assert M.get_db().execute("SELECT 1 FROM sqlite_master WHERE name='audit_log'").fetchone() is None
    assert M.get_db().execute("SELECT 1 FROM sqlite_master WHERE name='energy_hourly'").fetchone() is None
    assert M._db_conn(M.ENERGY_DB_PATH).execute(
        "SELECT 1 FROM sqlite_master WHERE name='energy_hourly'").fetchone()


def test_export_lists_three_files_and_import_accepts_legacy_name(tmp_path, monkeypatch):
    assert M._MANAGER_EXPORT_SQLITE == ["data/manager.db", "data/audit.db", "data/energy.db"]
    for n in ("data/manager.db", "data/audit.db", "data/energy.db", "data/metrics.db"):
        assert M._file_category(n) == "config"
    monkeypatch.setattr(M, "_REPO_ROOT_PATH", tmp_path)
    result = M._import_apply_manager({"data/metrics.db": b"legacy", "manifest.json": b"{}"})
    assert [Path(w).name for w in result["written"]] == ["manager.db"]
    assert (tmp_path / "data" / "manager.db").read_bytes() == b"legacy"
    assert not (tmp_path / "data" / "metrics.db").exists()


def test_export_archive_carries_all_three_databases(tmp_path, monkeypatch):
    data = tmp_path / "data"; data.mkdir()
    for name in ("manager.db", "audit.db", "energy.db"):
        c = sqlite3.connect(data / name); c.execute("CREATE TABLE t (x)"); c.commit(); c.close()
    monkeypatch.setattr(M, "_REPO_ROOT_PATH", tmp_path)
    files = M._build_manager_archive()
    assert {"data/manager.db", "data/audit.db", "data/energy.db"} <= set(files)
    assert all(files[n].startswith(b"SQLite format 3") for n in manager_db.EXPORT_ENTRIES)


# ── Database Performance card feed ──────────────────────────────────────────

def _admin_client(monkeypatch):
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    M.app.config["TESTING"] = True
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def test_dbstats_route_reports_the_three_files(monkeypatch):
    M.get_audit_db().execute("INSERT INTO audit_log (ts, actor) VALUES ('2026-09-18T00:00:00+00:00', 'x')")
    M.get_audit_db().commit()
    d = _admin_client(monkeypatch).get("/api/admin/dbstats/sqlite").get_json()
    assert d["ok"] is True
    assert d["manager_db"]["file"] == "manager.db" and "/" not in d["manager_db"]["file"]
    assert d["audit_db"]["file"] == "audit.db" and d["audit_db"]["audit_rows"] >= 1
    assert d["energy_db"]["file"] == "energy.db" and d["energy_db"]["energy_rows"] >= 0
    for k in ("manager_db", "audit_db", "energy_db"):
        assert d[k]["journal_mode"] == "wal"
        assert d[k]["size_bytes"] > 0 and d[k]["query_ms"] >= 0
    assert d["manager_db"]["benchmarks"] >= 0 and d["manager_db"]["tool_runs"] >= 0


def test_dbstats_route_is_admin_gated(monkeypatch):
    from flask import jsonify
    monkeypatch.setattr(M, "_require_admin", lambda: (jsonify({"ok": False}), 403))
    M.app.config["TESTING"] = True
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "operator"
    assert c.get("/api/admin/dbstats/sqlite").status_code == 403
