"""GPU Report Card (#468): standardized cross-provider bench, storage, routes."""
from __future__ import annotations

import json

# ── Storage ──────────────────────────────────────────────────────────
# One row per completed run; all runs retained so the table backs trending.

_COLS = "ts, agent_id, provider, mode, preset_version, eligible, result"


def init_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            mode TEXT NOT NULL,
            preset_version TEXT NOT NULL,
            eligible INTEGER NOT NULL,
            result TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_cards_lookup "
                 "ON report_cards(agent_id, provider, ts)")
    conn.commit()


def insert_card(conn, card: dict) -> int:
    cur = conn.execute(
        "INSERT INTO report_cards (ts, agent_id, provider, mode, preset_version,"
        " eligible, result) VALUES (?,?,?,?,?,?,?)",
        (int(card["ts"]), card["agent_id"], card["provider"], card["mode"],
         card["preset_version"], 1 if card["eligible"] else 0,
         json.dumps(card["result"])))
    conn.commit()
    return cur.lastrowid


def _row_to_card(row) -> dict:
    return {"ts": row[0], "agent_id": row[1], "provider": row[2], "mode": row[3],
            "preset_version": row[4], "eligible": bool(row[5]),
            "result": json.loads(row[6])}


def latest_card(conn, agent_id: str, provider: str) -> "dict | None":
    row = conn.execute(
        f"SELECT {_COLS} FROM report_cards WHERE agent_id=? AND provider=?"
        " ORDER BY ts DESC, id DESC LIMIT 1", (agent_id, provider)).fetchone()
    return _row_to_card(row) if row else None


def history(conn, agent_id: str, provider: str, model: str) -> "list[dict]":
    rows = conn.execute(
        f"SELECT {_COLS} FROM report_cards WHERE agent_id=? AND provider=?"
        " ORDER BY ts ASC, id ASC", (agent_id, provider)).fetchall()
    return [c for c in map(_row_to_card, rows)
            if c["result"].get("model") == model]
