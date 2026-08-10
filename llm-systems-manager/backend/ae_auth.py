"""Effective alarm-engine bearer, shared by main, proxies and companion (#519).

Mirrors the AE's own gate (backend/api/auth.py): management_token wins,
ingest_token is the fallback, and ""/"REPLACE_ME" count as unset.
"""

from __future__ import annotations

from typing import Any

_UNSET = ("", "REPLACE_ME")


def effective_bearer(settings: Any) -> str:
    """management_token, else ingest_token; "" when neither is real."""
    ae = getattr(settings, "alarm_engine", None)
    for raw in (getattr(ae, "management_token", ""),
                getattr(ae, "ingest_token", "")):
        tok = (raw or "").strip()
        if tok not in _UNSET:
            return tok
    return ""
