"""Windowed deltas over llama-server cumulative counters (prompt cache, speculative decoding)."""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Optional

# /metrics counter line prefix → sample key holding the raw cumulative value.
COUNTER_KEYS: dict[str, str] = {
    "llamacpp:prompt_tokens_total": "prompt_processed",
    "llamacpp:prompt_tokens_cached_total": "prompt_cached",
    "llamacpp:spec_decode_num_draft_tokens_total": "spec_drafted",
    "llamacpp:spec_decode_num_accepted_tokens_total": "spec_accepted",
    "llamacpp:spec_decode_num_drafts_total": "spec_drafts",
}

STATE_OK = "ok"
STATE_IDLE = "idle"
STATE_RESET = "reset"
STATE_UNSUPPORTED = "unsupported"
STATE_PAUSED = "paused"
STATE_OFF = "off"

DEFAULT_WINDOW_S = 60.0


def parse_counters(metrics_text: str) -> dict[str, int]:
    """Raw cumulative counters present in a /metrics body; absent names are left out."""
    out: dict[str, int] = {}
    for line in metrics_text.splitlines():
        if not line or line.startswith("#"):
            continue
        key = COUNTER_KEYS.get(line.split(" ", 1)[0])
        if key is None:
            continue
        try:
            v = float(line.split()[-1])
        except (ValueError, IndexError):
            continue
        if math.isfinite(v):
            out[key] = int(v)
    return out


def _ratio(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den > 0 else None


class CounterWindow:
    """Per-model ring of (ts, counters) trimmed to `window_s`; derives windowed ratios."""

    def __init__(self, window_s: float = DEFAULT_WINDOW_S) -> None:
        self.window_s = float(window_s)
        self._model: Optional[str] = None
        self._ring: "deque[tuple[float, dict[str, int]]]" = deque()
        self._reset_at: Optional[float] = None

    def observe(self, model_id: Optional[str], now: float, counters: dict[str, int]) -> None:
        """Record one /metrics read; a model switch or a decreasing counter clears the ring."""
        if model_id != self._model:
            self._ring.clear()
            self._reset_at = None
            self._model = model_id
        if self._ring:
            _, last = self._ring[-1]
            if set(counters) != set(last):
                self._ring.clear()
            elif any(counters[k] < last[k] for k in counters):
                self._ring.clear()
                self._reset_at = now
        self._ring.append((now, dict(counters)))
        self._trim(now)

    def reset(self, now: float) -> None:
        """Forget the ring after a server restart seen outside the counters (MainPID change)."""
        if self._ring:
            self._ring.clear()
            self._reset_at = now

    def _trim(self, now: float) -> None:
        while len(self._ring) > 1 and now - self._ring[0][0] > self.window_s:
            self._ring.popleft()

    def _supported(self, *keys: str) -> bool:
        return bool(self._ring) and all(k in self._ring[-1][1] for k in keys)

    def _delta(self, key: str) -> int:
        first, last = self._ring[0][1], self._ring[-1][1]
        return max(0, last.get(key, 0) - first.get(key, 0))

    def _state(self, now: float, active: bool, polled: bool) -> str:
        if self._reset_at is not None and now - self._reset_at <= self.window_s:
            return STATE_RESET
        if not polled:
            return STATE_PAUSED
        return STATE_OK if active else STATE_IDLE

    def cache_reuse(self, now: float, polled: bool = True) -> dict[str, Any]:
        """Prompt-cache reuse over the window: cached / (processed + cached)."""
        base: dict[str, Any] = {"state": STATE_UNSUPPORTED, "processed": None, "cached": None, "ratio": None}
        if not self._supported("prompt_processed", "prompt_cached"):
            return base
        self._trim(now)
        processed, cached = self._delta("prompt_processed"), self._delta("prompt_cached")
        total = processed + cached
        base.update({"processed": processed, "cached": cached,
                     "ratio": _ratio(cached, total) if total > 0 else None,
                     "state": self._state(now, total > 0, polled)})
        return base

    def spec_accept(self, now: float, polled: bool = True) -> dict[str, Any]:
        """Speculative/MTP acceptance over the window: accepted / drafted, plus tokens per verify step."""
        base: dict[str, Any] = {"state": STATE_UNSUPPORTED, "drafted": None, "accepted": None,
                                "drafts": None, "ratio": None, "accepted_per_draft": None}
        if not self._supported("spec_drafted", "spec_accepted"):
            return base
        self._trim(now)
        latest = self._ring[-1][1]
        if latest["spec_drafted"] == 0 and latest["spec_accepted"] == 0:
            base.update({"state": STATE_OFF, "drafted": 0, "accepted": 0,
                         "drafts": latest.get("spec_drafts")})
            return base
        drafted, accepted = self._delta("spec_drafted"), self._delta("spec_accepted")
        drafts = self._delta("spec_drafts") if self._supported("spec_drafts") else None
        base.update({"drafted": drafted, "accepted": accepted, "drafts": drafts,
                     "ratio": _ratio(accepted, drafted) if drafted > 0 else None,
                     "accepted_per_draft": round(accepted / drafts, 2) if drafts else None,
                     "state": self._state(now, drafted > 0, polled)})
        return base

    @property
    def model(self) -> Optional[str]:
        return self._model

    def latest(self) -> dict[str, int]:
        return dict(self._ring[-1][1]) if self._ring else {}
