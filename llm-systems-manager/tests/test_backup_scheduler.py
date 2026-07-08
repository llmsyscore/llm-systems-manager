"""Scheduled automatic backups (#218): export blob, run, prune, status."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import manager_mod as manager_mod  # noqa: E402  # loaded by conftest
import _archive


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    monkeypatch.setattr(manager_mod, "_BACKUP_DIR", bdir)
    monkeypatch.setattr(manager_mod, "_BACKUP_STATUS_FILE", bdir / "last_backup.json")
    manager_mod._backup_status.clear()
    return bdir


def test_export_blob_unencrypted_roundtrip():
    blob, n_files = manager_mod._build_manager_export_blob(None)
    assert n_files >= 1
    assert _archive.sniff_encrypted(blob) is False
    files = _archive.unpack_tar(_archive.decrypt(blob, None))
    assert "manifest.json" in files
    manifest = json.loads(files["manifest.json"])
    assert manifest["component"] == "manager"


def test_export_blob_encrypted_roundtrip():
    blob, _ = manager_mod._build_manager_export_blob("correct horse battery")
    assert _archive.sniff_encrypted(blob) is True
    files = _archive.unpack_tar(_archive.decrypt(blob, "correct horse battery"))
    assert "manifest.json" in files
    with pytest.raises(ValueError):
        _archive.decrypt(blob, "wrong password!!")


def test_run_scheduled_backup_writes_archive_and_status(backup_dir):
    st = manager_mod._run_scheduled_backup("", 5, "")
    assert st["ok"] is True, st
    assert st["encrypted"] is False
    archives = manager_mod._list_auto_backups()
    assert len(archives) == 1
    assert archives[0].name.startswith(manager_mod._BACKUP_PREFIX)
    mode = stat.S_IMODE(archives[0].stat().st_mode)
    assert mode == 0o600
    # Status persisted + readable.
    assert manager_mod._get_backup_status()["ok"] is True
    manager_mod._backup_status.clear()
    assert manager_mod._get_backup_status()["ok"] is True  # re-read from disk


def test_run_scheduled_backup_mirror(backup_dir, tmp_path):
    mirror = tmp_path / "mirror"
    st = manager_mod._run_scheduled_backup("", 5, str(mirror))
    assert st["ok"] is True and st.get("mirrored") is True
    assert len(list(mirror.glob("*.lsmenc"))) == 1


def test_prune_auto_backups_keeps_newest(backup_dir):
    backup_dir.mkdir(parents=True)
    names = [f"{manager_mod._BACKUP_PREFIX}host-2026070{i}-000000.lsmenc" for i in range(1, 6)]
    for n in names:
        (backup_dir / n).write_bytes(b"x")
    # Unrelated files must never be touched.
    (backup_dir / "manual-export.lsmenc").write_bytes(b"y")
    removed = manager_mod._prune_auto_backups(2)
    assert removed == 3
    left = sorted(p.name for p in backup_dir.glob("*.lsmenc"))
    assert left == sorted(["manual-export.lsmenc", names[3], names[4]])


def test_backup_cfg_defaults():
    enabled, interval_h, keep_last, passphrase, mirror = manager_mod._backup_cfg()
    assert isinstance(enabled, bool)
    assert interval_h >= 0
    assert keep_last >= 1
