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

from ._shared import (AbsenceLatch, collect_enabled, collect_sensors_cached,
                      latch_level_for, sensors_val)

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
# Expiring absence memory: skip devices/binary found missing so we don't
# re-spawn `sudo liquidctl status` every tick, and retry on the latch interval.
_MATCH_AIO, _MATCH_PSU, _MATCH_SMART = "Kraken", "HX1000i", "Smart Device"
_MATCHES = (_MATCH_AIO, _MATCH_PSU, _MATCH_SMART)
_binary_missing: bool = False
_bin_latch = AbsenceLatch("liquidctl binary (COLLECT_LIQUIDCTL_ENABLED is on)", logger=log)
_all_latch = AbsenceLatch("liquidctl devices (COLLECT_LIQUIDCTL_ENABLED is on)", logger=log)
_match_latches: "dict[str, AbsenceLatch]" = {}


def _latch_for(match: str) -> AbsenceLatch:
    # Per-device absence is normal on a host that has only some of them, so
    # these log at INFO; _all_latch carries the WARNING when none are found.
    latch = _match_latches.get(match)
    if latch is None:
        latch = _match_latches[match] = AbsenceLatch(
            f"liquidctl device {match!r}", level=logging.INFO, logger=log)
    return latch


def set_deps(*, config) -> None:
    global _binary_missing
    _deps.config = config
    _binary_missing = False
    for latch in (_bin_latch, _all_latch, *_match_latches.values()):
        latch.reset()
    _bin_latch.level = _all_latch.level = \
        latch_level_for(config, "COLLECT_LIQUIDCTL_ENABLED")


def collect_smart_device_sensors() -> dict:
    if not collect_enabled(_deps.config, "COLLECT_SENSORS_ENABLED"):
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
        _bin_latch.record(False, " on PATH")
        return results, device_name, True
    except subprocess.CalledProcessError:
        _bin_latch.record(True)
        return results, device_name, True
    except Exception as e:
        log.debug("liquidctl %s: %s", match, e)
        return results, device_name, False
    _bin_latch.record(True)
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
    # Skip a device found absent until its latch expires; remember new absences.
    if _binary_missing:
        return [], None
    latch = _latch_for(match)
    if not latch.should_probe():
        return [], None
    rows, name, definitive = _run_liquidctl_status(match)
    if definitive and not _binary_missing:
        latch.record(bool(rows) or name is not None)
    return rows, name


def collect_liquidctl() -> dict:
    global _binary_missing
    if not collect_enabled(_deps.config, "COLLECT_LIQUIDCTL_ENABLED"):
        return {}
    if _binary_missing:
        if not _bin_latch.should_probe():
            return {}
        _binary_missing = False
    kr_rows, kr_name = _status_or_absent(_MATCH_AIO)
    psu_rows, psu_name = _status_or_absent(_MATCH_PSU)
    smart_rows, smart_name = _status_or_absent(_MATCH_SMART)
    if _binary_missing:
        return {}
    if not (kr_rows or kr_name or psu_rows or psu_name or smart_rows or smart_name):
        # Only a definitive all-absent read is an outage; a transient one isn't.
        if all(_latch_for(m).absent for m in _MATCHES):
            _all_latch.record(False)
        return {}
    _all_latch.record(True)
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
    if not collect_enabled(_deps.config, "COLLECT_LIQUIDCTL_ENABLED"):
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
