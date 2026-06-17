"""llama.cpp install/upgrade methods. Stdlib-only leaf module: turns
(method, opts, cfg) into an InstallPlan the build worker executes.

No `from . import` and no heavy top-level imports — so it can be loaded
standalone in tests via importlib without triggering providers/__init__.py.
"""
from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LEGACY_SCRIPT = "/usr/local/llama-server/build-llama-cpp.sh"
REPO_URL = "https://github.com/ggml-org/llama.cpp.git"

_BACKEND_CMAKE = {
    "cpu": [],
    "cuda": ["-DGGML_CUDA=ON"],
    "vulkan": ["-DGGML_VULKAN=ON"],
    "metal": ["-DGGML_METAL=ON"],
    "rocm": ["-DGGML_HIP=ON"],
}


class InstallError(RuntimeError):
    """Unknown method or invalid opts; caught in _llama_build_worker."""


@dataclass(frozen=True)
class InstallPlan:
    method: str
    label: str
    steps: list[list[str]]
    cwd: "str | None"
    env: dict[str, str]
    resolve_binary: "Callable[[], str | None]"


def _agent_home(cfg) -> Path:
    """AGENT_USER's home (not euid's), mirroring _hf_cache_root in llama.py;
    falls back to the current home when AGENT_USER is unset/unknown."""
    user = (getattr(cfg, "AGENT_USER", "") or "").strip()
    if user:
        try:
            return Path(pwd.getpwnam(user).pw_dir)
        except KeyError:
            pass
    return Path(os.path.expanduser("~"))


def _build_root(cfg) -> Path:
    d = (getattr(cfg, "LLAMA_BUILD_DIR", "") or "").strip()
    if d:
        return Path(d).expanduser()
    return _agent_home(cfg) / ".local" / "share" / "llama.cpp"


def _h_custom_script(opts: dict, cfg) -> InstallPlan:
    script = (opts.get("script_path") or "").strip() or LEGACY_SCRIPT
    bin_path = getattr(cfg, "LLAMA_BIN", "") or ""
    return InstallPlan(
        method="custom_script", label="custom script",
        steps=[["sudo", "-n", script]], cwd=None, env={},
        resolve_binary=lambda: (bin_path or None),
    )


_GIT_REF_RE = re.compile(r"[A-Za-z0-9._/][A-Za-z0-9._/-]*\Z")


def _valid_git_ref(ref: str) -> bool:
    return bool(_GIT_REF_RE.match(ref)) and ".." not in ref \
        and not ref.startswith("/") and not ref.endswith("/")


def _h_source(opts: dict, cfg) -> InstallPlan:
    root = _build_root(cfg)
    src = root / "src"
    build = src / "build"
    ref = (opts.get("git_ref") or "master").strip()
    if not _valid_git_ref(ref):
        raise InstallError(f"invalid git_ref {ref!r}")
    backend = (opts.get("backend") or "cpu").strip().lower()
    if backend not in _BACKEND_CMAKE:
        raise InstallError(f"unknown backend {backend!r}; valid: {', '.join(sorted(_BACKEND_CMAKE))}")
    flags = _BACKEND_CMAKE[backend]
    if src.exists():
        fetch = [
            ["git", "-C", str(src), "fetch", "--depth", "1", "origin", "--", ref],
            ["git", "-C", str(src), "checkout", "-f", "FETCH_HEAD"],
        ]
    else:
        fetch = [["git", "clone", "--depth", "1", "--branch", ref, "--", REPO_URL, str(src)]]
    build_step = ["cmake", "--build", str(build), "--target", "llama-server", "-j"]
    jobs = opts.get("jobs")
    if jobs:
        build_step.append(str(int(jobs)))
    steps = [
        *fetch,
        ["cmake", "-S", str(src), "-B", str(build), *flags],
        build_step,
    ]
    return InstallPlan(
        method="source", label="source", steps=steps, cwd=None, env={},
        resolve_binary=lambda: str(build / "bin" / "llama-server"),
    )


METHODS: dict[str, Callable[[dict, Any], InstallPlan]] = {
    "custom_script": _h_custom_script,
    "source": _h_source,
}


def plan(method: str, opts: dict, cfg) -> InstallPlan:
    name = (method or "").strip() or "custom_script"
    handler = METHODS.get(name)
    if handler is None:
        raise InstallError(
            f"unknown LLAMA_BUILD_METHOD {name!r}; valid: {', '.join(sorted(METHODS))}"
        )
    return handler(opts or {}, cfg)
