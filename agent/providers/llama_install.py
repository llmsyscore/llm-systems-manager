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
# Current form `version: 0.4.0-dev (build 1, commit df03399)`; legacy form
# `version: 6150 (a0f7016d)`. The commit is the only stable identifier.
_COMMIT_RE = re.compile(r"\bcommit[\s:]+([0-9a-f]{7,40})\b")
_BUILD_NO_RE = re.compile(r"\bbuild[\s:]+b?(\d+)\b")
_VERSION_RE = re.compile(r"\bversion\s*:\s*(\S+)")
_BUILD_ID_RE = re.compile(r"\b(?:version|build)\s*:\s*b?(\d+)\s*\(([0-9a-f]{7,40})\)")
# Upstream's tool set, minus tests and examples.
_TOOLSET_CMAKE = ["-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF",
                  "-DLLAMA_BUILD_TOOLS=ON", "-DLLAMA_BUILD_SERVER=ON"]


def installed_build_id(bin_path, timeout: int = 30) -> dict:
    """{"build", "commit", "version", "text"} from `<bin> --version`. The binary's
    own directory goes on the loader path, since its libs live beside it."""
    out = {"build": None, "commit": None, "version": None, "text": ""}
    if not bin_path or not os.path.exists(bin_path):
        out["text"] = "not installed"
        return out
    env = dict(os.environ)
    libdir = os.path.dirname(os.path.abspath(str(bin_path)))
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        env[var] = libdir + (os.pathsep + env[var] if env.get(var) else "")
    try:
        r = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        out["text"] = "version check timed out"
        return out
    except (OSError, subprocess.SubprocessError) as e:
        out["text"] = f"could not run it: {e}"
        return out
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    commit = _COMMIT_RE.search(text)
    legacy = None if commit else _BUILD_ID_RE.search(text)
    if commit:
        out["commit"] = commit.group(1)
        bn = _BUILD_NO_RE.search(text)
        out["build"] = bn.group(1) if bn else None
        ver = _VERSION_RE.search(text)
        if ver and not ver.group(1).isdigit():
            out["version"] = ver.group(1)
    elif legacy:
        out["build"], out["commit"] = legacy.group(1), legacy.group(2)
    else:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        out["text"] = (first[:100] or f"no output, exit {r.returncode}")
    return out


def remote_commit(ref: str, timeout: int = 30) -> "str | None":
    """Upstream commit for ref without fetching the tree; None when unresolvable."""
    ref = (ref or "master").strip()
    if not _valid_git_ref(ref):
        return None
    try:
        r = subprocess.run(["git", "ls-remote", REPO_URL, ref], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    exact, peeled = None, None
    for line in (r.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        sha, name = sha.strip(), name.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        if name.endswith("^{}"):
            peeled = sha
        elif exact is None:
            exact = sha
    resolved = peeled or exact
    if resolved:
        return resolved
    # A ref that is already a commit sha resolves to itself.
    return ref.lower() if re.fullmatch(r"[0-9a-f]{7,40}", ref.lower()) else None


def source_up_to_date(opts: dict, installed_commit: "str | None",
                      recorded_backend: "str | None") -> dict:
    """Whether a source build would be a no-op: same upstream commit, same backend."""
    backend = ((opts or {}).get("backend") or "cpu").strip().lower()
    ref = ((opts or {}).get("git_ref") or "master").strip()
    out = {"up_to_date": False, "ref": ref, "backend": backend,
           "installed": installed_commit, "remote": None, "reason": ""}
    if not installed_commit or len(installed_commit) < 7:
        out["reason"] = "the installed binary did not report a usable build commit"
        return out
    if not recorded_backend:
        out["reason"] = "this install has no recorded build backend yet"
        return out
    if recorded_backend != backend:
        out["reason"] = f"backend changed ({recorded_backend} → {backend})"
        return out
    remote = remote_commit(ref)
    out["remote"] = remote
    if not remote:
        out["reason"] = f"could not resolve {ref} upstream"
        return out
    n = min(len(remote), len(installed_commit))
    if remote[:n] != installed_commit[:n]:
        out["reason"] = f"upstream {ref} moved to {remote[:8]}"
        return out
    out["up_to_date"] = True
    out["reason"] = f"installed build matches upstream {ref} at {remote[:8]}"
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
    flags = ["-DCMAKE_BUILD_TYPE=Release", *_TOOLSET_CMAKE, *_BACKEND_CMAKE[backend]]
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
    # llama-server must build; the rest of the tool set is tolerated so one
    # broken upstream tool cannot block the update.
    tools_cmd = " ".join(shlex.quote(a) for a in ["cmake", "--build", str(build), "-j", nj])
    tools_warn = "[warn] some llama.cpp tools failed to build; only the ones that built are installed"
    build_steps = [
        ["cmake", "--build", str(build), "--target", "llama-server", "-j", nj],
        ["sh", "-c", f"{tools_cmd} || echo {shlex.quote(tools_warn)}"],
    ]
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
        return _require_https(dl)
    return None


_NIGHTLY_POINTER = "nightly-tag.txt"


def _valid_release_tag(tag: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", tag)) and ".." not in tag


def _require_https(url: str) -> str:
    if not url.startswith("https://"):
        raise InstallError(f"release asset URL is not https: {url!r}")
    return url


def _http_get(url: str, what: str, accept: "str | None" = None,
              limit: "int | None" = None, timeout: float = 30) -> bytes:
    headers = {"User-Agent": "llm-systems-agent"}
    if accept:
        headers["Accept"] = accept
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read() if limit is None else r.read(limit)
    except Exception as e:
        raise InstallError(f"could not fetch {what}: {e}")


def _fetch_release(version: str) -> dict:
    url = f"{_RELEASES_API}/latest" if version in ("", "latest") else f"{_RELEASES_API}/tags/{version}"
    body = _http_get(url, f"llama.cpp releases ({version})", accept="application/vnd.github+json")
    try:
        return json.loads(body.decode())
    except Exception as e:
        raise InstallError(f"could not parse llama.cpp releases ({version}): {e}")


def _nightly_tag(assets: list) -> "str | None":
    """Build tag named by the release's nightly-tag.txt asset, else None."""
    asset = next((a for a in assets if (a.get("name") or "") == _NIGHTLY_POINTER), None)
    if asset is None:
        return None
    dl = asset.get("browser_download_url") or ""
    if not dl:
        return None
    text = _http_get(_require_https(dl), _NIGHTLY_POINTER, limit=256, timeout=10)
    tag = text.decode("utf-8-sig", errors="replace").split("\n", 1)[0].strip()
    if not _valid_release_tag(tag):
        raise InstallError(f"invalid build tag in {_NIGHTLY_POINTER}: {tag!r}")
    return tag


def _resolve_release_asset(version: str, backend: str = "cpu") -> "tuple[str, str]":
    """(download URL, release tag). `latest` follows a pointer-only release's
    nightly-tag.txt to the build that carries the binaries; a pinned tag never does."""
    latest = version in ("", "latest")
    if not latest and not _valid_release_tag(version):
        raise InstallError(f"invalid release version {version!r}")
    tokens = _asset_match_tokens()
    variant_tokens = _RELEASE_VARIANT.get(backend, ())
    data = _fetch_release(version)
    assets = data.get("assets", [])
    dl = _select_asset(assets, tokens, variant_tokens)
    resolved = data.get("tag_name") or version
    if dl is None and latest:
        tag = _nightly_tag(assets)
        if tag:
            resolved = tag
            dl = _select_asset(_fetch_release(tag).get("assets", []), tokens, variant_tokens)
    if dl is None:
        where = f"version {version!r}" + (f" (build {resolved})" if resolved != version else "")
        raise InstallError(f"no release asset matched {tokens} (backend {backend!r}) for {where}")
    return dl, resolved


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
    url, tag = _resolve_release_asset(version, backend)
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
        method="release_binary", label=f"release binary ({tag})", steps=steps, cwd=None, env={},
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
                # best-effort removal; ignore if it already vanished
                d.unlink()
            except OSError:
                pass
        try:
            shutil.move(str(s), str(d))
        except OSError as e:
            raise OSError(f"failed to move release file {s} -> {d}: {e}") from e
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
