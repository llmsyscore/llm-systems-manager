"""#1031 Forecast sampler: pushes the forecast-only series to the alarm engine once a minute."""
from __future__ import annotations

import logging
import math
import re
import sys
import threading
import time
from typing import Callable, Optional

from forecast_checks import slug

log = logging.getLogger("llm-systems-manager.forecast")

SOURCE = "forecast"
TICK_S = 60.0
MAX_MODELS = 20
GAUGES = ("db_manager_bytes", "db_audit_bytes", "db_energy_bytes", "db_wal_bytes",
          "backup_age_s", "backup_bytes", "agents_stale")
COUNTERS = ("model_loads", "model_unloads", "model_wakes", "log_errors")
GATEWAY_KEYS = ("requests", "errors", "timeouts", "failovers")
AGENT_FIELDS = (("gap_s", "agent_heartbeat_gap_s"), ("skew_s", "agent_clock_skew_s"))
MAX_SLUG = 80
_GW_PREFIX = "gateway|"
_SLUG_BAD = re.compile(r"[^A-Za-z0-9._-]")
_SLUG_RUNS = re.compile(r"_+")


def _unit(name: str) -> str:
    """bytes for *_bytes, seconds for *_s, empty for counts."""
    if name.endswith("_bytes"):
        return "bytes"
    return "s" if name.endswith("_s") else ""


def _num(value) -> Optional[float]:
    """Finite float reading, or None when it is missing, unparsable, NaN or infinite."""
    try:
        out = None if value is None else float(value)
    except (TypeError, ValueError):
        return None
    return out if out is None or math.isfinite(out) else None


def _slug_part(model) -> str:
    """Series-name part: slug, every other character folded to _, collapsed, truncated to MAX_SLUG."""
    out = _SLUG_RUNS.sub("_", _SLUG_BAD.sub("_", slug(model))).strip("_")
    return ((out or "model")[:MAX_SLUG]).rstrip("_") or "model"


class Sampler:
    """Reads the series nobody stores yet and pushes one batch of points per tick."""

    def __init__(self, *, read: "dict[str, Callable]", push: "Callable[[list], None]", hostname: str,
                 enabled: "Callable[[], bool]", now=time.time):
        self._read, self._push = dict(read or {}), push
        self.hostname, self._enabled, self._now = hostname, enabled, now
        self._base: "dict[str, float]" = {}
        self._lock = threading.Lock()
        self._busy = False
        self._push_down = False

    def _call(self, key: str):
        """Reader result, or None when the reader is absent or raises."""
        fn = self._read.get(key)
        if fn is None:
            return None
        try:
            return fn()
        except Exception as e:
            log.debug("forecast reader %s failed: %s", key, type(e).__name__)
            return None

    def _point(self, name: str, value: float, host: Optional[str] = None) -> dict:
        return {"source": SOURCE, "metric_name": name, "value": float(value),
                "unit": _unit(name), "hostname": host or self.hostname}

    def _delta(self, key: str, value) -> Optional[float]:
        """Rise since the previous tick; None on the first reading or after a reset."""
        cur = _num(value)
        if cur is None:
            return None
        prev = self._base.get(key)
        self._base[key] = cur
        return None if prev is None or cur < prev else cur - prev

    def _gauges(self, pts: list) -> None:
        for name in GAUGES:
            value = _num(self._call(name))
            if value is not None:
                pts.append(self._point(name, value))

    def _counters(self, pts: list) -> None:
        for name in COUNTERS:
            delta = self._delta(f"counter|{name}", self._call(name))
            if delta is not None:
                pts.append(self._point(name, delta))

    def _gateway(self, pts: list) -> None:
        """Deltas summed per slug, top MAX_MODELS by request rise then total activity; a slug that moved at
        all pushes all four series, so the four share their minutes."""
        data = self._call("gateway")
        if not isinstance(data, dict):
            return
        merged: "dict[str, dict[str, Optional[float]]]" = {}
        live = set()
        for model, counts in data.items():
            counts = counts if isinstance(counts, dict) else {}
            bucket = merged.setdefault(_slug_part(model), {k: None for k in GATEWAY_KEYS})
            for key in GATEWAY_KEYS:
                base_key = f"{_GW_PREFIX}{model}|{key}"
                live.add(base_key)
                delta = self._delta(base_key, counts.get(key))
                if delta is not None:
                    bucket[key] = (bucket[key] or 0.0) + delta
        for stale in [k for k in self._base if k.startswith(_GW_PREFIX) and k not in live]:
            self._base.pop(stale, None)
        rows = sorted(merged.items(), key=lambda row: (-(row[1]["requests"] or 0.0),
                                                       -sum(row[1][k] or 0.0 for k in GATEWAY_KEYS), row[0]))
        for name, deltas in rows[:MAX_MODELS]:
            if not any(deltas[k] for k in GATEWAY_KEYS):
                continue
            for key in GATEWAY_KEYS:
                pts.append(self._point(f"gateway_{key}_{name}", deltas[key] or 0.0))

    def _agents(self, pts: list) -> None:
        for row in self._call("agents") or []:
            host = row.get("host") if isinstance(row, dict) else None
            if not host:
                continue
            for field, name in AGENT_FIELDS:
                value = _num(row.get(field))
                if value is not None:
                    pts.append(self._point(name, value, host))

    def tick(self) -> int:
        """Read every series and push one batch; 0 when disabled, overlapping or the push failed. Never raises."""
        with self._lock:
            if self._busy:
                return 0
            self._busy = True
        try:
            return self._tick()
        finally:
            self._busy = False

    def _tick(self) -> int:
        try:
            on = bool(self._enabled())
        except Exception as e:
            log.debug("forecast enabled check failed: %s", type(e).__name__)
            on = False
        if not on:
            self._base.clear()
            return 0
        pts: list = []
        for step in (self._gauges, self._counters, self._gateway, self._agents):
            try:
                step(pts)
            except Exception as e:
                log.debug("forecast sampler %s failed: %s", step.__name__, type(e).__name__)
        try:
            self._push(pts)
        except Exception as e:
            # One line per outage, not one a minute.
            if not self._push_down:
                self._push_down = True
                log.warning("forecast push failed: %s", type(e).__name__)
            return 0
        if self._push_down:
            self._push_down = False
            log.info("forecast push recovered")
        return len(pts)


def start_thread(sampler: Sampler, shutting_down: Callable[[], bool]):
    """Daemon loop ticking the sampler every TICK_S; None under pytest."""
    if "pytest" in sys.modules:
        return None

    def _loop():
        while not shutting_down():
            sampler.tick()
            time.sleep(TICK_S)

    t = threading.Thread(target=_loop, name="forecast-sampler", daemon=True)
    t.start()
    return t
