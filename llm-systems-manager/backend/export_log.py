"""Last manual backup export per component, persisted as a small JSON under
the data dir so the Backups card survives a restart."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("llm-systems-manager.export-log")

COMPONENTS = ("manager", "alarm_engine")

_lock = threading.Lock()
_path: "Path | None" = None
_cache: dict = {}


def configure(path) -> None:
    """Bind the on-disk store; before this every call is a no-op read."""
    global _path, _cache
    with _lock:
        _path = Path(path)
        _cache = _read()


def _read() -> dict:
    if _path is None or not _path.is_file():
        return {}
    try:
        data = json.loads(_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    if _path is None:
        return
    tmp = f"{_path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(_path))
    except OSError as e:
        log.warning("export log write failed: %s", e)


def record(component: str, nbytes, ts: "float | None" = None) -> None:
    """Note a successful export of `component` and its archive size."""
    if component not in COMPONENTS:
        return
    try:
        size = int(nbytes or 0)
    except (TypeError, ValueError):
        size = 0
    entry = {"ts": time.time() if ts is None else float(ts), "bytes": size}
    with _lock:
        data = dict(_cache) or _read()
        data[component] = entry
        _cache.clear()
        _cache.update(data)
        _write(data)


def last_export() -> dict:
    """{component: {ts, bytes} | None} for every known component."""
    with _lock:
        data = _cache or _read()
        return {c: (dict(data[c]) if isinstance(data.get(c), dict) else None)
                for c in COMPONENTS}


def reset() -> None:
    with _lock:
        _cache.clear()
