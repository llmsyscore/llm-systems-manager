"""Thread-safe sliding-window counters and sample windows for live rate
readouts (pushes/s, requests/min, latency p50)."""
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

    def per_min(self, now: "float | None" = None) -> float:
        return self.per_s(now) * 60.0

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class SampleWindow:
    """Numeric samples inside a trailing window, for percentile readouts."""

    def __init__(self, window_s: float = 900.0, max_samples: int = 5000) -> None:
        self.window_s = float(window_s)
        self._samples: deque = deque(maxlen=int(max_samples))
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        s = self._samples
        while s and s[0][0] < cutoff:
            s.popleft()

    def add(self, value: float, now: "float | None" = None) -> None:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        t = time.time() if now is None else float(now)
        with self._lock:
            self._samples.append((t, v))
            self._prune(t)

    def count(self, now: "float | None" = None) -> int:
        t = time.time() if now is None else float(now)
        with self._lock:
            self._prune(t)
            return len(self._samples)

    def percentile(self, q: float = 0.5,
                   now: "float | None" = None) -> "float | None":
        """Nearest-rank percentile over the live window; None when empty."""
        t = time.time() if now is None else float(now)
        with self._lock:
            self._prune(t)
            vals = sorted(v for _ts, v in self._samples)
        if not vals:
            return None
        idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
        return vals[idx]

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()


class TimestampRing:
    """Bare event timestamps inside a trailing window."""

    def __init__(self, window_s: float = 900.0, max_events: int = 20000) -> None:
        self.window_s = float(window_s)
        self._ts: deque = deque(maxlen=int(max_events))
        self._lock = threading.Lock()

    def add(self, now: "float | None" = None) -> float:
        t = time.time() if now is None else float(now)
        with self._lock:
            self._ts.append(t)
            cutoff = t - self.window_s
            while self._ts and self._ts[0] < cutoff:
                self._ts.popleft()
        return t

    def count_since(self, seconds: float,
                    now: "float | None" = None) -> int:
        t = time.time() if now is None else float(now)
        cutoff = t - float(seconds)
        with self._lock:
            return sum(1 for ts in self._ts if ts >= cutoff)

    def count(self, now: "float | None" = None) -> int:
        return self.count_since(self.window_s, now)

    def reset(self) -> None:
        with self._lock:
            self._ts.clear()
