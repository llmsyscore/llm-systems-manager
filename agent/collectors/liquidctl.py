"""liquidctl-driven AIO + Corsair HX1000i PSU + NZXT Smart Device V2.

USB HID queries are slow; `get_liquidctl_cached` wraps `collect_liquidctl`
in a ~15s TTL. `collect_smart_device_sensors` reads the same Smart Device
V2 fan voltage/current/RPM via the `sensors -j` cache (cheaper path) and
the manager merges that enrichment into the liquidctl block at the
`_build_metric_sample` site.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from types import SimpleNamespace

from ._shared import collect_sensors_cached, sensors_val

# Matches the tree-drawing glyphs liquidctl prefixes sensor rows with.
_TREE_PREFIX_RE = re.compile(r"^[├└─\s]+")
# Splits columns on 2+ whitespace chars (tolerates single- and multi-space padding).
_COL_SPLIT_RE = re.compile(r"\s{2,}")

log = logging.getLogger("llm-systems-agent.collectors.liquidctl")

__all__ = ["set_deps", "collect_smart_device_sensors", "collect_liquidctl",
           "get_liquidctl_cached"]

_deps = SimpleNamespace()
_liquidctl_cache: dict = {}
_liquidctl_last_poll: float = 0.0
# Probe-once absence memory (reset on process restart): skip devices/binary
# found missing so we don't re-spawn `sudo liquidctl status` every tick.
_binary_missing: bool = False
_absent_matches: set = set()


def set_deps(*, config) -> None:
    _deps.config = config


def collect_smart_device_sensors() -> dict:
    if not getattr(_deps.config, "COLLECT_SENSORS_ENABLED", True):
        return {"fans": []}
    data = collect_sensors_cached()
    out = {"fans": []}
    for i in range(1, 4):
        fan = {
            "id": i,
            "voltage_v": sensors_val(data, "nzxtsmart2", f"FAN {i} Voltage", "in"),
            "current_ma": None,
            "rpm": sensors_val(data, "nzxtsmart2", f"FAN {i}", f"fan{i}_input"),
        }
        amps = sensors_val(data, "nzxtsmart2", f"FAN {i} Current", "curr")
        if amps is not None:
            fan["current_ma"] = round(amps * 1000)
        out["fans"].append(fan)
    return out


def _liquidctl_bin():
    return getattr(_deps.config, "LIQUIDCTL_BIN", "") or "liquidctl"


def _run_liquidctl_status(match: str) -> tuple[list[dict], "str | None", bool]:
    # 3rd element is `definitive`: False on transient errors (timeout/unknown)
    # so a flaky read isn't mistaken for absent hardware.
    global _binary_missing
    results: list[dict] = []
    device_name: "str | None" = None
    try:
        out = subprocess.check_output(
            ["sudo", "-n", _liquidctl_bin(), "status", "--match", match],
            text=True, timeout=5, stderr=subprocess.DEVNULL, close_fds=True
        )
    except FileNotFoundError:
        _binary_missing = True
        return results, device_name, True
    except subprocess.CalledProcessError:
        return results, device_name, True
    except Exception as e:
        log.debug("liquidctl %s: %s", match, e)
        return results, device_name, False
    for line in out.splitlines():
        line = _TREE_PREFIX_RE.sub("", line.strip()).strip()
        # Skip blank lines, device-header lines (NZXT/Corsair/etc.), and any
        # localized "WARNING:"-style banner (key ending in colon, no second column).
        if not line:
            continue
        parts = [p.strip() for p in _COL_SPLIT_RE.split(line) if p.strip()]
        if len(parts) < 2 or parts[0].endswith(":"):
            if device_name is None and len(parts) == 1 and not parts[0].endswith(":"):
                device_name = parts[0]
            continue
        key = parts[0]
        value_str = parts[1]
        unit = parts[2] if len(parts) > 2 else ""
        try: value = float(value_str)
        except ValueError: value = value_str.strip()
        results.append({"key": key, "value": value, "unit": unit.strip()})
    return results, device_name, True


def _parse_liquidctl_rows(rows: list[dict], keys: list[str]) -> dict:
    lookup = {r["key"]: r for r in rows}
    result = {}
    for key in keys:
        if key in lookup:
            r = lookup[key]
            result[key] = {"value": r["value"], "unit": r["unit"]}
        else:
            result[key] = None
    return result


def _status_or_absent(match: str) -> tuple[list[dict], "str | None"]:
    # Skip a probe we've already found absent; remember new absences.
    if _binary_missing or match in _absent_matches:
        return [], None
    rows, name, definitive = _run_liquidctl_status(match)
    if definitive and not _binary_missing and not rows and name is None:
        _absent_matches.add(match)
    return rows, name


def collect_liquidctl() -> dict:
    if not getattr(_deps.config, "COLLECT_LIQUIDCTL_ENABLED", True):
        return {}
    if _binary_missing:
        return {}
    kr_rows, kr_name = _status_or_absent("Kraken")
    psu_rows, psu_name = _status_or_absent("HX1000i")
    smart_rows, smart_name = _status_or_absent("Smart Device")
    if _binary_missing:
        return {}
    if not (kr_rows or kr_name or psu_rows or psu_name or smart_rows or smart_name):
        return {}
    aio = _parse_liquidctl_rows(kr_rows,
        ["Liquid temperature", "Pump speed", "Pump duty", "Fan speed", "Fan duty"])
    if kr_name: aio["_name"] = kr_name
    psu = _parse_liquidctl_rows(psu_rows,
        ["VRM temperature", "Case temperature", "Fan speed",
         "Input voltage", "Total power output",
         "Estimated input power", "Estimated efficiency"])
    if psu_name: psu["_name"] = psu_name
    smart = {"fans": []}
    if smart_name: smart["_name"] = smart_name
    for i in range(1, 4):
        fan = {"id": i, "control_mode": None, "duty": None, "speed": None}
        for row in smart_rows:
            k = row["key"]
            if k == f"Fan {i} control mode": fan["control_mode"] = row["value"]
            elif k == f"Fan {i} duty":       fan["duty"] = row["value"]
            elif k == f"Fan {i} speed":      fan["speed"] = {"value": row["value"], "unit": row["unit"]}
        smart["fans"].append(fan)
    return {"aio": aio, "psu": psu, "smart": smart}


def get_liquidctl_cached() -> dict:
    # ~15s TTL — liquidctl USB HID queries are too slow for the 2s tick.
    global _liquidctl_cache, _liquidctl_last_poll
    if not getattr(_deps.config, "COLLECT_LIQUIDCTL_ENABLED", True):
        return {}
    now = time.monotonic()
    if now - _liquidctl_last_poll < max(15.0, getattr(_deps.config, "POLL_INTERVAL_S", 5.0) * 3):
        return _liquidctl_cache
    fresh = collect_liquidctl()
    if fresh.get("smart"):
        smart_sensors = collect_smart_device_sensors()
        if smart_sensors.get("fans"):
            lq_fans = fresh["smart"].get("fans", [])
            for i, fan in enumerate(smart_sensors["fans"]):
                if i < len(lq_fans):
                    lq_fans[i]["voltage_v"]  = fan.get("voltage_v")
                    lq_fans[i]["current_ma"] = fan.get("current_ma")
    _liquidctl_cache = fresh
    _liquidctl_last_poll = now
    return _liquidctl_cache
