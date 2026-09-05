"""Ingest-surface authentication for the alarm engine.

The ingest routes (agent metric POSTs, OTLP /v1/*, external-alert ingest) are
reachable directly on host:8081, bypassing the manager proxy. A shared bearer
token gates them so only the manager + approved agents (which receive the token
on their heartbeat ack) can write.

Fail-open by design: when `[alarm_engine].ingest_token` is unset the ingest
surface stays OPEN, so deploying this code never breaks a running fleet — the
gate activates only once an operator provisions a token. The literal
"REPLACE_ME" is treated as unset too, so a half-rendered config can't enforce a
guessable token.
"""

import hmac
import ipaddress
from typing import Optional

from fastapi import Header, HTTPException

from config.unified_config import settings

_UNSET = {"", "REPLACE_ME"}


def _normalize_token(raw: Optional[str]) -> str:
    tok = (raw or "").strip()
    return "" if tok in _UNSET else tok


def _configured_token() -> str:
    return _normalize_token(settings.alarm_engine.ingest_token)


def _configured_management_token() -> str:
    return _normalize_token(getattr(settings.alarm_engine, "management_token", ""))


def _provided_bearer(authorization: Optional[str]) -> str:
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):].strip()
    return ""


def ingest_auth_active() -> bool:
    """True when a real ingest token is configured (the gate is enforcing)."""
    return bool(_configured_token())


def require_ingest_token(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency: enforce `Authorization: Bearer <ingest_token>`.

    No-op when no token is configured (open ingest). Constant-time compare so
    the check can't be timing-probed."""
    expected = _configured_token()
    if not expected:
        return
    provided = _provided_bearer(authorization)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="ingest authentication required")


def management_bearer_ok(authorization: Optional[str]) -> bool:
    """True when the Authorization header satisfies the management gate:
    `management_token`, else `ingest_token`; open when neither is set."""
    expected = _configured_management_token() or _configured_token()
    if not expected:
        return True
    provided = _provided_bearer(authorization)
    return bool(provided) and hmac.compare_digest(provided, expected)


def management_auth_active() -> bool:
    """True when a management or ingest token is configured (gate enforcing)."""
    return bool(_configured_management_token() or _configured_token())


def bind_is_loopback(host: Optional[str]) -> bool:
    """True when the bind host only reaches this machine."""
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def auth_state(authorization: Optional[str] = None) -> dict:
    """Which token each surface enforces, whether the bind is loopback-only, and
    whether a presented bearer matches (None when none presented or open)."""
    mgmt_tok = _configured_management_token()
    ingest_tok = _configured_token()
    mgmt = "management_token" if mgmt_tok else "ingest_token" if ingest_tok else "open"
    loopback = bind_is_loopback(settings.alarm_engine.host)
    presented = _provided_bearer(authorization)
    return {
        "management": mgmt,
        "ingest": "enforced" if ingest_tok else "open",
        "loopback_only": loopback,
        "open_on_network": mgmt == "open" and not loopback,
        "bearer_ok": (management_bearer_ok(authorization)
                      if presented and mgmt != "open" else None),
    }


AUTH_OPEN_WARNING = (
    "no management_token or ingest_token configured while bound to %s:%s — rules, "
    "alerts, notifications, metrics reads and dbstats are open to anyone on the "
    "network; set the SAME [alarm_engine].management_token on the engine and "
    "manager hosts and restart both")


def require_strict_management_token(
        authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency for the config surface: requires the dedicated
    management_token — no ingest-token fallback, fail-closed when unset."""
    expected = _configured_management_token()
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="management token required — set [alarm_engine].management_token")
    provided = _provided_bearer(authorization)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="management authentication required")


def require_management_token(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency for the management routes (rules/alerts/notifications):
    enforces `management_token`, else `ingest_token`; no-op when neither is set."""
    if not management_bearer_ok(authorization):
        raise HTTPException(status_code=401, detail="management authentication required")
