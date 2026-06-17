"""llama.cpp install/upgrade methods. Stdlib-only leaf module: turns
(method, opts, cfg) into an InstallPlan the build worker executes.

No `from . import` and no heavy top-level imports — so it can be loaded
standalone in tests via importlib without triggering providers/__init__.py.
"""
from __future__ import annotations

import json
import os
import platform
import pwd
import re
import shlex
import shutil
import subprocess
import urllib.request
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


_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"


def _asset_match_tokens() -> list[str]:
    sysname = platform.system().lower()      # 'linux' | 'darwin'
    mach = platform.machine().lower()        # 'x86_64' | 'arm64' | 'aarch64'
    os_tok = "macos" if sysname == "darwin" else "ubuntu" if sysname == "linux" else sysname
    arch_tok = "arm64" if mach in ("arm64", "aarch64") else "x64"
    return [os_tok, arch_tok]


def _resolve_release_asset(version: str) -> str:
    if version not in ("", "latest") and not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        raise InstallError(f"invalid release version {version!r}")
    url = f"{_RELEASES_API}/latest" if version in ("", "latest") else f"{_RELEASES_API}/tags/{version}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "llm-systems-agent"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        raise InstallError(f"could not query llama.cpp releases ({version}): {e}")
    tokens = _asset_match_tokens()
    for asset in data.get("assets", []):
        name = (asset.get("name") or "").lower()
        dl = asset.get("browser_download_url") or ""
        if name.endswith((".zip", ".tar.gz", ".tgz")) and all(t in name for t in tokens):
            if not dl.startswith("https://"):
                raise InstallError(f"release asset URL is not https: {dl!r}")
            return dl
    raise InstallError(f"no release asset matched {tokens} for version {version!r}")


def _find_under(root: Path, name: str) -> "str | None":
    if not root.exists():
        return None
    matches = sorted(str(p) for p in root.rglob(name) if p.is_file())
    if not matches:
        return None
    for m in matches:
        if os.sep + "bin" + os.sep in m:
            return m
    return matches[0]


def _h_release_binary(opts: dict, cfg) -> InstallPlan:
    root = _build_root(cfg)
    dest = root / "release"
    tmp = root / "release.download"
    version = (opts.get("version") or "latest").strip()
    url = _resolve_release_asset(version)
    unpack = (f"unzip -o {shlex.quote(str(tmp))} -d {shlex.quote(str(dest))} || "
              f"tar -xf {shlex.quote(str(tmp))} -C {shlex.quote(str(dest))}")
    steps = [
        ["mkdir", "-p", str(dest)],
        ["curl", "-fsSL", "-o", str(tmp), url],
        ["sh", "-c", unpack],
    ]
    return InstallPlan(
        method="release_binary", label="release binary", steps=steps, cwd=None, env={},
        resolve_binary=lambda: _find_under(dest, "llama-server"),
    )


def _h_conda(opts: dict, cfg) -> InstallPlan:
    mgr = "conda" if shutil.which("conda") else ("mamba" if shutil.which("mamba") else None)
    if not mgr:
        raise InstallError("conda/mamba not found on PATH")
    return InstallPlan(
        method="conda", label="conda-forge",
        steps=[[mgr, "install", "-y", "-c", "conda-forge", "llama-cpp"]],
        cwd=None, env={},
        resolve_binary=lambda: shutil.which("llama-server"),
    )


def _h_homebrew(opts: dict, cfg) -> InstallPlan:
    brew = shutil.which("brew")
    if not brew:
        raise InstallError("brew not found on PATH")
    try:
        listed = subprocess.run([brew, "list", "--formula", "llama.cpp"],
                                capture_output=True, text=True, timeout=30)
        sub = "upgrade" if listed.returncode == 0 else "install"
    except Exception:
        sub = "install"

    def _resolve() -> "str | None":
        try:
            pref = subprocess.run([brew, "--prefix"], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except Exception:
            pref = ""
        return str(Path(pref) / "bin" / "llama-server") if pref else shutil.which("llama-server")

    return InstallPlan(
        method="homebrew", label="Homebrew", steps=[[brew, sub, "llama.cpp"]],
        cwd=None, env={}, resolve_binary=_resolve,
    )


METHODS: dict[str, Callable[[dict, Any], InstallPlan]] = {
    "custom_script": _h_custom_script,
    "source": _h_source,
    "release_binary": _h_release_binary,
    "conda": _h_conda,
    "homebrew": _h_homebrew,
}


def plan(method: str, opts: dict, cfg) -> InstallPlan:
    name = (method or "").strip() or "custom_script"
    handler = METHODS.get(name)
    if handler is None:
        raise InstallError(
            f"unknown LLAMA_BUILD_METHOD {name!r}; valid: {', '.join(sorted(METHODS))}"
        )
    return handler(opts or {}, cfg)


def detect_method(cfg) -> "str | None":
    bin_path = getattr(cfg, "LLAMA_BIN", "") or ""
    p = bin_path.lower()
    if "/homebrew/" in p or p.startswith("/opt/homebrew") or "/cellar/" in p:
        return "homebrew"
    conda_prefix = (os.environ.get("CONDA_PREFIX") or "").lower()
    conda_env = len(conda_prefix) > 5 and p.startswith(conda_prefix)
    if "/envs/" in p or "/miniconda" in p or "/anaconda" in p or conda_env:
        return "conda"
    root = _build_root(cfg)
    if bin_path and str(root / "release").lower() in p:
        return "release_binary"
    if (root / "src" / "CMakeLists.txt").exists():
        return "source"
    if os.path.exists(LEGACY_SCRIPT):
        return "custom_script"
    return None
