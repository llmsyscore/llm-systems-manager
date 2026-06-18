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
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Sibling tools llama.cpp builds next to llama-server; replaced when present.
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

_BACKUP_PREFIX = ".upgrade.bak."
_STAGE_PREFIX = ".upgrade.stage."


@dataclass
class UpgradeResult:
    ok: bool
    message: str
    target: "str | None" = None
    swapped: "list[str]" = field(default_factory=list)
    backup_dir: "str | None" = None
    skipped: bool = False


def should_upgrade_in_place(method: str, opts: "dict | None") -> bool:
    """In-place swap for source/release_binary; defaults on, opt out with
    install_in_place: false."""
    if method not in ("source", "release_binary"):
        return False
    return bool((opts or {}).get("install_in_place", True))


def is_artifact(name: str, bin_name: str) -> bool:
    return name == bin_name or name in KNOWN_TOOLS or bool(_LIB_RE.match(name))


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

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = dest_real / f"{_BACKUP_PREFIX}{ts}"
        try:
            backup.mkdir()
            for name in names:
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
            for name in names:
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
        _prune_backups(dest_real, retain, emit)
    except Exception:
        pass  # pruning is best-effort; a completed swap must not fail here

    emit(f"[ok] upgraded {len(names)} file(s) at {dest_real}: {', '.join(names)}")
    emit(f"[ok] previous binaries backed up to {backup}")
    emit(f"restart to run the new build: sudo -n /usr/bin/systemctl restart {unit}")
    return UpgradeResult(True, "upgraded", target=dest_bin, swapped=names, backup_dir=str(backup))
