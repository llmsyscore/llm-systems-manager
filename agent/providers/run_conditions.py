"""Run conditions (hardware, OS, backend, model, start/end snapshots) for benchmark results.
psutil and the collectors are imported lazily inside functions."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("llm-systems-agent.providers.run_conditions")

SCHEMA = 1
SERVER_CMD_MAX = 2000
_SECRET_FLAGS = ("--api-key", "--api-key-file", "--hf-token", "-hft", "--ssl-key-file", "--admin-key")
_SECRET_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:token|secret|key|password|passwd)[A-Za-z0-9_]*)=.+$", re.IGNORECASE)


def redact_cmd(cmd: "str | None") -> "str | None":
    """Server command line with the value after any secret-bearing flag, or in KEY=VALUE tokens, replaced by ***."""
    if not isinstance(cmd, str):
        return cmd
    out, hide = [], False
    for tok in cmd.split(" "):
        if hide and tok:
            out.append("***")
            hide = False
            continue
        flag, eq, _val = tok.partition("=")
        if tok in _SECRET_FLAGS:
            out.append(tok)
            hide = True
        elif (eq and flag in _SECRET_FLAGS) or _SECRET_KV_RE.match(tok):
            out.append(f"{flag}=***")
        else:
            out.append(tok)
    return " ".join(out)
COND_MAX_BYTES = 6144

_OS_RELEASE = Path("/etc/os-release")
_HOST_CACHE: "dict | None" = None
_CHUNK = 4 << 20
_SHARD_RE = re.compile(r"-\d{5}-of-(\d{5})")


def _reset_cache() -> None:
    global _HOST_CACHE
    _HOST_CACHE = None


def file_identity(path: "str | Path | None") -> "dict | None":
    """Name, size, head/tail hash and shard count of a model file; None if not a file."""
    if not path:
        return None
    p = Path(path)
    try:
        if not p.is_file():
            return None
        size = p.stat().st_size
        digest = _ht_hash(p, size)
    except OSError as e:
        log.debug("file identity %s: %s", p, e)
        return None
    return {"file": p.name, "size_bytes": size, "hash": digest, "shards": _shard_count(p)}


def _ht_hash(p: Path, size: int) -> str:
    """sha256 over the first and last 4 MiB plus the size."""
    h = hashlib.sha256()
    with p.open("rb") as fh:
        if size <= 2 * _CHUNK:
            h.update(fh.read())
        else:
            h.update(fh.read(_CHUNK))
            fh.seek(size - _CHUNK)
            h.update(fh.read(_CHUNK))
    h.update(str(size).encode())
    return "ht-sha256:" + h.hexdigest()[:12]


def dir_identity(path: "str | Path | None") -> "dict | None":
    """Name, weight-file size sum, config.json hash and weight-file count of a model dir."""
    if not path:
        return None
    p = Path(path)
    try:
        cfg = p / "config.json"
        if not p.is_dir() or not cfg.is_file():
            return None
        weights = [w for w in p.iterdir() if w.suffix in (".safetensors", ".bin") and w.is_file()]
        size = sum(w.stat().st_size for w in weights)
        digest = _ht_hash(cfg, cfg.stat().st_size)
    except OSError as e:
        log.debug("dir identity %s: %s", p, e)
        return None
    return {"file": p.name, "size_bytes": size, "hash": digest, "shards": len(weights)}


def _shard_count(p: Path) -> "int | None":
    m = _SHARD_RE.search(p.name)
    if not m:
        return None
    key = _SHARD_RE.sub("", p.name)
    try:
        names = [s.name for s in p.parent.iterdir()]
    except OSError:
        return None
    count = 0
    for name in names:
        sm = _SHARD_RE.search(name)
        if sm and sm.group(1) == m.group(1) and _SHARD_RE.sub("", name) == key:
            count += 1
    return count


def _cpu_name() -> "str | None":
    try:
        from collectors.system import read_cpu_name  # type: ignore
        return read_cpu_name()
    except Exception as e:
        log.debug("cpu name: %s", e)
    return None


def _cpu_counts() -> "tuple[int | None, int | None]":
    try:
        import psutil  # type: ignore
        return psutil.cpu_count(logical=False), psutil.cpu_count(logical=True)
    except Exception:
        return None, os.cpu_count()


def _ram_total() -> "int | None":
    try:
        import psutil  # type: ignore
        return int(psutil.virtual_memory().total)
    except Exception:
        return None


def _os_name() -> "str | None":
    if sys.platform == "darwin":
        try:
            ver = subprocess.check_output(["sw_vers", "-productVersion"], text=True, timeout=3,
                                          stderr=subprocess.DEVNULL, close_fds=True).strip()
            return "macOS " + ver if ver else "macOS"
        except Exception:
            return "macOS"
    if sys.platform.startswith("linux"):
        try:
            kv = {}
            for line in _OS_RELEASE.read_text().splitlines():
                k, _, v = line.partition("=")
                kv[k.strip()] = v.strip().strip('"').strip("'")
            name = kv.get("PRETTY_NAME") or kv.get("NAME")
            if name:
                return name
        except OSError:
            pass  # unreadable os-release: fall through to the platform name
    return platform.system() or None


def host_facts() -> dict:
    """CPU, core counts, RAM and OS of this host; cached per process."""
    global _HOST_CACHE
    if _HOST_CACHE is None:
        cores, threads = _cpu_counts()
        _HOST_CACHE = {"cpu": _cpu_name(), "cpu_cores": cores, "cpu_threads": threads,
                       "ram_bytes": _ram_total(),
                       "os": {"name": _os_name(), "kernel": platform.release() or None}}
    return _HOST_CACHE


def gpu_label_from(sample: "dict | None") -> "tuple[str | None, str | None]":
    """(label, driver) from a collect_gpu sample: name, "N× name" or "a + b" for several cards."""
    if not isinstance(sample, dict):
        return None, None
    gpus = sample.get("gpus")
    names = [str(g.get("name")) for g in gpus if isinstance(g, dict) and g.get("name")] if isinstance(gpus, list) else []
    if len(names) > 1:
        label = f"{len(names)}× {names[0]}" if len(set(names)) == 1 else " + ".join(names)
    else:
        label = sample.get("name") or (names[0] if names else None)
    return label, sample.get("driver")


def _driver_of(gpu_sample: "dict | None") -> "str | None":
    return gpu_sample.get("driver") if isinstance(gpu_sample, dict) else None


def _round(v: Any) -> "int | None":
    try:
        return None if v is None else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def snapshot_from(gpu_sample: "dict | None", sys_sample: "dict | None") -> "dict | None":
    """Thermal, clock, power and memory readings from collector samples."""
    g = gpu_sample if isinstance(gpu_sample, dict) else {}
    s = sys_sample if isinstance(sys_sample, dict) else {}
    if not g and not s:
        return None
    ram = s.get("ram") if isinstance(s.get("ram"), dict) else {}
    out = {
        "temp_c": _round(g.get("temperature_c")),
        "junction_c": _round(g.get("temperature_junction_c")),
        "sclk_mhz": _round(g.get("sclk_mhz")),
        "mclk_mhz": _round(g.get("mclk_mhz")),
        "power_w": _round(g.get("power_watts")),
        "power_cap_w": _round(g.get("power_cap_watts")),
        "performance_level": g.get("performance_level"),
        "cpu_governor": s.get("cpu_governor"),
        "ram_available_bytes": ram.get("available_bytes"),
    }
    gpus = g.get("gpus")
    if isinstance(gpus, list) and len(gpus) > 1:
        out["cards"] = len(gpus)
    return out


_SAMPLE_SOURCE: "Callable[[], dict | None] | None" = None


def set_sample_source(fn: "Callable[[], dict | None] | None") -> None:
    """Register a zero-arg callable returning the collector's last metric sample."""
    global _SAMPLE_SOURCE
    _SAMPLE_SOURCE = fn


def _cheap_system_sample() -> "dict | None":
    out: dict = {}
    try:
        from collectors.system import read_cpu_governor  # type: ignore
        out["cpu_governor"] = read_cpu_governor()
    except Exception as e:
        log.debug("conditions governor sample: %s", e)
    try:
        import psutil  # type: ignore
        out["ram"] = {"available_bytes": psutil.virtual_memory().available}
    except Exception as e:
        log.debug("conditions ram sample: %s", e)
    return out or None


def samples() -> "tuple[dict | None, dict | None]":
    """GPU and system dicts of the collector's last sample; a cheap local system sample when none."""
    last = None
    try:
        last = _SAMPLE_SOURCE() if callable(_SAMPLE_SOURCE) else None
    except Exception as e:
        log.debug("conditions sample source: %s", e)
    system = last.get("system") if isinstance(last, dict) and isinstance(last.get("system"), dict) else None
    gpu = system.get("gpu") if system and isinstance(system.get("gpu"), dict) else None
    return gpu, (system if system is not None else _cheap_system_sample())


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start(facts: dict, gpu_sample: "dict | None" = None, sys_sample: "dict | None" = None,
          now: "Callable[[], str] | None" = None) -> dict:
    """Conditions block at run start from provider facts and a start snapshot."""
    facts = facts or {}
    host = dict(host_facts())
    os_info = host.pop("os", None)
    gpus = gpu_sample.get("gpus") if isinstance(gpu_sample, dict) else None
    label = facts.get("gpu_label")
    gpu_count = len(gpus) if isinstance(gpus, list) and gpus else (1 if label else None)
    ident = file_identity(facts.get("model_path")) or dir_identity(facts.get("model_path")) or {
        "file": None, "size_bytes": None, "hash": None, "shards": None}
    return {
        "schema": SCHEMA,
        "captured_start": (now or _utc_now)(),
        "hardware": {"gpu": label, "gpu_count": gpu_count,
                     "driver": facts.get("driver") or _driver_of(gpu_sample), **host},
        "os": os_info,
        "backend": {"name": facts.get("backend"), "build": facts.get("backend_build"),
                    "server_cmd": redact_cmd(facts.get("server_cmd"))},
        "model": {"id": facts.get("model_id"), **ident, "quantization": facts.get("quantization")},
        "context": facts.get("context"),
        "sampling": facts.get("sampling"),
        "snapshot": {"start": snapshot_from(gpu_sample, sys_sample)},
    }


def finish(cond: dict, gpu_sample: "dict | None" = None, sys_sample: "dict | None" = None,
           now: "Callable[[], str] | None" = None) -> dict:
    """Copy of the block with the end timestamp and end snapshot added."""
    out = dict(cond)
    out["captured_end"] = (now or _utc_now)()
    out["snapshot"] = {**(cond.get("snapshot") or {}), "end": snapshot_from(gpu_sample, sys_sample)}
    return out


def attach(env: dict, doc: dict) -> dict:
    """Sets doc["conditions"] to the finished block when env carries one."""
    cond = env.get("conditions") if isinstance(env, dict) else None
    if isinstance(cond, dict):
        doc["conditions"] = finish(cond, *samples())
    return doc


def compact(cond: "dict | None") -> "dict | None":
    """Size-capped copy: trims the server command, then drops optional parts."""
    if cond is None:
        return None
    out = copy.deepcopy(cond)
    backend = out.get("backend") if isinstance(out.get("backend"), dict) else None
    if backend and isinstance(backend.get("server_cmd"), str):
        backend["server_cmd"] = backend["server_cmd"][:SERVER_CMD_MAX]

    def _too_big() -> bool:
        return len(json.dumps(out, default=str)) > COND_MAX_BYTES

    if _too_big():
        out.pop("sampling", None)
    if _too_big() and isinstance(out.get("snapshot"), dict):
        out["snapshot"].pop("end", None)
    if _too_big() and backend:
        backend.pop("server_cmd", None)
    return out
