"""In-place llama.cpp upgrade. Stdlib-only leaf module (no `from . import`,
no heavy top-level imports) so it loads standalone in tests via importlib.

Swaps freshly-built binaries + their shared libs onto the live install at
dirname(LLAMA_BIN), preserving config.ini and other user files. Agent-writable
targets only: aborts (no changes) when the destination is not owned/writable by
the agent user. Backups are copied (not moved) and the new files are committed
with atomic os.replace, which is safe on a running ELF.
"""
from __future__ import annotations

import datetime
import filecmp
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Non-llama-* executables upstream ships beside the tools.
EXTRA_TOOLS = frozenset({"rpc-server"})

# Tools llama.cpp has shipped beside llama-server. Kept as documentation of the
# expected set — any upstream llama-* executable is treated as a tool.
KNOWN_TOOLS = frozenset({
    "llama-server", "llama-cli", "llama-run", "llama-bench", "llama-batched-bench",
    "llama-quantize", "llama-perplexity", "llama-embedding", "llama-tokenize",
    "llama-gguf", "llama-gguf-split", "llama-imatrix", "llama-export-lora",
    "llama-lookup", "llama-lookup-create", "llama-lookup-merge", "llama-lookup-stats",
    "llama-speculative", "llama-speculative-simple", "llama-parallel", "llama-passkey",
    "llama-retrieval", "llama-save-load-state", "llama-simple", "llama-simple-chat",
    "llama-mtmd-cli", "llama-llava-cli", "llama-minicpmv-cli", "llama-qwen2vl-cli",
    "llama-gemma3-cli", "llama-tts", "llama-gen-docs", "llama-eval-callback",
    "llama-batched", "llama-gritlm", "llama-infill",
})

# ggml/llama/mtmd shared objects: lib<name>.(so|dylib) with optional version.
_LIB_RE = re.compile(r"^lib(ggml|llama|mtmd).*\.(so|dylib)(\.[0-9]+)*$")
# Any llama-* executable beside the server, known to the allowlist or not.
_TOOL_RE = re.compile(r"^llama-[a-z0-9]+(?:-[a-z0-9]+)*$")
# Tools the product depends on; probed for runnability after every swap.
DEPENDENT_TOOLS = ("llama-perplexity",)

_BACKUP_PREFIX = ".upgrade.bak."
_STAGE_PREFIX = ".upgrade.stage."
STALE_MARKER = ".upgrade.stale.json"
BUILD_MARKER = ".llama-build.json"
_PROBE_TIMEOUT_S = 10
_PROBE_BUDGET_S = 60


@dataclass
class UpgradeResult:
    ok: bool
    message: str
    target: "str | None" = None
    swapped: "list[str]" = field(default_factory=list)
    backup_dir: "str | None" = None
    skipped: bool = False
    removed: "list[str]" = field(default_factory=list)


def should_upgrade_in_place(method: str, opts: "dict | None") -> bool:
    """In-place swap for source/release_binary; defaults on, opt out with
    install_in_place: false."""
    if method not in ("source", "release_binary"):
        return False
    return bool((opts or {}).get("install_in_place", True))


def is_tool(name: str) -> bool:
    """Any upstream llama-* tool, named in the allowlist or not, plus rpc-server."""
    return name in EXTRA_TOOLS or bool(_TOOL_RE.match(name))


def is_artifact(name: str, bin_name: str) -> bool:
    return name == bin_name or is_tool(name) or bool(_LIB_RE.match(name))


def select_artifacts(src_dir: Path, bin_name: str) -> list:
    """Allowlisted files (binary + tools + ggml/llama libs) present in src_dir."""
    out = []
    for entry in sorted(os.listdir(src_dir)):
        p = src_dir / entry
        if (p.is_file() or p.is_symlink()) and is_artifact(entry, bin_name):
            out.append(entry)
    return out


def _owner_name(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return str(uid)


def _probe_writable(d: Path) -> bool:
    """Confirm we can os.replace inside d (not just dir-write) — covers
    sticky-bit dirs where a dir-write check alone is insufficient."""
    try:
        fd, tmp = tempfile.mkstemp(prefix=_STAGE_PREFIX, dir=str(d))
        os.close(fd)
    except OSError:
        return False
    try:
        mv = tmp + ".mv"
        os.replace(tmp, mv)
        os.remove(mv)
        return True
    except OSError:
        for p in (tmp, tmp + ".mv"):
            try:
                os.remove(p)
            except OSError:
                pass  # best-effort: probe temp file may already be gone
        return False


def _same_artifact(staged: Path, live: Path) -> bool:
    """True if the staged artifact already matches the live one — symlinks by
    target name, regular files by mode + content — so the swap can skip it."""
    s_link, l_link = staged.is_symlink(), live.is_symlink()
    if s_link or l_link:
        if s_link != l_link:
            return False
        try:
            return os.path.basename(os.readlink(staged)) == os.path.basename(os.readlink(live))
        except OSError:
            return False
    if not live.exists():
        return False
    try:
        if (os.stat(staged).st_mode & 0o777) != (os.stat(live).st_mode & 0o777):
            return False
        return filecmp.cmp(str(staged), str(live), shallow=False)
    except OSError:
        return False


def _copy_one(src: Path, dst: Path) -> None:
    """copy2 preserving symlinks; fsync regular files so a crash can't leave
    a truncated artifact behind."""
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
        return
    shutil.copy2(src, dst, follow_symlinks=False)
    try:
        fd = os.open(dst, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError:
        pass  # fsync is best-effort; a flush failure doesn't void the copy


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError:
        pass  # directory fsync is best-effort / not portable across filesystems


def _smoke(binp: Path, libdir: Path, timeout: int = 30) -> "tuple[bool, str]":
    env = dict(os.environ)
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        env[var] = str(libdir) + (os.pathsep + env[var] if env.get(var) else "")
    try:
        r = subprocess.run([str(binp), "--version"], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{e}"
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()


def _probe_rc(binp: Path, libdir: Path, timeout: int = _PROBE_TIMEOUT_S) -> tuple:
    """(returncode, timed_out) for `binp --version` with libdir on the loader path;
    returncode is None when the binary could not be executed at all."""
    env = dict(os.environ)
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        env[var] = str(libdir) + (os.pathsep + env[var] if env.get(var) else "")
    try:
        r = subprocess.run([str(binp), "--version"], capture_output=True, text=True,
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, True
    except (OSError, subprocess.SubprocessError):
        return None, False
    return r.returncode, False


def tool_broken(rc: "int | None", timed_out: bool) -> "str | None":
    """Why a tool cannot run, or None when it looks fine. A tool that merely
    rejects --version is fine; only exec, loader and signal failures count."""
    if timed_out:
        return None
    if rc is None:
        return "could not be executed"
    if rc < 0:
        return f"crashed on startup (signal {-rc})"
    if rc == 127:
        return "could not load its shared libraries"
    return None


def sibling_tools(dest: Path, exclude) -> "list[str]":
    """llama-* executables in dest not listed in exclude."""
    out = []
    try:
        entries = sorted(os.listdir(dest))
    except OSError:
        return out
    for name in entries:
        if name in exclude or not is_tool(name):
            continue
        p = dest / name
        if (p.is_file() or p.is_symlink()) and os.access(p, os.X_OK):
            out.append(name)
    return out


def read_build_marker(dest) -> dict:
    """{"commit", "backend", "method", "ts"} recorded by the last swap, else {}."""
    try:
        with open(Path(dest) / BUILD_MARKER, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_build_marker(dest, *, commit: str, backend: str, method: str) -> None:
    """Record what the live install was built from, so a later run can tell
    whether a rebuild would change anything."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mp = Path(dest) / BUILD_MARKER
    try:
        tmp = mp.with_name(mp.name + ".tmp")
        tmp.write_text(json.dumps({"commit": commit, "backend": backend,
                                   "method": method, "ts": ts}, indent=1), encoding="utf-8")
        os.replace(tmp, mp)
    except OSError:
        pass  # best-effort: a marker write never fails the swap


def read_stale_marker(dest) -> dict:
    """{"ts", "removed": {tool: reason}} recorded by the last swap, else {}."""
    try:
        with open(Path(dest) / STALE_MARKER, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("removed"), dict) else {}
    except (OSError, ValueError):
        return {}


def _write_stale_marker(dest: Path, removed: dict, restored) -> None:
    """Merge newly removed tools into the marker, drop restored ones, delete when empty."""
    cur = dict(read_stale_marker(dest).get("removed") or {})
    for name in restored:
        cur.pop(name, None)
    cur.update(removed)
    mp = dest / STALE_MARKER
    try:
        if not cur:
            if mp.exists():
                mp.unlink()
            return
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp = mp.with_name(mp.name + ".tmp")
        tmp.write_text(json.dumps({"ts": ts, "removed": cur}, indent=1), encoding="utf-8")
        os.replace(tmp, mp)
    except OSError:
        pass  # best-effort: a marker write never fails the swap


def _reconcile_siblings(dest: Path, names, emit, ensure_backup) -> "list[str]":
    """Remove tools the new build did not provide that no longer run — the ones
    left behind against replaced shared libraries. A tool that still runs is kept
    even when this build does not produce it. Each removal is copied into the
    backup directory first; one that cannot be copied stays in place."""
    removed = {}
    budget = time.monotonic() + _PROBE_BUDGET_S
    for name in sibling_tools(dest, set(names)):
        if time.monotonic() > budget:
            emit("[warn] stopped checking leftover tools after "
                 f"{_PROBE_BUDGET_S}s; the rest were left in place")
            break
        why = tool_broken(*_probe_rc(dest / name, dest))
        if why:
            removed[name] = why
    saved_to = None
    for name, why in list(removed.items()):
        backup = ensure_backup()
        if backup is None:
            emit(f"[warn] {name} {why} but no backup directory could be created; left in place")
            removed.pop(name)
            continue
        try:
            _copy_one(dest / name, backup / name)
        except OSError as e:
            emit(f"[warn] {name} {why} but could not be backed up ({e}); left in place")
            removed.pop(name)
            continue
        try:
            os.remove(dest / name)
            saved_to = backup
        except OSError as e:
            emit(f"[warn] could not remove stale {name}: {e}")
            removed.pop(name)
    if removed:
        emit(f"[warn] removed {len(removed)} tool(s) that no longer run: "
             + ", ".join(f"{n} ({w})" for n, w in removed.items()))
        emit(f"[warn] copies of the removed tool(s) are in {saved_to}")
        emit("[warn] a source update builds the full upstream tool set; a tool removed here "
             "is one this build does not produce, and it could not run against the new libraries")
    _write_stale_marker(dest, removed, names)
    for name in DEPENDENT_TOOLS:
        if name in names and (dest / name).exists():
            why = tool_broken(*_probe_rc(dest / name, dest))
            if why:
                emit(f"[warn] {name} {why} after the swap; the new build of this tool "
                     f"is not runnable here")
    return sorted(removed)


def _prune_backups(dest: Path, retain: int, emit) -> None:
    retain = max(1, int(retain))
    backups = sorted((p for p in dest.iterdir()
                      if p.is_dir() and p.name.startswith(_BACKUP_PREFIX)),
                     key=lambda p: p.name)
    for old in backups[:-retain]:
        real = Path(os.path.realpath(old))
        if real.parent == Path(os.path.realpath(dest)) and real.name.startswith(_BACKUP_PREFIX):
            shutil.rmtree(real, ignore_errors=True)
            emit(f"[info] pruned old backup {old.name}")


def upgrade_in_place(resolved_bin: str, dest_bin: str, *, build_root=None,
                     unit: str = "the llama unit", agent_user: str = "",
                     retain: int = 2, emit=lambda _s: None,
                     smoke: bool = True) -> UpgradeResult:
    """Swap the freshly-built artifacts beside resolved_bin onto the live install
    at dirname(dest_bin). Aborts before committing on any pre-commit failure;
    rolls back from the backup on a mid-swap failure."""
    src_file = Path(resolved_bin)
    src_dir = src_file.parent
    dest_file = Path(dest_bin)
    dest_dir = dest_file.parent
    bin_name = dest_file.name

    if not src_file.exists():
        msg = f"[error] freshly built binary not found at {resolved_bin}"
        emit(msg)
        return UpgradeResult(False, msg)
    if not dest_dir.is_dir():
        msg = f"[error] {dest_dir} does not exist — not an in-place upgrade target"
        emit(msg)
        return UpgradeResult(False, msg)

    dest_real = Path(os.path.realpath(dest_dir))
    if Path(os.path.realpath(src_dir)) == dest_real:
        msg = "[info] build output is already at the live location; nothing to swap"
        emit(msg)
        return UpgradeResult(True, msg, target=dest_bin, skipped=True)

    st = os.stat(dest_real)
    if st.st_uid != os.geteuid() or not _probe_writable(dest_real):
        who = (" (" + agent_user + ")") if agent_user else ""
        msg = (f"[error] in-place upgrade needs {dest_real} writable by the agent user{who}; "
               f"it is owned by {_owner_name(st.st_uid)}. Skipping swap — the new build remains "
               f"at {src_dir}. chown the directory to the agent user or use a privileged install, "
               f"then retry.")
        emit(msg)
        return UpgradeResult(False, msg)

    names = select_artifacts(src_dir, bin_name)
    if bin_name not in names:
        msg = f"[error] freshly built {bin_name} not found alongside {resolved_bin}"
        emit(msg)
        return UpgradeResult(False, msg)

    staging = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=str(dest_real)))
    backup = None
    changed = []

    def _ensure_backup():
        """The run's backup dir, created on demand for a skipped-swap removal."""
        nonlocal backup
        if backup is None:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            b = dest_real / f"{_BACKUP_PREFIX}{ts}"
            try:
                b.mkdir(exist_ok=True)
            except OSError:
                return None
            backup = b
        return backup

    try:
        try:
            for name in names:
                _copy_one(src_dir / name, staging / name)
            _fsync_dir(staging)
        except OSError as e:
            msg = f"[error] failed to stage {name!r}: {e}; aborting swap (no changes)"
            emit(msg)
            return UpgradeResult(False, msg)

        if smoke:
            ok, out = _smoke(staging / bin_name, staging)
            if not ok:
                msg = f"[error] staged {bin_name} --version failed; aborting swap (no changes). {out}"
                emit(msg)
                return UpgradeResult(False, msg)

        changed = [n for n in names if not _same_artifact(staging / n, dest_real / n)]
        if changed:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = dest_real / f"{_BACKUP_PREFIX}{ts}"
            try:
                backup.mkdir()
                for name in changed:
                    live = dest_real / name
                    if live.exists() or live.is_symlink():
                        _copy_one(live, backup / name)
                _fsync_dir(backup)
            except OSError as e:
                msg = f"[error] failed to back up current install: {e}; aborting swap (no changes)"
                emit(msg)
                return UpgradeResult(False, msg)

            committed = []
            try:
                for name in changed:
                    os.replace(str(staging / name), str(dest_real / name))
                    committed.append(name)
            except OSError as e:
                rolled, failed = [], []
                for name in committed:
                    try:
                        os.replace(str(backup / name), str(dest_real / name))
                        rolled.append(name)
                    except OSError as re_err:
                        failed.append(f"{name}: {re_err}")
                if failed:
                    msg = (f"[error] swap failed AND rollback incomplete — live install may be "
                           f"inconsistent; restore manually from {backup}. not restored: "
                           f"{'; '.join(failed)}. cause: {e}")
                else:
                    msg = (f"[error] swap failed; rolled back {len(rolled)} file(s) from {backup}. "
                           f"cause: {e}")
                emit(msg)
                return UpgradeResult(False, msg, backup_dir=str(backup))
            _fsync_dir(dest_real)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if build_root:
        tarball = Path(build_root) / "release.download"
        if tarball.exists():
            try:
                tarball.unlink()
            except OSError:
                pass  # download cleanup is optional; never fail a done swap

    try:
        removed = _reconcile_siblings(dest_real, names, emit, _ensure_backup)
    except Exception as e:
        emit(f"[warn] stale-tool check failed: {e}")
        removed = []

    if not changed:
        emit(f"[ok] already up to date — 0 file(s) changed at {dest_real}")
        return UpgradeResult(True, "up to date", target=dest_bin, swapped=[], skipped=True,
                             removed=removed, backup_dir=str(backup) if backup else None)

    try:
        _prune_backups(dest_real, retain, emit)
    except Exception:
        pass  # pruning is best-effort; a completed swap must not fail here

    emit(f"[ok] upgraded {len(changed)} file(s) at {dest_real}: {', '.join(changed)}")
    emit(f"[ok] previous binaries backed up to {backup}")
    emit(f"restart to run the new build: sudo -n /usr/bin/systemctl restart {unit}")
    return UpgradeResult(True, "upgraded", target=dest_bin, swapped=changed, backup_dir=str(backup),
                         removed=removed)
