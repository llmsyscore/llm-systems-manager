"""Global cap on concurrent long-lived SSE streams.

Each held SSE response pins one synchronous Cheroot worker for the stream's
whole life. A healthy agent stream (keepalives ≤30s) is never reaped by the
proxied read timeout, so without a cap enough concurrent streams exhaust the
fixed worker pool and the manager stops answering control requests. This caps
concurrent streams below the pool size, reserving workers for non-stream
requests (/health, heartbeat, control). Over the cap → the caller returns a
503 and the browser EventSource retries.
"""
from __future__ import annotations

import logging
import threading
import time

from config.unified_config import settings  # type: ignore[import-not-found]

log = logging.getLogger("llm-systems-manager.stream_pool")


class StreamPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._peak = 0          # high-water mark since boot (survives self-heal)
        self._refusals = 0      # total try_acquire failures (= browser "Stream error"s)
        self._last_warn = 0.0

    def limit(self) -> int:
        """Max concurrent SSE streams = pool size minus the reserve kept free
        for control requests. getattr defaults guard a gitignored config that
        predates stream_reserve_threads."""
        threads = int(getattr(settings.manager, "http_threads", 64) or 64)
        reserve = int(getattr(settings.manager, "stream_reserve_threads", 24) or 24)
        return max(1, threads - reserve)

    def try_acquire(self) -> bool:
        with self._lock:
            lim = self.limit()
            if self._active >= lim:
                self._refusals += 1
                now = time.monotonic()
                if now - self._last_warn > 10.0:
                    self._last_warn = now
                    log.warning("SSE stream pool at capacity: active=%d limit=%d "
                                "refusals=%d — returning 503 (browser sees 'Stream error')",
                                self._active, lim, self._refusals)
                return False
            self._active += 1
            if self._active > self._peak:
                self._peak = self._active
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def active(self) -> int:
        with self._lock:
            return self._active

    def stats(self) -> dict:
        with self._lock:
            return {"active": self._active, "limit": self.limit(),
                    "peak": self._peak, "refusals": self._refusals}


POOL = StreamPool()
