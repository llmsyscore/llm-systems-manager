"""Unit tests for agent/frozen_self_update.py (loaded standalone via conftest)."""
import hashlib
import importlib.util
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from conftest import frozen_self_update as fsu


# ---- resolve_platform_asset -------------------------------------------------

@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", "llm-systems-agent-linux-x86_64"),
    ("Linux", "amd64", "llm-systems-agent-linux-x86_64"),
    ("Linux", "aarch64", "llm-systems-agent-linux-arm64"),
    ("Linux", "arm64", "llm-systems-agent-linux-arm64"),
    ("Darwin", "arm64", "llm-systems-agent-macos-arm64"),
    ("Darwin", "x86_64", None),
    ("Windows", "x86_64", None),
    ("Linux", "riscv64", None),
])
def test_resolve_platform_asset(system, machine, expected):
    assert fsu.resolve_platform_asset(system, machine) == expected


# ---- release_base -----------------------------------------------------------

def test_release_base_env_override(monkeypatch):
    monkeypatch.setenv("LSM_AGENT_RELEASE_BASE", "https://mirror.local/rel/")
    assert fsu.release_base() == "https://mirror.local/rel"
    monkeypatch.delenv("LSM_AGENT_RELEASE_BASE")
    assert fsu.release_base().startswith("https://github.com/llmsyscore/")


# ---- parse_sha256_line ------------------------------------------------------

ASSET = "llm-systems-agent-linux-x86_64"
HEX = "a" * 64


def test_parse_sha256_ok():
    assert fsu.parse_sha256_line(f"{HEX}  {ASSET}\n", ASSET) == HEX


def test_parse_sha256_star_binary_marker():
    assert fsu.parse_sha256_line(f"{HEX} *{ASSET}\n", ASSET) == HEX


def test_parse_sha256_uppercase_normalized():
    assert fsu.parse_sha256_line(f"{'A' * 64}  {ASSET}", ASSET) == HEX


def test_parse_sha256_wrong_name_rejected():
    with pytest.raises(fsu.UpdateError):
        fsu.parse_sha256_line(f"{HEX}  other-file", ASSET)


def test_parse_sha256_bad_hex_rejected():
    with pytest.raises(fsu.UpdateError):
        fsu.parse_sha256_line(f"zz{'a' * 62}  {ASSET}", ASSET)


def test_parse_sha256_empty_rejected():
    with pytest.raises(fsu.UpdateError):
        fsu.parse_sha256_line("", ASSET)


# ---- download_asset (injected getter, no network) ---------------------------

class FakeResp:
    def __init__(self, *, text="", content=b"", status=200):
        self.text, self._content, self._status = text, content, status

    def raise_for_status(self):
        if self._status != 200:
            raise RuntimeError(f"HTTP {self._status}")

    def iter_content(self, n):
        for i in range(0, len(self._content), n):
            yield self._content[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


TARBALL = f"{ASSET}.tar.gz"


def _make_tarball(binary=b"ELF fake binary", member=fsu.BINARY_MEMBER, extra=None):
    """Build a .tar.gz holding <member>=binary (+ optional (name, bytes) extra)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(binary)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(binary))
        if extra is not None:
            name, data = extra
            ei = tarfile.TarInfo(name)
            ei.size = len(data)
            tar.addfile(ei, io.BytesIO(data))
    return buf.getvalue()


def _fake_get_factory(body: bytes = None, sha_text=None, bin_status=200, sha_status=200):
    body = _make_tarball() if body is None else body
    sha = sha_text if sha_text is not None else \
        f"{hashlib.sha256(body).hexdigest()}  {TARBALL}\n"

    def get(url, **kw):
        if url.endswith(".sha256"):
            return FakeResp(text=sha, status=sha_status)
        return FakeResp(content=body, status=bin_status)
    return get


def test_download_asset_ok(tmp_path):
    binary = b"ELF fake binary"
    body = _make_tarball(binary)
    staged, hexd = fsu.download_asset("https://x", ASSET, tmp_path,
                                      get=_fake_get_factory(body))
    assert staged.name == fsu.BINARY_MEMBER
    assert staged.read_bytes() == binary
    assert hexd == hashlib.sha256(body).hexdigest()
    assert os.stat(staged).st_mode & 0o111
    assert not (tmp_path / TARBALL).exists()   # tarball cleaned up after extract


def test_download_asset_sha_mismatch(tmp_path):
    bad = f"{'b' * 64}  {TARBALL}\n"
    with pytest.raises(fsu.UpdateError, match="sha256 mismatch"):
        fsu.download_asset("https://x", ASSET, tmp_path,
                           get=_fake_get_factory(sha_text=bad))


def test_download_asset_http_error(tmp_path):
    with pytest.raises(fsu.UpdateError, match="download failed"):
        fsu.download_asset("https://x", ASSET, tmp_path,
                           get=_fake_get_factory(bin_status=404))


def test_download_asset_sha_http_error(tmp_path):
    with pytest.raises(fsu.UpdateError, match="checksum download failed"):
        fsu.download_asset("https://x", ASSET, tmp_path,
                           get=_fake_get_factory(sha_status=404))


def test_download_asset_missing_binary_member(tmp_path):
    body = _make_tarball(member="some-other-file")
    with pytest.raises(fsu.UpdateError, match="no 'llm-systems-agent' member"):
        fsu.download_asset("https://x", ASSET, tmp_path, get=_fake_get_factory(body))


def test_download_asset_ignores_traversal_member(tmp_path):
    # A crafted tarball carrying a path-traversal member must not escape
    # dest_dir: extraction only pulls the fixed BINARY_MEMBER by name.
    binary = b"ELF fake binary"
    body = _make_tarball(binary, extra=("../evil", b"pwned"))
    staged, _ = fsu.download_asset("https://x", ASSET, tmp_path, get=_fake_get_factory(body))
    assert staged == tmp_path / fsu.BINARY_MEMBER
    assert staged.read_bytes() == binary
    assert not (tmp_path.parent / "evil").exists()


def test_download_asset_corrupt_tarball(tmp_path):
    # A sha-matching but non-gzip body must fail as a clean UpdateError, not a
    # raw TarError/OSError (the caller only handles UpdateError).
    body = b"not a gzip tarball"
    with pytest.raises(fsu.UpdateError, match="extract failed"):
        fsu.download_asset("https://x", ASSET, tmp_path, get=_fake_get_factory(body))


# ---- staged_version ---------------------------------------------------------

def _fake_binary(tmp_path, script: str) -> Path:
    p = tmp_path / "fake-agent"
    p.write_text(f"#!/bin/sh\n{script}\n")
    p.chmod(0o755)
    return p


def test_staged_version_ok(tmp_path):
    p = _fake_binary(tmp_path, 'echo "v2026.07.15-2"')
    assert fsu.staged_version(p) == "v2026.07.15-2"


def test_staged_version_nonzero_exit(tmp_path):
    p = _fake_binary(tmp_path, "echo boom >&2; exit 3")
    with pytest.raises(fsu.UpdateError, match="exited 3"):
        fsu.staged_version(p)


def test_staged_version_no_output(tmp_path):
    p = _fake_binary(tmp_path, "exit 0")
    with pytest.raises(fsu.UpdateError, match="no output"):
        fsu.staged_version(p)


# ---- ensure_writable / swap_binary ------------------------------------------

def test_swap_binary_happy_path(tmp_path):
    live = tmp_path / "llm-systems-agent"
    live.write_bytes(b"old")
    staged = tmp_path / (fsu.STAGE_PREFIX + "new")
    staged.write_bytes(b"new")
    staged.chmod(0o755)
    backup = fsu.swap_binary(staged, live)
    assert live.read_bytes() == b"new"
    assert Path(backup).read_bytes() == b"old"
    assert Path(backup).name.startswith(fsu.BACKUP_PREFIX)
    assert not staged.exists()


def test_swap_binary_prunes_old_backups(tmp_path):
    live = tmp_path / "llm-systems-agent"
    for i in range(3):
        live.write_bytes(f"gen{i}".encode())
        staged = tmp_path / f"{fsu.STAGE_PREFIX}{i}"
        staged.write_bytes(f"gen{i + 1}".encode())
        fsu.swap_binary(staged, live, retain=1)
    backups = [p for p in tmp_path.iterdir() if p.name.startswith(fsu.BACKUP_PREFIX)]
    assert len(backups) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_ensure_writable_refuses_unwritable(tmp_path):
    d = tmp_path / "rootish"
    d.mkdir()
    live = d / "llm-systems-agent"
    live.write_bytes(b"old")
    d.chmod(0o555)
    try:
        with pytest.raises(fsu.UpdateError, match="not writable"):
            fsu.ensure_writable(live)
    finally:
        d.chmod(0o755)


# ---- --version flag ----------------------------------------------------------

@pytest.mark.skipif(importlib.util.find_spec("psutil") is None,
                    reason="full agent deps not installed in this venv")
def test_agent_version_flag():
    agent_py = Path(__file__).resolve().parent.parent / "llm-systems-agent.py"
    r = subprocess.run([sys.executable, str(agent_py), "--version"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0
    assert r.stdout.strip().startswith("v20")
