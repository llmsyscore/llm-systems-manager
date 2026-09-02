"""#797: backup-status last_export + folder_bytes, and POST /api/admin/backup-now."""
from __future__ import annotations

import types

import pytest

import export_log
import manager_mod as M


@pytest.fixture
def backup_env(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    monkeypatch.setattr(M, "_BACKUP_DIR", bdir)
    monkeypatch.setattr(M, "_BACKUP_STATUS_FILE", bdir / "last_backup.json")
    M._backup_status.clear()
    export_log.configure(tmp_path / "last_export.json")
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    monkeypatch.setattr(M.settings.manager, "backup",
                        types.SimpleNamespace(enabled=True, interval_hours=24.0,
                                              keep_last=3, passphrase="",
                                              mirror_dir=""),
                        raising=False)
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c, tmp_path
    export_log.reset()


# ── export log ───────────────────────────────────────────────────────

def test_export_log_starts_empty_and_persists(tmp_path):
    store = tmp_path / "last_export.json"
    export_log.configure(store)
    assert export_log.last_export() == {"manager": None, "alarm_engine": None}
    export_log.record("manager", 4096, ts=1000.0)
    export_log.record("alarm_engine", 2048, ts=1001.0)
    assert export_log.last_export() == {
        "manager": {"ts": 1000.0, "bytes": 4096},
        "alarm_engine": {"ts": 1001.0, "bytes": 2048}}
    # Reloaded from disk by a fresh configure().
    export_log.reset()
    export_log.configure(store)
    assert export_log.last_export()["manager"] == {"ts": 1000.0, "bytes": 4096}


def test_export_log_ignores_unknown_components(tmp_path):
    export_log.configure(tmp_path / "last_export.json")
    export_log.record("something-else", 10)
    assert export_log.last_export() == {"manager": None, "alarm_engine": None}


def test_manual_manager_export_records_last_export(backup_env):
    c, _ = backup_env
    assert export_log.last_export()["manager"] is None
    r = c.post("/api/admin/export/manager", json={})
    assert r.status_code == 200
    rec = export_log.last_export()["manager"]
    assert rec and rec["bytes"] == len(r.data) and rec["ts"] > 0


# ── backup-status ────────────────────────────────────────────────────

def test_backup_status_reports_last_export_and_folder_bytes(backup_env):
    c, _ = backup_env
    M._run_scheduled_backup("", 3, "")
    export_log.record("alarm_engine", 777, ts=1234.0)
    d = c.get("/api/admin/backup-status").get_json()
    assert d["ok"] is True
    assert d["last_export"]["alarm_engine"] == {"ts": 1234.0, "bytes": 777}
    assert d["last_export"]["manager"] is None
    assert d["folder_bytes"] == sum(b["bytes"] for b in d["backups"])
    assert d["folder_bytes"] > 0


def test_backup_status_folder_bytes_zero_when_empty(backup_env):
    c, _ = backup_env
    d = c.get("/api/admin/backup-status").get_json()
    assert d["backups"] == [] and d["folder_bytes"] == 0


def test_backup_status_mirrored_is_null_when_no_mirror_dir(backup_env):
    c, _ = backup_env
    M._run_scheduled_backup("", 3, "")
    d = c.get("/api/admin/backup-status").get_json()
    assert d["backups"] and all(b["mirrored"] is None for b in d["backups"])


def test_backup_status_mirrored_true_when_mirror_copy_exists(backup_env, monkeypatch, tmp_path):
    c, _ = backup_env
    mdir = tmp_path / "mirror"
    monkeypatch.setattr(M.settings.manager, "backup",
                        types.SimpleNamespace(enabled=True, interval_hours=24.0,
                                              keep_last=3, passphrase="",
                                              mirror_dir=str(mdir)),
                        raising=False)
    M._run_scheduled_backup("", 3, str(mdir))
    d = c.get("/api/admin/backup-status").get_json()
    assert d["backups"] and all(b["mirrored"] is True for b in d["backups"])


def test_backup_status_mirrored_false_when_mirror_copy_missing(backup_env, monkeypatch, tmp_path):
    c, _ = backup_env
    mdir = tmp_path / "mirror"
    monkeypatch.setattr(M.settings.manager, "backup",
                        types.SimpleNamespace(enabled=True, interval_hours=24.0,
                                              keep_last=3, passphrase="",
                                              mirror_dir=str(mdir)),
                        raising=False)
    # Backup was made when mirror_dir was unset, so no mirrored copy exists.
    M._run_scheduled_backup("", 3, "")
    d = c.get("/api/admin/backup-status").get_json()
    assert d["backups"] and all(b["mirrored"] is False for b in d["backups"])


# ── backup-now ───────────────────────────────────────────────────────

def test_backup_now_runs_one_cycle(backup_env):
    c, _ = backup_env
    d = c.post("/api/admin/backup-now").get_json()
    assert d["ok"] is True
    assert d["last"]["ok"] is True and d["last"]["bytes"] > 0
    assert len(M._list_auto_backups()) == 1
    # The same block is what backup-status then serves.
    assert c.get("/api/admin/backup-status").get_json()["last"]["file"] == \
        d["last"]["file"]


def test_backup_now_409_when_the_scheduler_is_disabled(backup_env, monkeypatch):
    c, _ = backup_env
    monkeypatch.setattr(M.settings.manager, "backup",
                        types.SimpleNamespace(enabled=False, interval_hours=24.0,
                                              keep_last=3, passphrase="",
                                              mirror_dir=""),
                        raising=False)
    r = c.post("/api/admin/backup-now")
    assert r.status_code == 409 and r.get_json()["ok"] is False
    assert M._list_auto_backups() == []


def test_backup_now_409_on_a_too_short_passphrase(backup_env, monkeypatch):
    c, _ = backup_env
    monkeypatch.setattr(M.settings.manager, "backup",
                        types.SimpleNamespace(enabled=True, interval_hours=24.0,
                                              keep_last=3, passphrase="short",
                                              mirror_dir=""),
                        raising=False)
    assert c.post("/api/admin/backup-now").status_code == 409


def test_backup_now_is_audited():
    action, _target, event = M._audit_match("POST", "/api/admin/backup-now")
    assert (action, event) == ("backup.run", "backup")


def test_scheduled_backup_runs_serialise_on_one_lock(monkeypatch):
    import importlib, threading
    mgr = importlib.import_module("llm-systems-manager")
    seen = []
    def fake(p, k, m):
        seen.append(mgr._backup_run_lock.locked())
        return {"ok": True}
    monkeypatch.setattr(mgr, "_run_scheduled_backup_locked", fake)
    mgr._run_scheduled_backup("", 1, "")
    assert seen == [True]
    assert not mgr._backup_run_lock.locked()
