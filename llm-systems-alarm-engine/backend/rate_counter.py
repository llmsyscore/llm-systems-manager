"""Thread-safe sliding-window event counters for the /health rate readouts."""
from __future__ import annotations

import threading
import time
from collections import deque


class RateCounter:
    """Counts events inside a trailing window and reports their rate."""

    def __init__(self, window_s: float = 60.0) -> None:
        self.window_s = float(window_s)
        self._events: deque = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        ev = self._events
        while ev and ev[0][0] < cutoff:
            ev.popleft()

    def add(self, n: int = 1, now: "float | None" = None) -> None:
        if n <= 0:
            return
        t = time.time() if now is None else float(now)
        with self._lock:
            self._events.append((t, int(n)))
            self._prune(t)

    def total(self, now: "float | None" = None) -> int:
        t = time.time() if now is None else float(now)
        with self._lock:
            self._prune(t)
            return sum(n for _ts, n in self._events)

    def per_s(self, now: "float | None" = None) -> float:
        return self.total(now) / self.window_s if self.window_s > 0 else 0.0

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


# Process-wide counters read by /health.
INGEST_POINTS = RateCounter(60.0)
INFLUX_WRITES = RateCounter(60.0)
