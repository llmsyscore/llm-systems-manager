"""Cap on concurrent long-lived SSE streams served by the agent.

Each sync-generator SSE response pins one anyio worker thread for the
stream's whole life. Without a cap, enough concurrent streams exhaust the
worker pool and the agent stops answering sync control requests (terminal
create/output, status, the manager's polls). The cap stays below the pool
size, reserving workers for non-stream requests. Over the cap the endpoint
returns 503 and the browser/manager reconnects.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("llm-systems-agent.stream_pool")

_worker_threads = 64
_reserve = 24


def configure(worker_threads: int, reserve: int) -> None:
    global _worker_threads, _reserve
    _worker_threads = max(1, int(worker_threads))
    _reserve = max(0, int(reserve))


class StreamPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._peak = 0          # high-water mark since boot (survives self-heal)
        self._refusals = 0      # total try_acquire failures (503s)
        self._last_warn = 0.0

    def limit(self) -> int:
        """Max concurrent SSE streams = worker pool minus the reserve kept
        free for control requests."""
        return max(1, _worker_threads - _reserve)

    def try_acquire(self) -> bool:
        with self._lock:
            lim = self.limit()
            if self._active >= lim:
                self._refusals += 1
                now = time.monotonic()
                if now - self._last_warn > 10.0:
                    self._last_warn = now
                    log.warning("SSE stream pool at capacity: active=%d limit=%d "
                                "refusals=%d — returning 503", self._active, lim, self._refusals)
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


async def guarded_async(sync_gen):
    """Async wrapper that drains a sync SSE generator and frees the slot in an
    async finally. Starlette delivers disconnect-cancel into a native async
    generator, so this finally runs deterministically — unlike a sync-generator
    finally wrapped by iterate_in_threadpool, which only runs at cyclic GC and
    leaks the slot on disconnect. Closes the inner generator so its own finally
    (e.g. terminal _reap_session) fires too."""
    import anyio

    _sentinel = object()

    def _step():
        try:
            return next(sync_gen)
        except StopIteration:
            return _sentinel

    try:
        while True:
            chunk = await anyio.to_thread.run_sync(_step)
            if chunk is _sentinel:
                break
            yield chunk
    finally:
        POOL.release()
        try:
            sync_gen.close()
        except Exception:
            pass
