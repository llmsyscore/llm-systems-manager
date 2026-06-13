"""Per-tick host system collector — CPU + RAM + swap + net + disk + I/O.

`collect_system_metrics()` is the single public entry; called once per
collector_loop tick. It pulls the GPU/UPS/liquidctl/iscsi sub-blocks via
sibling-module functions and stitches them into the dashboard payload
shape. The `RateTracker` + `_net_rate`/`_dio_rate` pair turn psutil's
monotonic counters into per-second rates.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil

from ._shared import collect_sensors_cached, sensors_val
from .gpu import collect_gpu
from .liquidctl import get_liquidctl_cached
from .ups import collect_ups

log = logging.getLogger("llm-systems-agent.collectors.system")

__all__ = ["set_deps", "collect_system_metrics", "read_cpu_governor",
           "read_cpu_temp_c"]

_deps = SimpleNamespace()


def set_deps(*, config) -> None:
    _deps.config = config


def collect_iscsi() -> dict:
    if not getattr(_deps.config, "COLLECT_ISCSI_ENABLED", True):
        return {}
    result = {"state": None, "target": None, "session": None}
    try:
        base = Path("/sys/class/iscsi_session")
        if not base.is_dir():
            return result
        sessions = list(base.iterdir())
        if not sessions:
            return result
        sess = sessions[0]
        result["session"] = sess.name
        tp = sess / "targetname"
        if tp.exists():
            result["target"] = tp.read_text().strip()
        sp = sess / "device" / "iscsi_session" / sess.name / "state"
        if sp.exists():
            result["state"] = sp.read_text().strip()
    except Exception as e:
        log.debug("iscsi sysfs: %s", e)
    return result


_GOVERNOR_ROOT = Path("/sys/devices/system/cpu/cpufreq")
_GOVERNOR_TTL_S = 60.0
_governor_cache: dict[str, Any] = {"ts": 0.0, "value": None}


def read_cpu_governor() -> "str | None":
    # Aggregates across policy* dirs (hybrid CPUs); single value if uniform,
    # comma-joined list ("performance,powersave") otherwise.
    now = time.monotonic()
    if now - _governor_cache["ts"] < _GOVERNOR_TTL_S:
        return _governor_cache["value"]
    values: list[str] = []
    try:
        for pol in sorted(_GOVERNOR_ROOT.glob("policy*")):
            try:
                values.append((pol / "scaling_governor").read_text().strip())
            except (FileNotFoundError, OSError):
                continue
    except (FileNotFoundError, OSError):
        pass
    unique = list(dict.fromkeys(values))
    value = unique[0] if len(unique) == 1 else (",".join(unique) if unique else None)
    _governor_cache["ts"] = now
    _governor_cache["value"] = value
    return value


def read_cpu_temp_c() -> "float | None":
    """CPU Tctl from sensors JSON (AMD k10temp adapter)."""
    return sensors_val(collect_sensors_cached(), "k10temp", "Tctl", "temp1_input")


_cpu_name_cache: "str | None" = None
_cpu_name_probed = False


def read_cpu_name() -> "str | None":
    global _cpu_name_cache, _cpu_name_probed
    if _cpu_name_probed:
        return _cpu_name_cache
    _cpu_name_probed = True
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                _, _, val = line.partition(":")
                _cpu_name_cache = val.strip() or None
                return _cpu_name_cache
    except Exception as e:
        log.debug("cpu name probe: %s", e)
    return _cpu_name_cache


class RateTracker:
    """Per-second rates from monotonically-increasing counters; clamps wraps to 0."""

    def __init__(self, fields: tuple[str, ...], counts: tuple[str, ...] = ()) -> None:
        self._rate_fields = fields
        self._count_fields = counts
        self._prev: dict[str, int] = {}
        self._prev_ts: float | None = None

    def update(self, now: float, sample: dict[str, int]) -> tuple[dict[str, float], dict[str, int]]:
        rates: dict[str, float] = {f: 0.0 for f in self._rate_fields}
        deltas: dict[str, int] = {f: 0 for f in self._count_fields}
        if self._prev_ts is not None:
            dt = max(0.001, now - self._prev_ts)
            for f in self._rate_fields:
                rates[f] = max(0.0, (sample[f] - self._prev.get(f, sample[f])) / dt)
            for f in self._count_fields:
                deltas[f] = max(0, sample[f] - self._prev.get(f, sample[f]))
        self._prev = dict(sample)
        self._prev_ts = now
        return rates, deltas


_net_rate = RateTracker(fields=("bytes_sent", "bytes_recv"))
_dio_rate = RateTracker(
    fields=("read_bytes", "write_bytes"),
    counts=("read_count", "write_count"),
)

# Network/FUSE mounts can stall the entire collector tick when unresponsive.
_DISK_SKIP_FSTYPES = frozenset({
    "nfs", "nfs3", "nfs4", "cifs", "smbfs",
    "fuse", "fuse.sshfs", "fusectl", "fuse.gvfsd-fuse",
})

_DISKS_TTL_S = 30.0
_disks_cache: dict[str, Any] = {"ts": 0.0, "value": []}


def _collect_disks() -> list[dict[str, Any]]:
    now = time.monotonic()
    if now - _disks_cache["ts"] < _DISKS_TTL_S and _disks_cache["ts"] > 0:
        return list(_disks_cache["value"])
    out: list[dict[str, Any]] = []
    for part in psutil.disk_partitions(all=False):
        if (part.fstype or "").lower() in _DISK_SKIP_FSTYPES:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (OSError, PermissionError) as e:
            log.debug("disk_usage skip %s: %s", part.mountpoint, e)
            continue
        out.append({
            "mountpoint":  part.mountpoint,
            "total_bytes": u.total,
            "used_bytes":  u.used,
            "free_bytes":  u.free,
            "percent":     u.percent,
        })
    _disks_cache["ts"] = now
    _disks_cache["value"] = out
    return list(out)


def collect_system_metrics() -> dict[str, Any]:
    cpu = psutil.cpu_percent(percpu=True)
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    # monotonic for rate deltas (wall-clock steps would fabricate spikes);
    # the iso timestamp in the return dict uses datetime.now separately.
    now = time.monotonic()

    nio = psutil.net_io_counters()
    net_rates, _ = _net_rate.update(now, {
        "bytes_sent": nio.bytes_sent,
        "bytes_recv": nio.bytes_recv,
    })

    disk_io = {"read_bytes_per_sec": 0.0, "write_bytes_per_sec": 0.0,
               "read_count": 0, "write_count": 0}
    try:
        dio = psutil.disk_io_counters(perdisk=False)
    except (OSError, RuntimeError) as e:
        log.debug("disk_io_counters: %s", e)
        dio = None
    if dio is not None:
        rates, counts = _dio_rate.update(now, {
            "read_bytes":  dio.read_bytes,
            "write_bytes": dio.write_bytes,
            "read_count":  dio.read_count,
            "write_count": dio.write_count,
        })
        disk_io = {
            "read_bytes_per_sec":  rates["read_bytes"],
            "write_bytes_per_sec": rates["write_bytes"],
            "read_count":  counts["read_count"],
            "write_count": counts["write_count"],
        }

    disks = _collect_disks()

    # Smart-device fan voltage/current enrichment now happens inside
    # get_liquidctl_cached so it runs once per ~15s TTL, not every tick.
    lq = get_liquidctl_cached()

    return {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "host":         _deps.config.AGENT_HOSTNAME,
        "cpu_per_core": cpu,
        "cpu_total":    (sum(cpu) / len(cpu)) if cpu else 0.0,
        "cpu_governor": read_cpu_governor(),
        "cpu_temp_c":   read_cpu_temp_c(),
        "cpu_name":     read_cpu_name(),
        "ram": {
            "total_bytes": vm.total,
            "used_bytes": vm.total - vm.available,
            "available_bytes": vm.available,
            "percent": vm.percent,
            "cached_bytes":  int(getattr(vm, "cached", 0) or 0),
            "buffers_bytes": int(getattr(vm, "buffers", 0) or 0),
        },
        "swap": {
            "total_bytes": sm.total,
            "used_bytes": sm.used,
            "free_bytes": int(getattr(sm, "free", max(0, sm.total - sm.used)) or 0),
            "percent": sm.percent,
        },
        "net": {
            "bytes_sent_per_s":    net_rates["bytes_sent"],
            "bytes_recv_per_s":    net_rates["bytes_recv"],
            "bytes_sent_per_sec":  net_rates["bytes_sent"],
            "bytes_recv_per_sec":  net_rates["bytes_recv"],
            "bytes_sent_total":    nio.bytes_sent,
            "bytes_recv_total":    nio.bytes_recv,
        },
        "disk":      disks,
        "disk_io":   disk_io,
        "gpu":       collect_gpu(),
        "ups":       collect_ups(),
        "liquidctl": lq,
        "iscsi":     collect_iscsi(),
    }
