"""Read-only inventory of llama.cpp builds installed on this host (#1389).
Stdlib-only leaf module."""
from __future__ import annotations

import contextlib
import json
import os
import shlex
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


def _build_name(bin_path: str) -> str:
    """Folder name of a build: the binary's directory without trailing bin and build segments."""
    parts = [x for x in os.path.dirname(_real(bin_path)).split(os.sep) if x]
    while len(parts) > 1 and parts[-1] in ("bin", "build"):
        parts.pop()
    return parts[-1] if parts else ""


def _note_for(path: str, notes: dict) -> str:
    """Note whose key is the binary or its folder (paths compared after realpath)."""
    folder = os.path.dirname(path)
    for k, v in (notes or {}).items():
        rk = _real(k)
        if rk in (path, folder):
            return str(v or "")[:200]
    return ""


def describe(path: str, active_real: str, notes: dict, probe: Callable,
             plain_check: Optional[Callable] = None) -> dict:
    p = Path(path)
    row = {"path": path, "dir": str(p.parent), "name": _build_name(path), "active": path == active_real,
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
    elif plain_check is not None:
        plain_ok, why = plain_check(path)
        if not plain_ok:
            row["ok"] = False
            row["error"] = f"runs only with its own folder on the library path: {why}"
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


def scan(cfg, probe: Callable, build_root: str, seen_file=None,
         plain_check: Optional[Callable] = None) -> dict:
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
            rows = list(ex.map(lambda p: describe(p, active, notes, probe, plain_check), paths))
        _state["last"] = {"ok": True, "scanned_at": int(time.time()), "builds": rows,
                          "truncated": truncated}
        _state["gen"] += 1
        return _state["last"]


def cached() -> Optional[dict]:
    return _state["last"]


def unit_run_context(unit_text: str) -> "tuple[Optional[str], dict]":
    """(WorkingDirectory, Environment= variables) from a systemd unit's [Service] lines."""
    cwd: Optional[str] = None
    env: dict = {}
    for line in unit_text.splitlines():
        s = line.strip()
        if s.startswith("WorkingDirectory="):
            v = s[len("WorkingDirectory="):].strip().lstrip("-")
            cwd = v if v.startswith("/") else None
        elif s.startswith("Environment="):
            with contextlib.suppress(ValueError):
                for tok in shlex.split(s[len("Environment="):]):
                    k, sep, v = tok.partition("=")
                    if sep and k:
                        env[k] = v
    return cwd, env


def forget_cache() -> None:
    _state["last"] = None


def listed_build(path: str) -> Optional[dict]:
    """The row for path in the last scan, or None when it was not listed."""
    rp = _real(path)
    for row in (_state["last"] or {}).get("builds") or []:
        if row.get("path") == rp:
            return row
    return None


def set_yaml_key(path, key: str, value: str) -> str:
    """Sets a top-level `KEY: "value"` line (replacing the first live one, else appending); returns the backup path."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    line = f"{key}: {json.dumps(value)}"
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}:"):
            lines[i] = line
            break
    else:
        lines.append(line)
    backup = f"{p}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
    shutil.copy2(p, backup)
    st = p.stat()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, st.st_mode & 0o777)
    with contextlib.suppress(OSError):
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, p)
    return backup


def switch(target: str, exec_tokens: list, *, preflight: Callable, apply_exec: Callable,
           wait_ready: Callable, persist: Callable, log_tail: Callable) -> dict:
    """Preflight → point ExecStart at target and restart → wait ready → persist; restores exec_tokens on failure."""
    ok, why = preflight(target)
    if not ok:
        return {"ok": False, "stage": "check", "error": why, "changed": False}
    res = apply_exec([target, *exec_tokens[1:]])
    if res.get("ok"):
        ready, why = wait_ready()
        if ready:
            out = {"ok": True, "active": target, "previous": exec_tokens[0]}
            try:
                out["config_backup"] = persist(target)
            except Exception as e:  # noqa: BLE001 — returned as a warning
                out["warning"] = f"switched, but saving LLAMA_BIN failed: {e}"
            return out
    else:
        why = res.get("error") or "could not update the service"
    tail = log_tail()
    back = apply_exec(list(exec_tokens))
    restored, back_why = wait_ready()
    if not restored and not back.get("ok"):
        back_why = back.get("error") or back_why
    return {"ok": False, "stage": "start", "error": why, "changed": False, "rolled_back": True,
            "restored": restored, "restore_error": None if restored else back_why,
            "previous": exec_tokens[0], "log": tail}
