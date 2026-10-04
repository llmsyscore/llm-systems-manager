"""Crash-safe file writes: the data reaches the disk before the rename does."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Union


def fsync_dir(path: Union[str, Path]) -> None:
    """Flushes a directory's entries to disk; a no-op where the OS can't."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def write_durable(path: Union[str, Path], data: Union[str, bytes],
                  mode: int = 0o600) -> None:
    """Writes `data` to a pid-unique temp, flushes it, renames it over `path`."""
    p = Path(path)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    payload = data.encode("utf-8") if isinstance(data, str) else data
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(p.parent)
