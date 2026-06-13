"""GPU metrics collector — AMD sysfs first, NVIDIA via nvidia-smi second.

Vendor probes happen at IMPORT time:
  - ``_GPU_PATH``: first AMD card under ``/sys/class/drm`` (vendor 0x1002)
  - ``_HWMON``: the hwmon subdir under ``_GPU_PATH``
  - ``_NVIDIA_PRESENT``: True iff an NVIDIA card AND ``nvidia-smi`` exist

A box with neither just returns ``{}`` from ``collect_gpu()``. The
``_read_*_file`` helpers stay private — only this module's GPU code uses
them.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

log = logging.getLogger("llm-systems-agent.collectors.gpu")

__all__ = ["set_deps", "collect_gpu"]

_deps = SimpleNamespace()


def set_deps(*, config) -> None:
    _deps.config = config


# Lazy-probed on first collect_gpu() so module import doesn't sysfs-walk on every host.
_UNPROBED = object()
_GPU_PATH: Any = _UNPROBED
_HWMON: Any = _UNPROBED


def _find_amd_gpu_path():
    try:
        for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            if "-" in card.name:
                continue
            try:
                if (card / "device" / "vendor").read_text().strip() == "0x1002":
                    return card / "device"
            except Exception:
                pass
    except Exception as e:
        log.debug("AMD GPU sysfs scan failed: %s", e)
    return None


def _hwmon_dir():
    if _GPU_PATH is None:
        return None
    hd = _GPU_PATH / "hwmon"
    if not hd.is_dir():
        return None
    for h in sorted(hd.iterdir()):
        if h.is_dir() and h.name.startswith("hwmon"):
            return h
    return None


def _read_int_file(path, default=None):
    try: return int(Path(path).read_text().strip())
    except Exception: return default


def _read_float_file(path, default=None):
    try: return float(Path(path).read_text().strip())
    except Exception: return default


def _read_str_file(path, default=None):
    try: return Path(path).read_text().strip()
    except Exception: return default


def _gpu_temp(suffix: str):
    if _HWMON is None: return None
    v = _read_int_file(_HWMON / f"temp{suffix}_input")
    return v / 1000.0 if v is not None else None


def _gpu_vram():
    if _GPU_PATH is None: return None, None, None
    used  = _read_int_file(_GPU_PATH / "mem_info_vram_used")
    total = _read_int_file(_GPU_PATH / "mem_info_vram_total")
    if used is None or total is None or total == 0:
        return None, None, None
    return used, total, (used / total) * 100.0


def _gpu_clocks():
    sclk = mclk = None
    if _GPU_PATH is None:
        return sclk, mclk
    for fname, target in [("pp_dpm_sclk", "sclk"), ("pp_dpm_mclk", "mclk")]:
        raw = _read_str_file(_GPU_PATH / fname)
        if not raw:
            continue
        for line in raw.splitlines():
            if "*" in line:
                for part in line.split():
                    if part.lower().endswith("mhz"):
                        try:
                            val = int(part.lower().replace("mhz", ""))
                            if target == "sclk": sclk = val
                            else: mclk = val
                        except Exception:
                            pass
                break
    return sclk, mclk


def _gpu_voltage_offset():
    if _GPU_PATH is None: return None
    raw = _read_str_file(_GPU_PATH / "pp_od_clk_voltage")
    if not raw: return None
    found = False
    for line in raw.splitlines():
        if "OD_VDDGFX_OFFSET" in line.upper():
            found = True; continue
        if found:
            try: return int(line.strip().lower().replace("mv", "").strip())
            except Exception: return None
    return None


def _gpu_performance_level():
    if _GPU_PATH is None: return None
    return _read_str_file(_GPU_PATH / "power_dpm_force_performance_level")


def _gpu_power_profile():
    if _GPU_PATH is None: return None
    raw = _read_str_file(_GPU_PATH / "pp_power_profile_mode")
    if not raw: return None
    for line in raw.splitlines():
        if "*" in line:
            m = re.search(r'\d+\s+([\w_]+)\s*\*', line)
            if m:
                return m.group(1).replace("_", " ").title()
    return None


def _find_nvidia_gpu() -> bool:
    """True iff a DRM card with PCI vendor 0x10de exists AND nvidia-smi is callable."""
    try:
        for card in Path("/sys/class/drm").glob("card[0-9]*"):
            if "-" in card.name:
                continue
            try:
                if (card / "device" / "vendor").read_text().strip() == "0x10de":
                    return shutil.which("nvidia-smi") is not None
            except Exception:
                pass
    except Exception as e:
        log.debug("Nvidia GPU sysfs scan failed: %s", e)
    return False


_NVIDIA_PRESENT: Any = _UNPROBED
_AMD_NAME: Any = _UNPROBED
_NV_NAME: Any = _UNPROBED


def _lspci_amd_name() -> "str | None":
    if _GPU_PATH is None:
        return None
    if shutil.which("lspci") is None:
        return None
    try:
        slot = _GPU_PATH.parent.name
        out = subprocess.check_output(
            ["lspci", "-s", slot, "-mm", "-nn"],
            text=True, timeout=3, stderr=subprocess.DEVNULL, close_fds=True,
        ).strip()
    except Exception as e:
        log.debug("lspci amd name: %s", e)
        return None
    parts = [p[0] or p[1] for p in re.findall(r'"([^"]*)"|(\S+)', out)]
    if len(parts) >= 3:
        return parts[2]
    return None


def _nvidia_smi_name() -> "str | None":
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            text=True, timeout=3, stderr=subprocess.DEVNULL, close_fds=True,
        ).strip().splitlines()
    except Exception as e:
        log.debug("nvidia-smi name: %s", e)
        return None
    return out[0].strip() if out else None


def _ensure_probed() -> None:
    # Run sysfs/nvidia-smi probes once on first call so module import is cheap
    # and COLLECT_GPU_ENABLED=false can suppress them entirely.
    global _GPU_PATH, _HWMON, _NVIDIA_PRESENT, _AMD_NAME, _NV_NAME
    if _GPU_PATH is _UNPROBED:
        _GPU_PATH = _find_amd_gpu_path()
        _HWMON = _hwmon_dir()
        _NVIDIA_PRESENT = _find_nvidia_gpu()
        _AMD_NAME = _lspci_amd_name() if _GPU_PATH is not None else None
        _NV_NAME = _nvidia_smi_name() if _NVIDIA_PRESENT else None

# Field order MUST match the parsing below. fan.speed is %, not RPM.
_NV_QUERY_FIELDS = [
    "temperature.gpu",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "power.draw",
    "power.limit",
    "clocks.current.graphics",
    "clocks.current.memory",
    "fan.speed",
]


def _collect_nvidia_gpu() -> dict:
    """AMD-key-compatible dict for the first NVIDIA card; AMD-only fields stay None."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=" + ",".join(_NV_QUERY_FIELDS),
             "--format=csv,noheader,nounits"],
            text=True, timeout=5, close_fds=True, stderr=subprocess.DEVNULL,
        ).strip().splitlines()
    except Exception as e:
        log.debug("nvidia-smi failed: %s", e)
        return {}
    if not out:
        return {}
    parts = [p.strip() for p in out[0].split(",")]
    if len(parts) < len(_NV_QUERY_FIELDS):
        return {}

    def _f(s):
        try: return float(s)
        except Exception: return None

    (temp_c, util, mem_used_mb, mem_total_mb, power_w, power_cap_w,
     sclk, mclk, fan_pct) = (_f(p) for p in parts)
    used_bytes  = round(mem_used_mb  * 1_048_576) if mem_used_mb  is not None else None
    total_bytes = round(mem_total_mb * 1_048_576) if mem_total_mb is not None else None
    vram_pct = (
        mem_used_mb / mem_total_mb * 100.0
        if mem_used_mb is not None and mem_total_mb
        else None
    )

    return {
        "name":                   _NV_NAME,
        "temperature_c":          temp_c,
        "temperature_junction_c": None,
        "temperature_memory_c":   None,
        "vddgfx_mv":              None,
        "fan1_rpm":               None,
        "fan_percent":            fan_pct,
        "vram_used_bytes":        used_bytes,
        "vram_used_mb":           int(mem_used_mb) if mem_used_mb is not None else None,
        "vram_total_bytes":       total_bytes,
        "vram_usage_percent":     vram_pct,
        "gpu_util_percent":       util,
        "power_watts":            power_w,
        "power_cap_watts":        power_cap_w,
        "sclk_mhz":               int(sclk) if sclk is not None else None,
        "mclk_mhz":               int(mclk) if mclk is not None else None,
        "voltage_offset_mv":      None,
        "performance_level":      None,
        "power_profile":          None,
        "vendor":                 "nvidia",
    }


def collect_gpu() -> dict:
    """GPU metrics via AMD sysfs or nvidia-smi; {} on hosts with neither."""
    if not getattr(_deps.config, "COLLECT_GPU_ENABLED", True):
        return {}
    _ensure_probed()
    if _GPU_PATH is not None:
        vram_used, vram_total, vram_pct = _gpu_vram()
        sclk, mclk = _gpu_clocks()
        perf = _gpu_performance_level()
        vram_mb = round(vram_used / 1_048_576) if vram_used is not None else None
        power_avg = _read_int_file(_HWMON / "power1_average") if _HWMON else None
        power_cap = _read_int_file(_HWMON / "power1_cap") if _HWMON else None
        fan_rpm   = _read_int_file(_HWMON / "fan1_input") if _HWMON else None
        vddgfx    = _read_int_file(_HWMON / "in0_input") if _HWMON else None
        return {
            "name":                   _AMD_NAME,
            "temperature_c":          _gpu_temp("1"),
            "temperature_junction_c": _gpu_temp("2"),
            "temperature_memory_c":   _gpu_temp("3"),
            "vddgfx_mv":              vddgfx,
            "fan1_rpm":               fan_rpm,
            "vram_used_bytes":        vram_used,
            "vram_used_mb":           vram_mb,
            "vram_total_bytes":       vram_total,
            "vram_usage_percent":     vram_pct,
            "gpu_util_percent":       _read_float_file(_GPU_PATH / "gpu_busy_percent") if _GPU_PATH else None,
            "power_watts":            power_avg / 1_000_000.0 if power_avg is not None else None,
            "power_cap_watts":        power_cap / 1_000_000.0 if power_cap is not None else None,
            "sclk_mhz":               sclk,
            "mclk_mhz":               mclk,
            "voltage_offset_mv":      _gpu_voltage_offset(),
            "performance_level":      perf,
            "power_profile":          _gpu_power_profile(),
            "vendor":                 "amd",
        }
    if _NVIDIA_PRESENT:
        return _collect_nvidia_gpu()
    return {}
