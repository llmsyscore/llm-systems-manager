"""Self-update for frozen (PyInstaller) agent binaries. Stdlib-only leaf module
(requests imported lazily) so it loads standalone in tests via importlib."""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

RELEASE_BASE_DEFAULT = (
    "https://github.com/llmsyscore/llm-systems-manager/releases/latest/download"
)
STAGE_PREFIX = ".self-update.stage."
BACKUP_PREFIX = ".self-update.bak."

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")

_ASSETS = {
    ("linux", "x86_64"): "llm-systems-agent-linux-x86_64",
    ("linux", "amd64"): "llm-systems-agent-linux-x86_64",
    ("linux", "aarch64"): "llm-systems-agent-linux-arm64",
    ("linux", "arm64"): "llm-systems-agent-linux-arm64",
    ("darwin", "arm64"): "llm-systems-agent-macos-arm64",
}


class UpdateError(Exception):
    """Fatal self-update failure; the live binary is untouched."""


def release_base() -> str:
    return os.environ.get("LSM_AGENT_RELEASE_BASE", "").rstrip("/") or RELEASE_BASE_DEFAULT


def resolve_platform_asset(system: "str | None" = None,
                           machine: "str | None" = None) -> "str | None":
    import platform
    sysname = (system or platform.system()).lower()
    mach = (machine or platform.machine()).lower()
    return _ASSETS.get((sysname, mach))


def parse_sha256_line(text: str, asset: str) -> str:
    """Extract the hex digest for asset from `<hex>  <name>` checksum lines."""
    for line in (text or "").splitlines():
        parts = line.split()
        if (len(parts) >= 2 and parts[-1].lstrip("*") == asset
                and _HEX64.match(parts[0])):
            return parts[0].lower()
    raise UpdateError(f"no valid sha256 entry for {asset} in the checksum file")


def download_asset(base: str, asset: str, dest_dir: Path,
                   get=None, timeout: int = 300) -> "tuple[Path, str]":
    """Download <asset> + <asset>.sha256 from base into dest_dir, verify the
    checksum, chmod 0755. Returns (staged_path, sha256_hex)."""
    if get is None:
        import requests
        get = requests.get
    try:
        r = get(f"{base}/{asset}.sha256", timeout=60)
        r.raise_for_status()
        sha_text = r.text
    except Exception as e:
        raise UpdateError(f"checksum download failed: {e}")
    expected = parse_sha256_line(sha_text, asset)

    staged = Path(dest_dir) / asset
    h = hashlib.sha256()
    try:
        with get(f"{base}/{asset}", stream=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(staged, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
                        h.update(chunk)
                f.flush()
                os.fsync(f.fileno())
    except Exception as e:
        raise UpdateError(f"binary download failed: {e}")
    if h.hexdigest() != expected:
        raise UpdateError("sha256 mismatch — refusing update (old binary untouched)")
    os.chmod(staged, 0o755)
    return staged, expected


def staged_version(staged: Path, timeout: int = 120) -> str:
    """Run `<staged> --version`; the last stdout line is the version string."""
    try:
        r = subprocess.run([str(staged), "--version"], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise UpdateError("staged binary --version timed out — refusing swap")
    except OSError as e:
        raise UpdateError(f"staged binary failed to execute: {e}")
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-300:]
        raise UpdateError(f"staged binary --version exited {r.returncode}: {tail}")
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise UpdateError("staged binary --version produced no output")
    return lines[-1]


def _probe_writable(d: Path) -> bool:
    """Confirm os.replace works inside d (covers sticky-bit dirs)."""
    try:
        fd, tmp = tempfile.mkstemp(prefix=STAGE_PREFIX, dir=str(d))
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
                pass
        return False


def ensure_writable(live: Path) -> Path:
    """Realpath dest dir of the live binary; raises unless agent-owned+writable."""
    dest = Path(os.path.realpath(Path(live).parent))
    try:
        st = os.stat(dest)
    except OSError as e:
        raise UpdateError(f"cannot stat {dest}: {e}")
    if st.st_uid != os.geteuid() or not _probe_writable(dest):
        raise UpdateError(
            f"{dest} is not writable by the agent user (owner uid {st.st_uid}) — "
            f"chown it to the agent user and retry, or replace the binary manually")
    return dest


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _prune_backups(dest: Path, retain: int) -> None:
    backups = sorted(p for p in dest.iterdir()
                     if p.is_file() and p.name.startswith(BACKUP_PREFIX))
    for old in backups[:-max(1, int(retain))]:
        try:
            old.unlink()
        except OSError:
            pass


def swap_binary(staged: Path, live: Path, retain: int = 1,
                dest: "Path | None" = None) -> str:
    """Back up the live binary then atomically replace it with staged.
    os.replace is atomic: on failure the live binary is untouched."""
    dest = dest if dest is not None else ensure_writable(live)
    live_real = dest / Path(live).name
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = dest / f"{BACKUP_PREFIX}{ts}"
    try:
        shutil.copy2(live_real, backup)
        with open(backup, "rb") as f:
            os.fsync(f.fileno())
    except OSError as e:
        raise UpdateError(f"backup failed: {e}; aborting swap (old binary untouched)")
    try:
        os.replace(staged, live_real)
    except OSError as e:
        try:
            backup.unlink()
        except OSError:
            pass
        raise UpdateError(f"swap failed: {e}; old binary untouched")
    _fsync_dir(dest)
    try:
        _prune_backups(dest, retain)
    except Exception:
        pass
    return str(backup)
