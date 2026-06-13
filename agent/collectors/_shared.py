"""Shared `sensors -j` cache for collectors that read hardware sensors.

The would-be import cycle between `system.py` (PR A1b) and `liquidctl.py`
(also A1b) is broken here: both modules import ``collect_sensors_cached``
and ``sensors_val`` from this leaf. _shared.py imports nothing from any
sibling collector — by design.

``set_deps(config=...)`` hands the module the agent's AgentConfig so the
cache TTL can read ``CONFIG.POLL_INTERVAL_S`` and the enable flag at call
time. Main re-calls it from the ``/config/reload`` route so an in-place
config swap doesn't leave us holding a stale reference.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from types import SimpleNamespace

log = logging.getLogger("llm-systems-agent.collectors._shared")

__all__ = ["set_deps", "collect_sensors_cached", "sensors_val"]

_deps = SimpleNamespace()
_sensors_cache: dict = {}
_sensors_last: float = 0.0


def set_deps(*, config) -> None:
    _deps.config = config


def collect_sensors_cached() -> dict:
    """Run `sensors -j` and return parsed JSON, cached by collection interval."""
    global _sensors_cache, _sensors_last
    if not getattr(_deps.config, "COLLECT_SENSORS_ENABLED", True):
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
        return {}
    except Exception as e:
        log.debug("sensors -j error: %s", e)
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
