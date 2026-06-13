"""
Naive UTC `now()` — replaces `datetime.utcnow()` without changing semantics.

`datetime.utcnow()` returns a naive datetime in UTC and is scheduled for
removal in a future Python release (Python 3.12+ emits a DeprecationWarning).
The recommended `datetime.now(timezone.utc)` returns a tz-aware datetime,
which is NOT safely interchangeable with the rest of this codebase — many
existing comparisons, SQLite TEXT serializations, and Pydantic model fields
assume the naive shape `utcnow()` produced.

`now_utc()` keeps the value naive while computing it via the non-deprecated
API, so callsites that swap `datetime.utcnow()` → `now_utc()` see identical
behavior.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return the current UTC time as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
