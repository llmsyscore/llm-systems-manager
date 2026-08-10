"""Shared `sensors -j` cache for collectors that read hardware sensors.

The would-be import cycle between `system.py` (PR A1b) and `liquidctl.py`
(also A1b) is broken here: both modules import ``collect_sensors_cached``
and ``sensors_val`` from this leaf. _shared.py imports nothing from any
sibling collector — by design.

``set_deps(config=...)`` hands the module the agent's AgentConfig so the
cache TTL can read ``CONFIG.POLL_INTERVAL_S`` and the enable flag at call
time. Main re-calls it from the ``/config/reload`` route so an in-place
config swap doesn't leave us holding a stale reference.

``AbsenceLatch`` is the shared absence memory for hardware probes — see the
class docstring. Every instance is reset by ``set_deps``, so a
``/config/reload`` re-probes immediately instead of waiting out the interval.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from types import SimpleNamespace

log = logging.getLogger("llm-systems-agent.collectors._shared")

__all__ = ["set_deps", "collect_sensors_cached", "sensors_val", "AbsenceLatch",
           "collect_enabled", "collect_is_auto", "latch_level_for"]

_deps = SimpleNamespace()
_sensors_cache: dict = {}
_sensors_last: float = 0.0

# Absent hardware is re-probed this often when the collector doesn't override it.
REPROBE_INTERVAL_S = 900.0
_latches: "list[AbsenceLatch]" = []


def _fmt_interval(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.0f} min"


class AbsenceLatch:
    """Expiring absence memory for a hardware probe.

    A probe that finds nothing is skipped until ``interval_s`` has passed,
    instead of being disabled for the life of the process, and the outage is
    logged once (with one recovery line when the hardware comes back).
    """

    def __init__(self, what: str, *, interval_s: "float | None" = None,
                 level: int = logging.WARNING,
                 logger: "logging.Logger | None" = None) -> None:
        self.what = what
        self.level = level
        self._interval = interval_s
        self._log = logger or log
        self._absent = False
        self._warned = False
        self._at = 0.0
        _latches.append(self)

    @property
    def interval_s(self) -> float:
        if self._interval is not None:
            return float(self._interval)
        cfg = getattr(_deps, "config", None)
        raw = getattr(cfg, "COLLECT_REPROBE_INTERVAL_S", REPROBE_INTERVAL_S)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return REPROBE_INTERVAL_S
        return val if val > 0 else REPROBE_INTERVAL_S

    @property
    def absent(self) -> bool:
        return self._absent

    def should_probe(self) -> bool:
        """False only while a recent absence result is still fresh."""
        if not self._absent:
            return True
        now = time.monotonic()
        if now - self._at < self.interval_s:
            return False
        # Stamp here too, so a probe that raises still waits out the interval.
        self._at = now
        return True

    def record(self, found: bool, detail: str = "") -> None:
        """Remember the outcome; log the first absence and the recovery."""
        if found:
            if self._warned:
                self._log.info("%s detected again on re-probe", self.what)
            self._absent = False
            self._warned = False
            return
        self._absent = True
        self._at = time.monotonic()
        if not self._warned:
            self._warned = True
            self._log.log(self.level, "%s not detected%s; re-probing every %s",
                          self.what, detail, _fmt_interval(self.interval_s))

    def reset(self) -> None:
        """Forget the absence entirely so the next call probes immediately."""
        self._absent = False
        self._warned = False
        self._at = 0.0


def set_deps(*, config) -> None:
    _deps.config = config
    for latch in _latches:
        latch.reset()
    _sensors_latch.level = latch_level_for(config, "COLLECT_SENSORS_ENABLED")


def collect_enabled(config, name: str) -> bool:
    """Tri-state COLLECT_* gate: true and "auto" run the collector,
    only an affirmative false/"false" disables it."""
    v = getattr(config, name, True)
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off")
    return bool(v)


def collect_is_auto(config, name: str) -> bool:
    """True when the flag is "auto" — the collector's own probe decides."""
    v = getattr(config, name, None)
    return isinstance(v, str) and v.strip().lower() == "auto"


def latch_level_for(config, name: str) -> int:
    """Absence log level: INFO under "auto" (absence is expected), else WARNING."""
    return logging.INFO if collect_is_auto(config, name) else logging.WARNING


_sensors_latch = AbsenceLatch("lm-sensors (COLLECT_SENSORS_ENABLED is on)")


def collect_sensors_cached() -> dict:
    """Run `sensors -j` and return parsed JSON, cached by collection interval.
    A missing binary or empty report backs off on the latch interval."""
    global _sensors_cache, _sensors_last
    if not collect_enabled(_deps.config, "COLLECT_SENSORS_ENABLED"):
        return {}
    if not _sensors_latch.should_probe():
        return {}
    now = time.monotonic()
    if now - _sensors_last < max(2.0, getattr(_deps.config, "POLL_INTERVAL_S", 5.0)):
        return _sensors_cache
    _sensors_last = now
    try:
        out = subprocess.check_output(
            ["sensors", "-j"], text=True, timeout=5, close_fds=True,
            stderr=subprocess.DEVNULL,
        )
        _sensors_cache = json.loads(out)
    except FileNotFoundError:
        _sensors_latch.record(False, " on PATH")
        return {}
    except Exception as e:
        log.debug("sensors -j error: %s", e)
        return _sensors_cache
    _sensors_latch.record(bool(_sensors_cache))
    return _sensors_cache


def sensors_val(data: dict, adapter_key: str, sensor_key: str, sub_key: str):
    for k, v in data.items():
        if adapter_key.lower() in k.lower():
            s = v.get(sensor_key, {})
            for sk, sv in s.items():
                if sk.startswith(sub_key):
                    try:
                        return float(sv)
                    except Exception:
                        return None
    return None
