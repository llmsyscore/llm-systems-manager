"""Scheduled automatic backups (#218): export blob, run, prune, status."""
from __future__ import annotations

import json
import stat

import pytest

import manager_mod as manager_mod  # noqa: E402  # loaded by conftest
import _archive


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    monkeypatch.setattr(manager_mod, "_BACKUP_DIR", bdir)
    monkeypatch.setattr(manager_mod, "_BACKUP_STATUS_FILE", bdir / "last_backup.json")
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "")
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
    enabled, interval_h, keep_last, mirror = manager_mod._backup_cfg()
    assert isinstance(enabled, bool)
    assert interval_h >= 0
    assert keep_last >= 1
    assert isinstance(manager_mod._backup_passphrase(), str)


# ── #855: the Alarm Engine rides along on every scheduled run ────────

def _fake_ae_blob(passphrase):
    files = {"data/ae_alarms.db": b"ae", "manifest.json": b'{"component": "alarm_engine"}'}
    return _archive.encrypt(_archive.pack_tar(files), passphrase or None)


def test_run_scheduled_backup_writes_a_matching_ae_archive(backup_dir, monkeypatch):
    monkeypatch.setattr(manager_mod, "_ae_backup_coverage", lambda: None)
    monkeypatch.setattr(manager_mod, "_fetch_ae_export_blob", _fake_ae_blob)
    st = manager_mod._run_scheduled_backup("", 5, "")
    assert st["ok"] is True and st["partial"] is False
    names = sorted(p.name for p in manager_mod._list_auto_backups())
    assert len(names) == 2
    ae = [n for n in names if n.startswith("lsm-auto-ae-")]
    mgr = [n for n in names if n.startswith("lsm-auto-manager-")]
    assert len(ae) == 1 and len(mgr) == 1
    assert manager_mod._backup_run_stamp(ae[0]) == manager_mod._backup_run_stamp(mgr[0])
    comps = st["components"]
    assert comps["manager"]["ok"] is True and comps["manager"]["file"] == mgr[0]
    assert comps["alarm_engine"]["ok"] is True and comps["alarm_engine"]["file"] == ae[0]
    assert comps["alarm_engine"]["bytes"] == (backup_dir / ae[0]).stat().st_size
    blob = (backup_dir / ae[0]).read_bytes()
    files = _archive.unpack_tar(_archive.decrypt(blob, None))
    assert json.loads(files["manifest.json"])["component"] == "alarm_engine"


def test_run_scheduled_backup_encrypts_the_ae_archive_with_the_same_passphrase(backup_dir, monkeypatch):
    monkeypatch.setattr(manager_mod, "_ae_backup_coverage", lambda: None)
    seen = {}
    def fetch(passphrase):
        seen["pw"] = passphrase
        return _fake_ae_blob(passphrase)
    monkeypatch.setattr(manager_mod, "_fetch_ae_export_blob", fetch)
    st = manager_mod._run_scheduled_backup("correct horse battery", 5, "")
    assert st["ok"] is True and seen["pw"] == "correct horse battery"
    ae = next(p for p in manager_mod._list_auto_backups() if p.name.startswith("lsm-auto-ae-"))
    assert _archive.sniff_encrypted(ae.read_bytes()) is True


def test_run_scheduled_backup_is_partial_when_the_ae_fetch_fails(backup_dir, monkeypatch):
    monkeypatch.setattr(manager_mod, "_ae_backup_coverage", lambda: None)
    def fetch(passphrase):
        raise manager_mod._AeBackupError("unauthorized", "HTTP 403", "set the token")
    monkeypatch.setattr(manager_mod, "_fetch_ae_export_blob", fetch)
    st = manager_mod._run_scheduled_backup("", 5, "")
    assert st["ok"] is True and st["partial"] is True
    assert st["error"] is None
    names = [p.name for p in manager_mod._list_auto_backups()]
    assert len(names) == 1 and names[0].startswith("lsm-auto-manager-")
    ae = st["components"]["alarm_engine"]
    assert ae["ok"] is False and ae["file"] is None
    assert ae["error"] == "unauthorized — HTTP 403" and ae["remedy"] == "set the token"


def test_run_scheduled_backup_mirrors_both_archives(backup_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "_ae_backup_coverage", lambda: None)
    monkeypatch.setattr(manager_mod, "_fetch_ae_export_blob", _fake_ae_blob)
    mirror = tmp_path / "mirror"
    st = manager_mod._run_scheduled_backup("", 5, str(mirror))
    assert st["ok"] is True and st.get("mirrored") is True
    assert sorted(p.name for p in mirror.glob("*.lsmenc")) == \
        sorted(p.name for p in manager_mod._list_auto_backups())


def test_list_auto_backups_orders_by_run_then_name(backup_dir):
    backup_dir.mkdir(parents=True)
    for n in ["lsm-auto-manager-h-20260902-000000.lsmenc", "lsm-auto-ae-h-20260903-000000.lsmenc",
              "lsm-auto-manager-h-20260903-000000.lsmenc", "lsm-auto-ae-h-20260901-000000.lsmenc",
              "lsm-auto-other-h-20260904-000000.lsmenc", "manual.lsmenc"]:
        (backup_dir / n).write_bytes(b"x")
    assert [p.name for p in manager_mod._list_auto_backups()] == [
        "lsm-auto-ae-h-20260901-000000.lsmenc",
        "lsm-auto-manager-h-20260902-000000.lsmenc",
        "lsm-auto-ae-h-20260903-000000.lsmenc",
        "lsm-auto-manager-h-20260903-000000.lsmenc",
    ]


def test_prune_auto_backups_keeps_whole_runs(backup_dir):
    backup_dir.mkdir(parents=True)
    runs = ["20260901-000000", "20260902-000000", "20260903-000000", "20260904-000000"]
    for r in runs:
        (backup_dir / f"lsm-auto-manager-h-{r}.lsmenc").write_bytes(b"m")
        if r != "20260903-000000":  # no AE archive for this run
            (backup_dir / f"lsm-auto-ae-h-{r}.lsmenc").write_bytes(b"a")
    (backup_dir / "manual-export.lsmenc").write_bytes(b"y")
    removed = manager_mod._prune_auto_backups(2)
    assert removed == 4
    left = sorted(p.name for p in backup_dir.glob("*.lsmenc"))
    assert left == sorted(["manual-export.lsmenc",
                           "lsm-auto-manager-h-20260903-000000.lsmenc",
                           "lsm-auto-ae-h-20260904-000000.lsmenc",
                           "lsm-auto-manager-h-20260904-000000.lsmenc"])


def test_prune_never_orphans_a_run_partner(backup_dir):
    backup_dir.mkdir(parents=True)
    for r in ["20260901-000000", "20260902-000000"]:
        (backup_dir / f"lsm-auto-manager-h-{r}.lsmenc").write_bytes(b"m")
        (backup_dir / f"lsm-auto-ae-h-{r}.lsmenc").write_bytes(b"a")
    assert manager_mod._prune_auto_backups(1) == 2
    left = sorted(p.name for p in backup_dir.glob("*.lsmenc"))
    assert left == ["lsm-auto-ae-h-20260902-000000.lsmenc",
                    "lsm-auto-manager-h-20260902-000000.lsmenc"]


def test_fetch_ae_export_blob_without_an_ae_url(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "")
    with pytest.raises(manager_mod._AeBackupError) as ei:
        manager_mod._fetch_ae_export_blob("")
    assert "alarm_engine_url" in ei.value.remedy
    assert ei.value.kind == "unconfigured"


class _Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


def test_fetch_ae_export_blob_names_the_token_gate_on_403(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae.example:8081")
    monkeypatch.setattr(manager_mod._ae_session, "post", lambda *a, **k: _Resp(403))
    with pytest.raises(manager_mod._AeBackupError) as ei:
        manager_mod._fetch_ae_export_blob("")
    assert ei.value.kind == "unauthorized"
    assert "management_token" in ei.value.remedy


def test_fetch_ae_export_blob_reports_unreachable(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae.example:8081")
    def boom(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(manager_mod._ae_session, "post", boom)
    with pytest.raises(manager_mod._AeBackupError) as ei:
        manager_mod._fetch_ae_export_blob("")
    assert ei.value.kind == "unreachable"
    assert "refused" not in ei.value.detail


def test_fetch_ae_export_blob_rejects_a_non_archive_body(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae.example:8081")
    monkeypatch.setattr(manager_mod._ae_session, "post",
                        lambda *a, **k: _Resp(200, b"<html>login</html>"))
    with pytest.raises(manager_mod._AeBackupError) as ei:
        manager_mod._fetch_ae_export_blob("")
    assert ei.value.kind == "http"


def test_fetch_ae_export_blob_sends_the_passphrase_and_returns_the_body(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae.example:8081/")
    calls = {}
    blob = _fake_ae_blob("correct horse battery")
    def post(url, json=None, timeout=None, **k):
        calls.update({"url": url, "json": json, "timeout": timeout})
        return _Resp(200, blob)
    monkeypatch.setattr(manager_mod._ae_session, "post", post)
    assert manager_mod._fetch_ae_export_blob("correct horse battery") == blob
    assert calls["url"] == "http://ae.example:8081/api/alarm/admin/export"
    assert calls["json"] == {"password": "correct horse battery"}


def test_fetch_ae_export_blob_accepts_a_plaintext_archive(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae.example:8081")
    blob = _fake_ae_blob("")
    monkeypatch.setattr(manager_mod._ae_session, "post", lambda *a, **k: _Resp(200, blob))
    assert manager_mod._fetch_ae_export_blob("") == blob


def test_fetch_ae_export_blob_rejects_an_unencrypted_archive_when_a_passphrase_is_set(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae.example:8081")
    monkeypatch.setattr(manager_mod._ae_session, "post", lambda *a, **k: _Resp(200, _fake_ae_blob("")))
    with pytest.raises(manager_mod._AeBackupError) as ei:
        manager_mod._fetch_ae_export_blob("correct horse battery")
    assert "passphrase" in ei.value.detail


def test_run_without_an_ae_is_not_partial(backup_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(manager_mod, "_fetch_ae_export_blob", lambda pw: calls.append(pw))
    st = manager_mod._run_scheduled_backup("", 5, "")
    assert st["ok"] is True and st["partial"] is False and calls == []
    ae = st["components"]["alarm_engine"]
    assert ae["ok"] is False and ae["error"] is None
    assert "alarm_engine_url" in ae["skipped"]


def test_coverage_requires_a_management_token(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae.example:8081")
    monkeypatch.setattr(manager_mod.settings.alarm_engine, "management_token", "", raising=False)
    assert "management_token" in manager_mod._ae_backup_coverage()
    monkeypatch.setattr(manager_mod.settings.alarm_engine, "management_token", "tok", raising=False)
    assert manager_mod._ae_backup_coverage() is None


def test_fetch_ae_export_blob_remedy_names_the_export_route(monkeypatch):
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://ae.example:8081")
    monkeypatch.setattr(manager_mod._ae_session, "post", lambda *a, **k: _Resp(404))
    with pytest.raises(manager_mod._AeBackupError) as ei:
        manager_mod._fetch_ae_export_blob("")
    assert ei.value.kind == "unsupported" and "/api/alarm/admin/export" in ei.value.remedy


def test_list_auto_backups_skips_files_without_a_run_stamp(backup_dir):
    backup_dir.mkdir(parents=True)
    (backup_dir / "lsm-auto-manager-h-20260901-000000.lsmenc").write_bytes(b"m")
    (backup_dir / "lsm-auto-manager-copy.lsmenc").write_bytes(b"m")
    assert [p.name for p in manager_mod._list_auto_backups()] == ["lsm-auto-manager-h-20260901-000000.lsmenc"]
    assert manager_mod._prune_auto_backups(1) == 0
    assert (backup_dir / "lsm-auto-manager-copy.lsmenc").exists()


def test_mirror_failure_is_reported_per_archive(backup_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "_ae_backup_coverage", lambda: None)
    monkeypatch.setattr(manager_mod, "_fetch_ae_export_blob", _fake_ae_blob)
    real_copy = manager_mod.shutil.copy2
    def copy2(src, dst):
        if "lsm-auto-ae-" in str(src):
            raise OSError("disk full")
        return real_copy(src, dst)
    monkeypatch.setattr(manager_mod.shutil, "copy2", copy2)
    mirror = tmp_path / "mirror"
    st = manager_mod._run_scheduled_backup("", 5, str(mirror))
    assert st["ok"] is True and st["mirrored"] is False
    assert st["mirror_failed"] == [st["components"]["alarm_engine"]["file"]]
    assert [p.name for p in mirror.glob("*.lsmenc")] == [st["components"]["manager"]["file"]]
