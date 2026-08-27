"""Size / pragma / row-count snapshot of an open SQLite DB (dbstats card)."""
from __future__ import annotations

import os
import sqlite3
import time


def scalar(conn: sqlite3.Connection, sql: str):
    try:
        r = conn.execute(sql).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def sqlite_stats(path: str, conn: sqlite3.Connection,
                 counts: dict[str, str]) -> dict:
    """Stats dict for `path`/`conn`; `counts` maps output key -> table name.
    Caller holds the connection lock."""
    out: dict = {"path": path}
    try:
        out["size_bytes"] = int(os.stat(path).st_size)
    except Exception:
        out["size_bytes"] = None
    for suffix, key in (("-wal", "wal_size_bytes"), ("-shm", "shm_size_bytes")):
        try:
            out[key] = int(os.stat(path + suffix).st_size)
        except Exception:
            out[key] = None
    out["page_size"] = scalar(conn, "PRAGMA page_size")
    out["page_count"] = scalar(conn, "PRAGMA page_count")
    out["journal_mode"] = scalar(conn, "PRAGMA journal_mode")
    out["cache_size"] = scalar(conn, "PRAGMA cache_size")
    t0 = time.perf_counter()
    scalar(conn, "SELECT 1")
    out["query_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    for key, table in counts.items():
        out[key] = scalar(conn, f"SELECT COUNT(*) FROM {table}")
    return out
