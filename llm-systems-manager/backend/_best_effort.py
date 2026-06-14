"""best_effort: run a swallow-on-failure block, logging the swallowed error at debug."""
from __future__ import annotations

import logging
from contextlib import contextmanager

_fallback_logger = logging.getLogger("llm-systems-manager")


@contextmanager
def best_effort(what: str, log: logging.Logger | None = None):
    """Yield; on any exception log it at debug (with traceback) and continue."""
    try:
        yield
    except Exception:
        (log or _fallback_logger).debug("best-effort %s failed", what, exc_info=True)
