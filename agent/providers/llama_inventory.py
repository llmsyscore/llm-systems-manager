"""Read-only inventory of llama.cpp builds installed on this host (#1389).
Stdlib-only leaf module."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Optional

BIN_NAME = "llama-server"
MAX_BUILDS = 20
MAX_DEPTH = 4
MAX_DIRS = 5000
PROBE_TIMEOUT_S = 10
PROBE_WORKERS = 4
BUILD_MARKER = ".llama-build.json"
_SKIP_DIRS = frozenset({"CMakeFiles", "node_modules", "__pycache__"})

# Library-name prefix → backend label; first GPU match wins over CPU.
_BACKEND_LIBS = (
    ("libggml-vulkan", "Vulkan"), ("libggml-hip", "ROCm"), ("libggml-cuda", "CUDA"),
    ("libggml-metal", "Metal"), ("libggml-sycl", "SYCL"), ("libggml-cpu", "CPU"),
)
_MARKER_BACKENDS = {"vulkan": "Vulkan", "rocm": "ROCm", "hip": "ROCm", "cuda": "CUDA",
                    "metal": "Metal", "sycl": "SYCL", "cpu": "CPU"}

_lock = threading.Lock()
_state: dict = {"last": None, "gen": 0}


def _real(p) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _is_bin(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _walk(root: Path, found: list, budget: list) -> None:
    """Depth-limited search under root; hidden dirs (backups included) are skipped."""
    stack = [(root, 0)]
    while stack and budget[0] > 0:
        d, depth = stack.pop()
        budget[0] -= 1
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if e.name == BIN_NAME and _is_bin(Path(e.path)):
                    found.append(e.path)
                elif (depth < MAX_DEPTH and e.is_dir(follow_symlinks=False)
                      and not e.name.startswith(".") and e.name not in _SKIP_DIRS):
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                continue


def discover(llama_bin: str, roots: Iterable[str], which: Optional[str]) -> "tuple[list[str], bool]":
    """(real paths of llama-server binaries, truncated); LLAMA_BIN first, deduped."""
    found: list[str] = []
    budget = [MAX_DIRS]
    for r in roots:
        if not r:
            continue
        p = Path(os.path.expanduser(str(r)))
        if _is_bin(p) and p.name == BIN_NAME:
            found.append(str(p))
        elif p.is_dir():
            _walk(p, found, budget)
    if which:
        found.append(which)
    found.sort()
    if llama_bin:
        found.insert(0, llama_bin)
    out: list[str] = []
    seen: set = set()
    for f in found:
        rp = _real(f)
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out[:MAX_BUILDS], len(out) > MAX_BUILDS


def detect_backend(bin_dir: Path) -> str:
    """Backend label from the build marker, else the ggml libraries beside the binary."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        with open(bin_dir / BUILD_MARKER, encoding="utf-8") as f:
            m = json.load(f)
        lbl = _MARKER_BACKENDS.get(str((m or {}).get("backend") or "").lower())
        if lbl:
            return lbl
    names: list[str] = []
    for d in (bin_dir, bin_dir.parent / "lib"):
        try:
            names.extend(os.listdir(d))
        except OSError:
            continue
    hits = [lbl for pre, lbl in _BACKEND_LIBS if any(n.startswith(pre) for n in names)]
    gpu = [h for h in hits if h != "CPU"]
    if gpu:
        return " + ".join(gpu)
    return hits[0] if hits else "Unknown"


def _note_for(path: str, notes: dict) -> str:
    """Note whose key is the binary or its folder (paths compared after realpath)."""
    folder = os.path.dirname(path)
    for k, v in (notes or {}).items():
        rk = _real(k)
        if rk in (path, folder):
            return str(v or "")[:200]
    return ""


def describe(path: str, active_real: str, notes: dict, probe: Callable) -> dict:
    p = Path(path)
    row = {"path": path, "dir": str(p.parent), "active": path == active_real,
           "backend": detect_backend(p.parent), "note": _note_for(path, notes),
           "version": None, "build": None, "commit": None, "ok": False, "error": None}
    if not p.exists():
        row["error"] = "not found"
        return row
    info = probe(path, timeout=PROBE_TIMEOUT_S) or {}
    row.update(version=info.get("version"), build=info.get("build"), commit=info.get("commit"))
    row["ok"] = bool(info.get("commit") or info.get("build"))
    if not row["ok"]:
        err = info.get("text") or "no version reported"
        row["error"] = err[len(path) + 2:] if err.startswith(path + ": ") else err
    return row


def remember(llama_bin: str, seen_file) -> list:
    """Adds the active build to the seen-builds file, drops missing ones; returns the list."""
    try:
        with open(seen_file, encoding="utf-8") as f:
            seen = [str(p) for p in json.load(f) if isinstance(p, str)]
    except (OSError, ValueError, TypeError):
        seen = []
    before = list(seen)
    if llama_bin:
        rp = _real(llama_bin)
        if os.path.exists(rp):
            seen = [rp] + [p for p in seen if p != rp]
    kept = [p for p in seen if os.path.isfile(p)][:MAX_BUILDS]
    if kept != before:
        with contextlib.suppress(OSError):
            tmp = f"{seen_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(kept, f, indent=1)
            os.replace(tmp, seen_file)
    return kept


def scan(cfg, probe: Callable, build_root: str, seen_file=None) -> dict:
    """Runs a fresh inventory and caches it; a caller that waited on a running scan gets its result."""
    seen_gen = _state["gen"]
    with _lock:
        if _state["gen"] != seen_gen and _state["last"] is not None:
            return _state["last"]
        llama_bin = (getattr(cfg, "LLAMA_BIN", "") or "").strip()
        seen = remember(llama_bin, seen_file) if seen_file else []
        notes = getattr(cfg, "LLAMA_BUILD_NOTES", {}) or {}
        if not isinstance(notes, dict):
            notes = {}
        extra = getattr(cfg, "LLAMA_BUILD_SEARCH_DIRS", []) or []
        if isinstance(extra, str):
            extra = [s for s in extra.split("|") if s.strip()]
        roots = [build_root, *[str(x).strip() for x in extra], *[str(k) for k in notes], *seen]
        paths, truncated = discover(llama_bin, roots, shutil.which(BIN_NAME))
        active = _real(llama_bin) if llama_bin else ""
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
            rows = list(ex.map(lambda p: describe(p, active, notes, probe), paths))
        _state["last"] = {"ok": True, "scanned_at": int(time.time()), "builds": rows,
                          "truncated": truncated}
        _state["gen"] += 1
        return _state["last"]


def cached() -> Optional[dict]:
    return _state["last"]
