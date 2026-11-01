"""GPU metrics collector — AMD sysfs first, NVIDIA via nvidia-smi second.

Vendor probes happen on the first ``collect_gpu()``:
  - ``_GPU_PATH``: the AMD card (vendor 0x1002) named by ``GPU_DEVICE``, else
    the one with the most VRAM
  - ``_HWMON``: the hwmon subdir under ``_GPU_PATH``
  - ``_NVIDIA_PRESENT``: True iff an NVIDIA card AND ``nvidia-smi`` exist (and
    ``GPU_DEVICE``, when set, names one); ``_NV_ID`` is its ``nvidia-smi -i`` id
  - ``_CARDS``: every AMD and NVIDIA card found, for the Admin GPU picker

A box with neither just returns ``{}`` from ``collect_gpu()``, and re-probes
on the ``AbsenceLatch`` interval so a driver that comes up after the agent
isn't missed for the life of the process. The ``_read_*_file`` helpers stay
private — only this module's GPU code uses them.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling

from ._shared import AbsenceLatch, collect_enabled, latch_level_for

log = logging.getLogger("llm-systems-agent.collectors.gpu")

__all__ = ["set_deps", "collect_gpu", "gpu_cards"]

_deps = SimpleNamespace()


def set_deps(*, config) -> None:
    _deps.config = config
    _probe_latch.reset()
    _probe_latch.level = latch_level_for(config, "COLLECT_GPU_ENABLED")


# Lazy-probed on first collect_gpu() so module import doesn't sysfs-walk on every host.
_UNPROBED = object()
_GPU_PATH: Any = _UNPROBED
_HWMON: Any = _UNPROBED


def _amd_cards(drm_root: Path) -> list:
    cards = []
    for card in sorted(drm_root.glob("card[0-9]*")):
        if "-" in card.name:
            continue
        with best_effort("gpu: read AMD card vendor id", log=log):
            if (card / "device" / "vendor").read_text().strip() == "0x1002":
                cards.append(card)
    return cards


def _card_matches(card: Path, sel: str) -> bool:
    """True when GPU_DEVICE names this card: cardN, a PCI address, or a sysfs path."""
    if sel == card.name:
        return True
    try:
        dev = (card / "device").resolve()
    except OSError:
        return False
    if ":" in sel and _pci_tail(dev.name) == _pci_tail(sel):
        return True
    if sel.startswith("/"):
        try:
            target = Path(sel).resolve()
        except OSError:
            return False
        return target in (dev, card.resolve())
    return False


_DRM_ROOT = Path("/sys/class/drm")
_GPU_DEVICE_WARNED: "set[str]" = set()


def _gpu_device_sel() -> str:
    return str(getattr(getattr(_deps, "config", None), "GPU_DEVICE", "") or "").strip()


def _find_amd_gpu_path(drm_root: Path = _DRM_ROOT):
    # GPU_DEVICE picks a card; unset picks the AMD card with the most VRAM.
    sel = _gpu_device_sel()
    try:
        cards = _amd_cards(drm_root)
    except Exception as e:
        log.debug("AMD GPU sysfs scan failed: %s", e)
        return None
    if not cards:
        return None
    if sel:
        return next((c / "device" for c in cards if _card_matches(c, sel)), None)
    best = max(cards, key=lambda c: _read_int_file(c / "device" / "mem_info_vram_total", -1))
    return best / "device"


def _pci_tail(addr: str) -> str:
    """'00000000:01:00.0' / '0000:01:00.0' / '01:00.0' → '01:00.0'."""
    return ":".join(addr.strip().lower().split(":")[-2:])


def _sel_pci(sel: str, drm_root: Path = _DRM_ROOT) -> "str | None":
    """PCI tail named by GPU_DEVICE as a PCI address, cardN or sysfs path."""
    if sel.startswith("card") or sel.startswith("/"):
        path = drm_root / sel if sel.startswith("card") else Path(sel)
        try:
            dev = path.resolve()
            if dev.name.startswith("card"):
                dev = (dev / "device").resolve()
            return _pci_tail(dev.name)
        except OSError:
            return None
    return _pci_tail(sel) if sel.count(":") >= 1 else None


def _nvidia_gpus() -> list:
    """(index, pci.bus_id, uuid, memory MiB, name) per nvidia-smi card; [] on failure."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,pci.bus_id,uuid,memory.total,name",
             "--format=csv,noheader,nounits"],
            text=True, timeout=3, stderr=subprocess.DEVNULL, close_fds=True,
        ).strip().splitlines()
    except Exception as e:
        log.debug("nvidia-smi list: %s", e)
        return []
    gpus = []
    for line in out:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            try: mem = float(parts[3])
            except ValueError: mem = -1.0
            gpus.append((parts[0], parts[1], parts[2], mem, ", ".join(parts[4:])))
    return gpus


def _select_nvidia_id(drm_root: Path = _DRM_ROOT) -> "str | None":
    """nvidia-smi -i id of the card to monitor; "" = nvidia-smi default; None = no match."""
    sel = _gpu_device_sel()
    gpus = _nvidia_gpus()
    if not gpus:
        return None if sel else ""
    if sel:
        pci = _sel_pci(sel, drm_root)
        for idx, bus, uuid, *_ in gpus:
            if sel == idx or sel.lower() == uuid.lower() or (pci and pci == _pci_tail(bus)):
                return bus
        return None
    return max(gpus, key=lambda g: g[3])[1]


def _warn_unmatched_device(sel: str) -> None:
    level = logging.DEBUG if sel in _GPU_DEVICE_WARNED else logging.WARNING
    _GPU_DEVICE_WARNED.add(sel)
    log.log(level, "GPU_DEVICE=%r matches no AMD or NVIDIA card", sel)


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


def _gpu_gtt():
    """GTT (host memory the GPU maps) used/total/percent; None where unreadable."""
    if _GPU_PATH is None: return None, None, None
    used  = _read_int_file(_GPU_PATH / "mem_info_gtt_used")
    total = _read_int_file(_GPU_PATH / "mem_info_gtt_total")
    pct = (used / total) * 100.0 if used is not None and total else None
    return used, total, pct


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
                        except ValueError:
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
            with best_effort("gpu: read NVIDIA card vendor id", log=log):
                if (card / "device" / "vendor").read_text().strip() == "0x10de":
                    return shutil.which("nvidia-smi") is not None
    except Exception as e:
        log.debug("Nvidia GPU sysfs scan failed: %s", e)
    return False


_NVIDIA_PRESENT: Any = _UNPROBED
_AMD_NAME: Any = _UNPROBED
_NV_NAME: Any = _UNPROBED
_NV_ID = ""
_DRIVER: "str | None" = None


def _nv_id_args() -> list:
    return ["-i", _NV_ID] if _NV_ID else []


def _lspci_amd_name() -> "str | None":
    if _GPU_PATH is None:
        return None
    try:
        slot = _GPU_PATH.resolve().name
    except OSError as e:
        log.debug("lspci amd name: %s", e)
        return None
    return _lspci_name(slot)


def _lspci_name(slot: str) -> "str | None":
    if shutil.which("lspci") is None:
        return None
    try:
        out = subprocess.check_output(
            ["lspci", "-s", slot, "-vmm"],
            text=True, timeout=3, stderr=subprocess.DEVNULL, close_fds=True,
        ).strip()
    except Exception as e:
        log.debug("lspci amd name: %s", e)
        return None
    # -vmm emits position-independent "Device:\t<model>" key lines.
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "Device" and value.strip():
            return value.strip()
    return None


def _nvidia_smi_name() -> "str | None":
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.check_output(
            ["nvidia-smi", *_nv_id_args(), "--query-gpu=name", "--format=csv,noheader"],
            text=True, timeout=3, stderr=subprocess.DEVNULL, close_fds=True,
        ).strip().splitlines()
    except Exception as e:
        log.debug("nvidia-smi name: %s", e)
        return None
    return out[0].strip() if out else None


def _probe_driver() -> "str | None":
    """Kernel/driver version string for the detected GPU vendor, or None."""
    if _GPU_PATH not in (None, _UNPROBED):
        ver = _read_str_file("/sys/module/amdgpu/version")
        return "amdgpu " + ver if ver else None
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.check_output(
            ["nvidia-smi", *_nv_id_args(), "--query-gpu=driver_version", "--format=csv,noheader"],
            text=True, timeout=3, stderr=subprocess.DEVNULL, close_fds=True,
        ).strip().splitlines()
    except Exception as e:
        log.debug("nvidia-smi driver: %s", e)
        return None
    return "nvidia " + out[0].strip() if out and out[0].strip() else None


def _pci_id(addr: str) -> str:
    """'00000000:01:00.0' → '0000:01:00.0' (sysfs form)."""
    dom, _, rest = addr.strip().lower().partition(":")
    return f"{dom[-4:]}:{rest}" if rest else dom


def _list_cards(drm_root: Path = _DRM_ROOT) -> list:
    """Every AMD card in sysfs and NVIDIA card in nvidia-smi: id (PCI address), vendor, name, VRAM."""
    cards = []
    with best_effort("gpu: list AMD cards", log=log):
        for card in _amd_cards(drm_root):
            try:
                slot = (card / "device").resolve().name
            except OSError:
                continue
            cards.append({"id": _pci_id(slot), "vendor": "amd", "name": _lspci_name(slot),
                          "vram_bytes": _read_int_file(card / "device" / "mem_info_vram_total")})
    with best_effort("gpu: list NVIDIA cards", log=log):
        for _idx, bus, _uuid, mem, name in (_nvidia_gpus() if shutil.which("nvidia-smi") else []):
            cards.append({"id": _pci_id(bus), "vendor": "nvidia", "name": name or None,
                          "vram_bytes": round(mem * 1_048_576) if mem >= 0 else None})
    return cards


def _monitored_pci() -> "str | None":
    if _GPU_PATH not in (None, _UNPROBED):
        try:
            return _pci_tail(_GPU_PATH.resolve().name)
        except OSError:
            return None
    if _NVIDIA_PRESENT is True and _NV_ID:
        return _pci_tail(_NV_ID)
    return None


def gpu_cards() -> list:
    """Cards found at the last probe, the monitored one flagged; [] before the first probe."""
    if _CARDS is _UNPROBED:
        return []
    cur = _monitored_pci()
    return [{**c, "monitored": cur is not None and _pci_tail(c["id"]) == cur} for c in _CARDS]


_CARDS: Any = _UNPROBED
_probe_latch = AbsenceLatch("GPU (COLLECT_GPU_ENABLED is on)", logger=log)


def _probe_complete() -> bool:
    """True when the last probe resolved a GPU we can actually read."""
    if _GPU_PATH is _UNPROBED:
        return False
    return (_GPU_PATH is not None and _HWMON is not None) or bool(_NVIDIA_PRESENT)


def _ensure_probed() -> None:
    # Probe sysfs/nvidia-smi on first call, then only on the latch interval
    # while nothing is found, so a late driver is picked up without a restart.
    global _GPU_PATH, _HWMON, _NVIDIA_PRESENT, _AMD_NAME, _NV_NAME, _NV_ID, _DRIVER, _CARDS
    if _probe_complete() or not _probe_latch.should_probe():
        return
    _GPU_PATH = _find_amd_gpu_path()
    _HWMON = _hwmon_dir()
    _NVIDIA_PRESENT = False
    if _GPU_PATH is None and _find_nvidia_gpu():
        nv_id = _select_nvidia_id()
        _NVIDIA_PRESENT, _NV_ID = nv_id is not None, nv_id or ""
    sel = _gpu_device_sel()
    if sel and _GPU_PATH is None and not _NVIDIA_PRESENT:
        _warn_unmatched_device(sel)
    _AMD_NAME = _lspci_amd_name() if _GPU_PATH is not None else None
    _NV_NAME = _nvidia_smi_name() if _NVIDIA_PRESENT else None
    _DRIVER = _probe_driver() if _GPU_PATH is not None or _NVIDIA_PRESENT else None
    _CARDS = _list_cards()
    _probe_latch.record(
        _probe_complete(),
        " (AMD card found, hwmon sensors missing)" if _GPU_PATH is not None else "",
    )

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
    """AMD-key-compatible dict for the selected NVIDIA card; AMD-only fields stay None."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", *_nv_id_args(),
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
        "gtt_used_bytes":         None,
        "gtt_total_bytes":        None,
        "gtt_usage_percent":      None,
        "gpu_util_percent":       util,
        "power_watts":            power_w,
        "power_cap_watts":        power_cap_w,
        "sclk_mhz":               int(sclk) if sclk is not None else None,
        "mclk_mhz":               int(mclk) if mclk is not None else None,
        "voltage_offset_mv":      None,
        "performance_level":      None,
        "power_profile":          None,
        "vendor":                 "nvidia",
        "driver":                 _DRIVER,
    }


def collect_gpu() -> dict:
    """GPU metrics via AMD sysfs or nvidia-smi; {} on hosts with neither."""
    if not collect_enabled(_deps.config, "COLLECT_GPU_ENABLED"):
        return {}
    _ensure_probed()
    if _GPU_PATH is not None:
        vram_used, vram_total, vram_pct = _gpu_vram()
        gtt_used, gtt_total, gtt_pct = _gpu_gtt()
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
            "gtt_used_bytes":         gtt_used,
            "gtt_total_bytes":        gtt_total,
            "gtt_usage_percent":      gtt_pct,
            "gpu_util_percent":       _read_float_file(_GPU_PATH / "gpu_busy_percent") if _GPU_PATH else None,
            "power_watts":            power_avg / 1_000_000.0 if power_avg is not None else None,
            "power_cap_watts":        power_cap / 1_000_000.0 if power_cap is not None else None,
            "sclk_mhz":               sclk,
            "mclk_mhz":               mclk,
            "voltage_offset_mv":      _gpu_voltage_offset(),
            "performance_level":      perf,
            "power_profile":          _gpu_power_profile(),
            "vendor":                 "amd",
            "driver":                 _DRIVER,
        }
    if _NVIDIA_PRESENT:
        return _collect_nvidia_gpu()
    return {}
