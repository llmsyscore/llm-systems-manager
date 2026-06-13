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
import logging
from typing import Optional

from fastapi import Header, HTTPException

from config.unified_config import settings

logger = logging.getLogger(__name__)

_UNSET = {"", "REPLACE_ME"}


def _configured_token() -> str:
    tok = (settings.alarm_engine.ingest_token or "").strip()
    return "" if tok in _UNSET else tok


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
    provided = ""
    if authorization and authorization.startswith("Bearer "):
        provided = authorization[len("Bearer "):].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="ingest authentication required")
