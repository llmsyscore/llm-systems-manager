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

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Remove CSI escape sequences (color, clear, cursor-visibility) from text."""
    return _ANSI_RE.sub("", text)

_BACKEND_CMAKE = {
    "cpu": [],
    "cuda": ["-DGGML_CUDA=ON"],
    "vulkan": ["-DGGML_VULKAN=ON"],
    "metal": ["-DGGML_METAL=ON"],
    "rocm": ["-DGGML_HIP=ON"],
}

_ACCEL_TOKENS = ("cuda", "vulkan", "rocm", "hip", "sycl", "openvino", "musa", "cann", "kompute")

_RELEASE_VARIANT = {
    "cpu": (),
    "metal": (),
    "vulkan": ("vulkan",),
    "rocm": ("rocm", "hip"),
    "cuda": ("cuda",),
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
    tools: tuple = ()


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
        tools=("sudo",),
        resolve_binary=lambda: (bin_path or None),
    )


_GIT_REF_RE = re.compile(r"[A-Za-z0-9._/][A-Za-z0-9._/-]*\Z")
_TOOL_RE = re.compile(r"^llama-[a-z0-9]+(?:-[a-z0-9]+)*$")


def _existing_tool_targets(cfg) -> "list[str]":
    """llama-* tool binaries installed beside LLAMA_BIN, excluding llama-server.
    Used as extra cmake targets so a source upgrade rebuilds every tool the host
    already has instead of only the server."""
    bin_path = (getattr(cfg, "LLAMA_BIN", "") or "").strip()
    if not bin_path:
        return []
    d = Path(bin_path).parent
    try:
        entries = os.listdir(d)
    except OSError:
        return []
    out = []
    for name in sorted(entries):
        if name == "llama-server" or not _TOOL_RE.match(name):
            continue
        p = d / name
        if (p.is_file() or p.is_symlink()) and os.access(p, os.X_OK):
            out.append(name)
    return out


def cleanup_after_inplace(cfg, method: str, emit: "Callable[[str], None]" = lambda _s: None) -> None:
    """Remove disposable build artifacts after a successful in-place swap. Source:
    drop the build output, keep the git checkout for a fast next fetch+build.
    Release: drop the extracted dir and the downloaded archive. Never touches a
    directory that contains LLAMA_BIN."""
    try:
        root = _build_root(cfg)
    except Exception as e:
        emit(f"[warn] cleanup skipped: could not resolve build root: {e}")
        return
    bin_path = (getattr(cfg, "LLAMA_BIN", "") or "").strip()
    live = Path(os.path.realpath(bin_path)).parent if bin_path else None
    if method == "source":
        targets = [root / "src" / "build"]
    elif method == "release_binary":
        targets = [root / "release", root / "release.download"]
    else:
        return
    for t in targets:
        rt = Path(os.path.realpath(t))
        if live and (live == rt or rt in live.parents):
            emit(f"[warn] cleanup skipped for {t}: it contains LLAMA_BIN")
            continue
        try:
            if t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
                emit(f"[info] cleaned up {t}")
            elif t.exists():
                t.unlink()
                emit(f"[info] cleaned up {t}")
        except OSError as e:
            emit(f"[warn] could not clean up {t}: {e}")


def _valid_git_ref(ref: str) -> bool:
    return bool(_GIT_REF_RE.match(ref)) and ".." not in ref \
        and not ref.startswith("/") and not ref.endswith("/")


def _hip_build_env() -> dict:
    """Resolve HIPCXX/HIP_PATH for a rocm source build, mirroring upstream's
    HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)"."""
    hipconfig = shutil.which("hipconfig")
    if not hipconfig:
        raise InstallError(
            "rocm backend selected but 'hipconfig' was not found on PATH; "
            "install ROCm/HIP (or choose a different backend) and retry"
        )
    try:
        lib = subprocess.run([hipconfig, "-l"], capture_output=True, text=True,
                             timeout=30, check=True).stdout.strip()
        root = subprocess.run([hipconfig, "-R"], capture_output=True, text=True,
                              timeout=30, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise InstallError(f"could not query hipconfig for rocm build env: {e}")
    if not lib or not root:
        raise InstallError(
            f"hipconfig returned empty HIP paths (-l={lib!r}, -R={root!r}); "
            "the ROCm/HIP install looks incomplete"
        )
    return {"HIPCXX": f"{lib}/clang", "HIP_PATH": root}


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
    flags = ["-DCMAKE_BUILD_TYPE=Release", *_BACKEND_CMAKE[backend]]
    env = _hip_build_env() if backend == "rocm" else {}
    if src.exists():
        fetch = [
            ["git", "-C", str(src), "fetch", "--depth", "1", "origin", "--", ref],
            ["git", "-C", str(src), "checkout", "-f", "FETCH_HEAD"],
        ]
    else:
        fetch = [["git", "clone", "--depth", "1", "--branch", ref, "--", REPO_URL, str(src)]]
    jobs = opts.get("jobs")
    if jobs in (None, ""):
        njobs = os.cpu_count() or 1
    else:
        try:
            njobs = int(jobs)
        except (TypeError, ValueError):
            raise InstallError(f"invalid jobs {jobs!r}; must be a positive integer")
        if njobs < 1:
            raise InstallError(f"invalid jobs {jobs!r}; must be a positive integer")
    nj = str(njobs)
    build_steps = [["cmake", "--build", str(build), "--target", "llama-server", "-j", nj]]
    extras = _existing_tool_targets(cfg)
    if extras:
        core = ["cmake", "--build", str(build), "--target", *extras, "-j", nj]
        warn = "[warn] some existing llama-* tools failed to rebuild; prior copies left in place"
        joined = " ".join(shlex.quote(a) for a in core)
        build_steps.append(["sh", "-c", f"{joined} || echo {shlex.quote(warn)}"])
    steps = [
        *fetch,
        ["cmake", "-S", str(src), "-B", str(build), *flags],
        *build_steps,
    ]
    return InstallPlan(
        method="source", label="source", steps=steps, cwd=None, env=env,
        tools=("git", "cmake"),
        resolve_binary=lambda: str(build / "bin" / "llama-server"),
    )


_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"


def _asset_match_tokens() -> list[str]:
    sysname = platform.system().lower()      # 'linux' | 'darwin'
    mach = platform.machine().lower()        # 'x86_64' | 'arm64' | 'aarch64'
    os_tok = "macos" if sysname == "darwin" else "ubuntu" if sysname == "linux" else sysname
    arch_tok = "arm64" if mach in ("arm64", "aarch64") else "x64"
    return [os_tok, arch_tok]


def _select_asset(assets: list, tokens: list, variant_tokens: tuple) -> "str | None":
    for asset in assets:
        name = (asset.get("name") or "").lower()
        dl = asset.get("browser_download_url") or ""
        if not name.endswith((".zip", ".tar.gz", ".tgz")):
            continue
        if not all(t in name for t in tokens):
            continue
        if variant_tokens:
            if not any(v in name for v in variant_tokens):
                continue
        elif any(a in name for a in _ACCEL_TOKENS):
            continue
        if not dl.startswith("https://"):
            raise InstallError(f"release asset URL is not https: {dl!r}")
        return dl
    return None


def _resolve_release_asset(version: str, backend: str = "cpu") -> str:
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
    variant_tokens = _RELEASE_VARIANT.get(backend, ())
    dl = _select_asset(data.get("assets", []), tokens, variant_tokens)
    if dl is None:
        raise InstallError(
            f"no release asset matched {tokens} (backend {backend!r}) for version {version!r}"
        )
    return dl


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
    backend = (opts.get("backend") or "cpu").strip().lower()
    if backend not in _BACKEND_CMAKE:
        raise InstallError(f"unknown backend {backend!r}; valid: {', '.join(sorted(_BACKEND_CMAKE))}")
    url = _resolve_release_asset(version, backend)
    if url.lower().endswith(".zip"):
        unpack, unpack_tool = ["unzip", "-o", str(tmp), "-d", str(dest)], "unzip"
    else:
        unpack, unpack_tool = ["tar", "-xf", str(tmp), "-C", str(dest)], "tar"
    steps = [
        ["mkdir", "-p", str(dest)],
        ["curl", "-fsSL", "-o", str(tmp), url],
        unpack,
    ]
    return InstallPlan(
        method="release_binary", label="release binary", steps=steps, cwd=None, env={},
        tools=("curl", unpack_tool),
        resolve_binary=lambda: _find_under(dest, "llama-server"),
    )


def flatten_release(resolved: str, cfg, emit: "Callable[[str], None]" = lambda _s: None) -> str:
    """Move the extracted release artifacts up to the build root so LLAMA_BIN is a
    stable, build-number-free path. Returns the flat llama-server path. Setup-time
    only — the in-place upgrade path swaps from the nested dir and must not flatten."""
    root = _build_root(cfg)
    src_dir = Path(resolved).parent
    root.mkdir(parents=True, exist_ok=True)
    if Path(os.path.realpath(src_dir)) == Path(os.path.realpath(root)):
        return resolved
    moved = 0
    for entry in sorted(os.listdir(src_dir)):
        s = src_dir / entry
        if not (s.is_file() or s.is_symlink()):
            continue
        d = root / entry
        if d.exists() or d.is_symlink():
            try:
                d.unlink()
            except OSError:
                pass
        shutil.move(str(s), str(d))
        moved += 1
    if moved:
        emit(f"[info] flattened {moved} release file(s) to {root}")
    flat = root / Path(resolved).name
    return str(flat)


def _h_conda(opts: dict, cfg) -> InstallPlan:
    mgr = "conda" if shutil.which("conda") else ("mamba" if shutil.which("mamba") else None)
    if not mgr:
        raise InstallError("conda/mamba not found on PATH")
    return InstallPlan(
        method="conda", label="conda-forge",
        steps=[[mgr, "install", "-y", "-c", "conda-forge", "llama-cpp"]],
        cwd=None, env={}, tools=(mgr,),
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
        cwd=None, env={}, tools=(brew,), resolve_binary=_resolve,
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


def missing_build_tools(tools, env: dict) -> list:
    """Required `tools` not found on env['PATH']. Tuple/list entries are
    any-of (satisfied if any candidate resolves)."""
    path = env.get("PATH")

    def have(exe: str) -> bool:
        if os.path.isabs(exe):
            return os.path.exists(exe) and os.access(exe, os.X_OK)
        return shutil.which(exe, path=path) is not None

    missing: list = []
    for entry in tools or ():
        if isinstance(entry, (tuple, list)):
            if not any(have(e) for e in entry):
                missing.append(" or ".join(entry))
        elif not have(entry):
            missing.append(entry)
    return missing


def run_install(iplan: InstallPlan, *, env: "dict | None" = None,
                emit: Callable[[str], None] = lambda _s: None,
                popen: Callable = subprocess.Popen) -> "tuple[int, str | None]":
    """Preflight the plan's tools, run its steps streaming each output line via
    emit(line), then resolve the installed binary. Returns (rc, resolved|None).
    Shared by the agent build worker and the setup-time installer."""
    run_env = dict(os.environ if env is None else env)
    run_env.update(iplan.env or {})
    run_env["PYTHONUNBUFFERED"] = "1"
    run_env["FORCE_COLOR"] = "0"
    missing = missing_build_tools(iplan.tools, run_env)
    if missing:
        emit(f"[error] required command(s) not found: {', '.join(missing)} — "
             f"install them or choose a build method that doesn't need them, then retry")
        return 127, None
    rc = 0
    for step in iplan.steps:
        proc = popen(step, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, text=True, bufsize=1, close_fds=True,
                     cwd=iplan.cwd, env=run_env)
        if proc.stdout is not None:
            for raw in iter(proc.stdout.readline, ""):
                line = _ANSI_RE.sub("", raw).rstrip()
                if line:
                    emit(line)
        proc.wait()
        rc = proc.returncode if proc.returncode is not None else 1
        if rc != 0:
            break
    resolved = None
    if rc == 0:
        try:
            resolved = iplan.resolve_binary()
        except Exception as e:
            emit(f"[warn] build succeeded but binary location could not be resolved: {e}")
            resolved = None
    return rc, resolved


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
    if bin_path and os.path.normpath(os.path.dirname(p)) == os.path.normpath(str(root).lower()):
        return "release_binary"
    if (root / "src" / "CMakeLists.txt").exists():
        return "source"
    if os.path.exists(LEGACY_SCRIPT):
        return "custom_script"
    return None
