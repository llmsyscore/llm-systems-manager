"""Shared serialization helpers for the SQLite-backed alarm-engine stores.

Both `ae_settings_db` and `ae_alarms_db` write timestamps as ISO strings,
JSON-encode list / dict columns, and accept Enum-or-string values from the
model layer. Centralizing those conversions keeps the two stores' row
mappers honest.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def to_iso(v: Any) -> Optional[str]:
    """Coerce a datetime / string / None to ISO 8601, or None for empty input."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def to_json(v: Any, default: str = "[]") -> str:
    """JSON-encode lists/dicts. Pass-through strings unchanged so already-
    encoded values from elsewhere don't get double-encoded."""
    if v is None:
        return default
    if isinstance(v, str):
        return v
    return json.dumps(v, default=str)


def from_json(v: Any, default: Any) -> Any:
    """Decode a JSON-string column back into a list/dict. Already-decoded
    values (list/dict) pass through untouched so callers can call this
    defensively without round-tripping."""
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return default
    return default


def enum_value(v: Any, default: str = "") -> str:
    """Coerce an Enum to its `.value`, or stringify anything else.
    Status / channel_type / method values arrive from the model layer as
    enum instances; SQL needs canonical strings."""
    if v is None:
        return default
    if hasattr(v, "value"):
        return str(v.value)
    return str(v)


def open_sqlite(path: Path, schema_sql: str) -> sqlite3.Connection:
    """Open a SQLite file with the alarm-engine's standard pragmas + apply
    the caller's idempotent schema. WAL + NORMAL synchronous matches the
    write-rare / read-often pattern of all transactional alarm-engine
    state. `check_same_thread=False` because FastAPI handlers, the
    rule-eval loop, and background tasks all hit the same connection
    serialized by an RLock the caller owns."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path), check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(schema_sql)
    return conn


# Re-export RLock so callers don't need a second import line.
RLock = threading.RLock
