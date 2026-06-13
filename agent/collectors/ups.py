"""UPS metrics via ``upower``; degrades cleanly when neither upower nor a
UPS device is present.

``_UPS_DEVICE`` is probed once at import via ``upower -e`` and cached for
the lifetime of the process. ``None`` means upower isn't installed or no
UPS device was advertised — ``collect_ups()`` returns the all-None result
dict so downstream callers don't need to distinguish "no UPS" from "UPS
unreachable this tick".
"""

from __future__ import annotations

import logging
import subprocess
from types import SimpleNamespace
from typing import Any

log = logging.getLogger("llm-systems-agent.collectors.ups")

__all__ = ["set_deps", "collect_ups"]

_deps = SimpleNamespace()


def set_deps(*, config) -> None:
    _deps.config = config


# Lazy-probed on first collect_ups() so module import doesn't block on `upower -e`.
_UNPROBED = object()
_UPS_DEVICE: Any = _UNPROBED


def _find_ups_device():
    """Probe a UPS device path via `upower -e`; result cached for process lifetime."""
    try:
        out = subprocess.check_output(["upower", "-e"], text=True, timeout=3, close_fds=True)
        for line in out.splitlines():
            if "ups" in line.lower():
                return line.strip()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return None


def _ensure_probed() -> None:
    global _UPS_DEVICE
    if _UPS_DEVICE is _UNPROBED:
        _UPS_DEVICE = _find_ups_device()


def collect_ups() -> dict:
    """Return UPS metrics; empty dict if upower or UPS isn't present."""
    if not getattr(_deps.config, "COLLECT_UPS_ENABLED", True):
        return {}
    _ensure_probed()
    result = {"percent": None, "state": None, "warning_level": None,
              "on_battery": None, "time_to_empty": None, "time_to_full": None}
    if _UPS_DEVICE is None:
        return result
    try:
        out = subprocess.check_output(
            ["upower", "-i", _UPS_DEVICE], text=True, timeout=3, close_fds=True
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("percentage:"):
                try: result["percent"] = float(line.split(":")[1].strip().replace("%", ""))
                except Exception: pass
            elif line.startswith("state:"):
                result["state"] = line.split(":")[1].strip()
                result["on_battery"] = "discharging" in result["state"].lower()
            elif line.startswith("warning-level:"):
                result["warning_level"] = line.split(":")[1].strip()
            elif line.startswith("time to empty:"):
                result["time_to_empty"] = line.split(":", 1)[1].strip()
            elif line.startswith("time to full:"):
                result["time_to_full"] = line.split(":", 1)[1].strip()
    except Exception as e:
        log.debug("upower -i %s: %s", _UPS_DEVICE, e)
    return result
