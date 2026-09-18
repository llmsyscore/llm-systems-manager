"""Manager SQLite layout: manager.db (small tables), audit.db (audit_log),
energy.db (energy_hourly); boot migrations for the legacy metrics.db (#1036, #1037)."""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("llm-systems-manager.db")

LEGACY_NAME = "metrics.db"
MANAGER_NAME = "manager.db"
AUDIT_NAME = "audit.db"
ENERGY_NAME = "energy.db"
# Sidecars move first; the main file moves last so its presence marks completion.
SIDECARS = ("-wal", "-shm", "-journal", "")

# Archive entry names, current and legacy (pre-rename archives restore as manager.db).
EXPORT_ENTRIES = (f"data/{MANAGER_NAME}", f"data/{AUDIT_NAME}", f"data/{ENERGY_NAME}")
LEGACY_EXPORT_ENTRY = f"data/{LEGACY_NAME}"

ENERGY_KEY = ("hour_ts", "agent_id")
AUDIT_SAME = ("ts", "actor", "action", "target")


def resolve_paths(data_dir: Path, env=None) -> dict[str, Path]:
    """Paths for manager/audit/energy; LLMSYS_MANAGER_DB (alias LLMSYS_METRICS_DB)
    moves the main file, LLMSYS_AUDIT_DB / LLMSYS_ENERGY_DB default beside it."""
    env = os.environ if env is None else env
    main = Path(env.get("LLMSYS_MANAGER_DB") or env.get("LLMSYS_METRICS_DB")
                or Path(data_dir) / MANAGER_NAME)
    audit = Path(env.get("LLMSYS_AUDIT_DB") or main.parent / AUDIT_NAME)
    energy = Path(env.get("LLMSYS_ENERGY_DB") or main.parent / ENERGY_NAME)
    return {"manager": main, "audit": audit, "energy": energy}


def rename_legacy_file(main: Path) -> bool:
    """Rename metrics.db and its sidecars to `main` when `main` is absent."""
    legacy = main.parent / LEGACY_NAME
    if main.name == LEGACY_NAME or main.exists() or not legacy.exists():
        return False
    moved = []
    for suffix in SIDECARS:
        src = legacy.with_name(legacy.name + suffix)
        if src.exists():
            os.replace(src, main.with_name(main.name + suffix))
            moved.append(src.name)
    log.info("renamed %s -> %s (moved: %s)", legacy, main, ", ".join(moved))
    return True


def _table_exists(conn: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()]


def split_table(main_path: Path, dst_path: Path, table: str, *, key: tuple[str, ...],
                same: tuple[str, ...] = ()) -> int | None:
    """Move `table` rows from main_path into dst_path (table must exist there), then
    drop the source; None when already done. Keyed upsert, or append when `same` clashes."""
    if not main_path.exists():
        return None
    conn = sqlite3.connect(str(main_path), timeout=30.0, isolation_level=None)
    try:
        if not _table_exists(conn, table):
            return None
        conn.execute("ATTACH DATABASE ? AS dst", (str(dst_path),))
        if not _table_exists(conn, table, "dst"):
            raise RuntimeError(f"{dst_path} has no {table} table")
        dst_cols = set(_columns(conn, table, "dst"))
        cols = [c for c in _columns(conn, table) if c in dst_cols]
        n = conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
        match = " AND ".join(f"d.{k} = m.{k}" for k in key)
        keep_key = True
        if same:
            # Rows whose key already exists in dst with different content force append mode.
            differ = " OR ".join(f"d.{c} IS NOT m.{c}" for c in same if c in cols)
            clash = conn.execute(f"SELECT COUNT(*) FROM main.{table} m JOIN dst.{table} d "
                                 f"ON {match} WHERE {differ}").fetchone()[0]
            keep_key = clash == 0
        if not keep_key:
            cols = [c for c in cols if c not in key]
        col_sql = ", ".join(cols)
        conn.execute("BEGIN")
        verb = "INSERT OR REPLACE" if keep_key else "INSERT"
        cur = conn.execute(f"{verb} INTO dst.{table} ({col_sql}) "
                           f"SELECT {col_sql} FROM main.{table} ORDER BY rowid")
        if keep_key:
            missing = conn.execute(
                f"SELECT COUNT(*) FROM main.{table} m WHERE NOT EXISTS "
                f"(SELECT 1 FROM dst.{table} d WHERE {match})").fetchone()[0]
            ok = missing == 0
        else:
            ok = cur.rowcount == n
        if not ok:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"{table}: copied rows do not match the source ({n} rows)")
        conn.execute(f"DROP TABLE main.{table}")
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE dst")
        log.info("moved %d %s rows from %s to %s%s", n, table, main_path.name, dst_path.name,
                 "" if keep_key else " (appended; keys clashed)")
        return n
    finally:
        conn.close()


def _scalar(conn: sqlite3.Connection, sql: str):
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def sqlite_stats(path: Path, conn: sqlite3.Connection, counts: dict[str, str]) -> dict:
    """Size / WAL / pragma / row-count snapshot of one file for the dashboard card."""
    out: dict = {"file": path.name}
    for suffix, key in (("", "size_bytes"), ("-wal", "wal_size_bytes"), ("-shm", "shm_size_bytes")):
        try:
            out[key] = int(os.stat(str(path) + suffix).st_size)
        except OSError:
            out[key] = None
    out["page_size"] = _scalar(conn, "PRAGMA page_size")
    out["page_count"] = _scalar(conn, "PRAGMA page_count")
    out["journal_mode"] = _scalar(conn, "PRAGMA journal_mode")
    t0 = time.perf_counter()
    _scalar(conn, "SELECT 1")
    out["query_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    for key, table in counts.items():
        out[key] = _scalar(conn, f"SELECT COUNT(*) FROM {table}")
    return out


def split_write_heavy_tables(paths: dict[str, Path]) -> None:
    """Run both splits; a failure is logged and leaves that source table for the next boot."""
    jobs = (("audit_log", paths["audit"], {"key": ("id",), "same": AUDIT_SAME}),
            ("energy_hourly", paths["energy"], {"key": ENERGY_KEY}))
    moved = False
    for table, dst, kw in jobs:
        try:
            if split_table(paths["manager"], dst, table, **kw) is not None:
                moved = True
        except (sqlite3.Error, RuntimeError, OSError):
            log.exception("split of %s into %s failed; will retry next boot", table, dst)
    if moved:
        try:
            conn = sqlite3.connect(str(paths["manager"]), timeout=30.0)
            try:
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.Error:
            log.warning("VACUUM of %s after the split failed", paths["manager"], exc_info=True)
