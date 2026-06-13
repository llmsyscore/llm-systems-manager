"""Tiny shared helpers used by both llm-systems-agent.py and
buffered_metric_client.py. Keep this module dependency-free (stdlib only)
so importing it never fails on a partial install."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union


def atomic_write_text(
    path: Union[str, Path],
    content: str,
    mode: int | None = None,
    encoding: str = "utf-8",
) -> None:
    """Write `content` to `path` atomically via a sibling .tmp + rename.

    Why atomic: readers (the manager fetching TLS bundles, the agent
    re-reading log-watch state on restart) must never observe a half-written
    file. `replace()` is atomic on POSIX and Windows.

    `mode` is applied to the temp file before the rename so the final file
    is never briefly world-readable.
    """
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    if mode is not None:
        os.chmod(tmp, mode)
    tmp.replace(p)


def atomic_write_bytes(path: Union[str, Path], data: bytes, mode: int | None = None) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    if mode is not None:
        os.chmod(tmp, mode)
    tmp.replace(p)
