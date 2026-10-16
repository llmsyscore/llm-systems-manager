"""manager.db rename + audit/energy split migrations (#1036, #1037); legacy merge (#1289)."""
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


def _bench_schema(conn):
    conn.execute("CREATE TABLE model_benchmarks (id INTEGER PRIMARY KEY, model_id TEXT NOT NULL,"
                 " agent_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT 'llama',"
                 " avg_gen_tps REAL, ts TEXT, UNIQUE(model_id, agent_id, provider))")
    conn.execute("CREATE TABLE tool_runs (id INTEGER PRIMARY KEY, tool TEXT NOT NULL, model_id TEXT NOT NULL,"
                 " agent_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT 'llama',"
                 " ok INTEGER NOT NULL DEFAULT 1, summary TEXT, ts TEXT NOT NULL, run_id TEXT)")
    conn.execute("CREATE UNIQUE INDEX idx_tool_runs_run ON tool_runs(run_id, model_id) WHERE run_id IS NOT NULL")
    conn.commit()


def test_rename_replaces_an_empty_target(tmp_path):
    # A stray zero-byte manager.db (the sqlite3 CLI creates one) must not block the rename (#1289).
    (tmp_path / "metrics.db").write_bytes(b"old")
    main = tmp_path / "manager.db"
    main.write_bytes(b"")
    (tmp_path / "manager.db-wal").write_bytes(b"w")
    assert manager_db.rename_legacy_file(main) is True
    assert main.read_bytes() == b"old" and not (tmp_path / "metrics.db").exists()
    assert (tmp_path / "manager.db.empty").exists() and (tmp_path / "manager.db.empty-wal").read_bytes() == b"w"
    assert not (tmp_path / "manager.db-wal").exists()


def test_rename_replaces_a_schema_only_target(tmp_path):
    (tmp_path / "metrics.db").write_bytes(b"old")
    main = tmp_path / "manager.db"
    c = sqlite3.connect(main); _bench_schema(c); c.close()
    assert manager_db.rename_legacy_file(main) is True
    assert main.read_bytes() == b"old" and (tmp_path / "manager.db.empty").exists()


def test_rename_keeps_a_target_that_holds_rows(tmp_path):
    (tmp_path / "metrics.db").write_bytes(b"old")
    main = tmp_path / "manager.db"
    c = sqlite3.connect(main); _bench_schema(c)
    c.execute("INSERT INTO tool_runs (tool, model_id, ts) VALUES ('benchmark', 'm', 't')"); c.commit(); c.close()
    assert manager_db.rename_legacy_file(main) is False
    assert (tmp_path / "metrics.db").exists() and not (tmp_path / "manager.db.empty").exists()


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


# ── merge_legacy_file (#1289) ────────────────────────────────────────────────

def _legacy_paths(tmp_path):
    paths = {"manager": tmp_path / "manager.db", "audit": tmp_path / "audit.db", "energy": tmp_path / "energy.db"}
    m = sqlite3.connect(paths["manager"]); _bench_schema(m)
    import report_card
    report_card.init_table(m)
    m.execute("INSERT INTO model_benchmarks (model_id, avg_gen_tps, ts) VALUES ('shared', 9.0, 'new')")
    m.execute("INSERT INTO tool_runs (tool, model_id, ts) VALUES ('benchmark', 'shared', 'new')")
    m.commit(); m.close()
    a = sqlite3.connect(paths["audit"]); _audit_schema(a)
    a.execute("INSERT INTO audit_log (ts, actor, action) VALUES ('2026-10-01T00:00:00+00:00', 'new', 'x')")
    a.commit(); a.close()
    e = sqlite3.connect(paths["energy"]); energy.init_table(e)
    e.execute("INSERT INTO energy_hourly (hour_ts, agent_id, energy_wh) VALUES (200, 'a', 2.0)"); e.commit(); e.close()
    return paths


def _legacy_file(tmp_path, extra_table=False):
    legacy = tmp_path / "metrics.db"
    c = sqlite3.connect(legacy); _bench_schema(c); _audit_schema(c); energy.init_table(c)
    import report_card
    report_card.init_table(c)
    c.executemany("INSERT INTO model_benchmarks (id, model_id, avg_gen_tps, ts) VALUES (?,?,?,?)",
                  [(1, "shared", 1.0, "old"), (2, "only-old", 2.0, "old")])
    c.executemany("INSERT INTO tool_runs (id, tool, model_id, ts) VALUES (?,?,?,?)",
                  [(1, "benchmark", "shared", "old"), (2, "autotune", "only-old", "old")])
    c.execute("INSERT INTO report_cards (ts, agent_id, provider, mode, preset_version, eligible, result)"
              " VALUES (1, 'a', 'llama', 'm', 'v1', 1, '{}')")
    c.executemany("INSERT INTO audit_log (id, ts, actor, action) VALUES (?,?,?,?)",
                  [(1, "2026-08-01T00:00:00+00:00", "old", "x"), (2, "2026-08-02T00:00:00+00:00", "old", "y")])
    c.executemany("INSERT INTO energy_hourly (hour_ts, agent_id, energy_wh) VALUES (?,?,?)",
                  [(100, "a", 1.0), (200, "a", 5.0)])
    if extra_table:
        c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT)")
        c.execute("INSERT INTO jobs (kind) VALUES ('x')")
    c.commit(); c.close()
    return legacy


def test_merge_legacy_file_carries_missing_rows_and_moves_the_file_aside(tmp_path, caplog):
    paths = _legacy_paths(tmp_path)
    legacy = _legacy_file(tmp_path)
    with caplog.at_level("INFO"):
        assert manager_db.merge_legacy_file(paths) is True
    assert not legacy.exists()
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("metrics.db.merged-")]
    assert len(aside) == 1
    m = sqlite3.connect(paths["manager"])
    # Existing rows win; only the legacy rows the new file lacks come over, ids renumbered.
    assert m.execute("SELECT model_id, avg_gen_tps FROM model_benchmarks ORDER BY id").fetchall() == \
        [("shared", 9.0), ("only-old", 2.0)]
    assert m.execute("SELECT tool, model_id, ts FROM tool_runs ORDER BY id").fetchall() == \
        [("benchmark", "shared", "new"), ("benchmark", "shared", "old"), ("autotune", "only-old", "old")]
    assert m.execute("SELECT COUNT(*) FROM report_cards").fetchone()[0] == 1
    a = sqlite3.connect(paths["audit"])
    assert [r[0] for r in a.execute("SELECT actor FROM audit_log ORDER BY id")] == ["new", "old", "old"]
    e = sqlite3.connect(paths["energy"])
    assert e.execute("SELECT hour_ts, energy_wh FROM energy_hourly ORDER BY hour_ts").fetchall() == \
        [(100, 1.0), (200, 2.0)]
    assert "merged legacy" in caplog.text
    # Nothing left to do on the next boot.
    assert manager_db.merge_legacy_file(paths) is False


def test_merge_legacy_file_is_idempotent_after_a_crash_before_the_move(tmp_path):
    paths = _legacy_paths(tmp_path)
    _legacy_file(tmp_path)
    assert manager_db.merge_legacy_file(paths) is True
    _legacy_file(tmp_path)        # same legacy content shows up again
    assert manager_db.merge_legacy_file(paths) is True
    m = sqlite3.connect(paths["manager"])
    assert m.execute("SELECT COUNT(*) FROM model_benchmarks").fetchone()[0] == 2
    assert m.execute("SELECT COUNT(*) FROM tool_runs").fetchone()[0] == 3
    assert sqlite3.connect(paths["audit"]).execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 3


def test_merge_legacy_file_keeps_unknown_tables_in_the_aside_file(tmp_path, caplog):
    paths = _legacy_paths(tmp_path)
    _legacy_file(tmp_path, extra_table=True)
    with caplog.at_level("WARNING"):
        assert manager_db.merge_legacy_file(paths) is True
    assert "jobs" in caplog.text
    aside = next(p for p in tmp_path.iterdir() if p.name.startswith("metrics.db.merged-"))
    c = sqlite3.connect(aside)
    assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='audit_log'").fetchone() is None


def test_merge_legacy_file_leaves_the_file_for_the_next_boot_on_failure(tmp_path, caplog):
    paths = _legacy_paths(tmp_path)
    legacy = _legacy_file(tmp_path)
    paths["audit"].unlink(); sqlite3.connect(paths["audit"]).close()     # no audit_log table
    with caplog.at_level("ERROR"):
        assert manager_db.merge_legacy_file(paths) is False
    assert legacy.exists() and "merge of audit_log" in caplog.text
    assert sqlite3.connect(legacy).execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2


def test_merge_legacy_file_skips_when_no_legacy_or_main_is_metrics_db(tmp_path):
    paths = _legacy_paths(tmp_path)
    assert manager_db.merge_legacy_file(paths) is False
    _legacy_file(tmp_path)
    assert manager_db.merge_legacy_file({**paths, "manager": tmp_path / "metrics.db"}) is False
    assert (tmp_path / "metrics.db").exists()


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


def test_init_db_merges_a_leftover_legacy_file(caplog):
    # End-to-end wiring (#1289): a metrics.db beside the live files is merged by init_db.
    legacy = M.DB_PATH.parent / "metrics.db"
    c = sqlite3.connect(legacy); _bench_schema(c)
    c.execute("INSERT INTO model_benchmarks (model_id, agent_id, avg_gen_tps, ts) "
              "VALUES ('legacy-1289', 'ag', 3.5, 'old')")
    c.commit(); c.close()
    with caplog.at_level("INFO"):
        M.init_db()
    assert not legacy.exists() and "merged legacy" in caplog.text
    row = M.get_db().execute("SELECT avg_gen_tps FROM model_benchmarks WHERE model_id='legacy-1289'").fetchone()
    assert row is not None and row[0] == 3.5
    for p in M.DB_PATH.parent.glob("metrics.db.merged-*"):
        p.unlink()


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
