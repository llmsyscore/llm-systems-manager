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
    monkeypatch.setattr(M, "_alarm_engine_url", "")
    # Keep the on-disk config out of the run: backup-now reads live settings otherwise.
    monkeypatch.setattr(M, "_backup_reload_if_changed", lambda: None)
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
    import importlib
    mgr = importlib.import_module("llm-systems-manager")
    seen = []
    def fake(p, k, m):
        seen.append(mgr._backup_run_lock.locked())
        return {"ok": True}
    monkeypatch.setattr(mgr, "_run_scheduled_backup_locked", fake)
    mgr._run_scheduled_backup("", 1, "")
    assert seen == [True]
    assert not mgr._backup_run_lock.locked()


# ── backup-archive download (#848) ───────────────────────────────────

def test_backup_archive_downloads_a_listed_file(backup_env):
    c, _ = backup_env
    M._run_scheduled_backup("", 3, "")
    name = c.get("/api/admin/backup-status").get_json()["backups"][0]["file"]
    r = c.get(f"/api/admin/backup-archive/{name}")
    assert r.status_code == 200
    assert r.data == (M._BACKUP_DIR / name).read_bytes()
    assert name in r.headers["Content-Disposition"]


def test_backup_archive_rejects_a_file_outside_the_listing(backup_env, tmp_path):
    c, _ = backup_env
    M._run_scheduled_backup("", 3, "")
    (M._BACKUP_DIR / "notes.txt").write_text("secret")
    # The first two are excluded by the listing glob; the last three probe the
    # containment check itself with names the glob would otherwise admit.
    for name in ("notes.txt", "last_backup.json",
                 M._BACKUP_PREFIX + "nope.lsmenc",
                 "..%2f..%2f" + M._BACKUP_PREFIX + "x.lsmenc",
                 "../" + M._BACKUP_PREFIX + "x.lsmenc"):
        assert c.get(f"/api/admin/backup-archive/{name}").status_code == 404


def test_backup_archive_404s_when_the_file_is_pruned_mid_request(backup_env, monkeypatch):
    """The retention pruner can unlink between the listing and send_file."""
    c, _ = backup_env
    M._run_scheduled_backup("", 3, "")
    gone = M._BACKUP_DIR / (M._BACKUP_PREFIX + "vanished.lsmenc")
    monkeypatch.setattr(M, "_list_auto_backups", lambda: [gone])
    r = c.get(f"/api/admin/backup-archive/{gone.name}")
    assert r.status_code == 404
    assert r.get_json()["error"] == "no such archive"


def test_backup_archive_requires_admin(backup_env, monkeypatch):
    c, _ = backup_env
    M._run_scheduled_backup("", 3, "")
    name = c.get("/api/admin/backup-status").get_json()["backups"][0]["file"]
    monkeypatch.setattr(M, "_require_admin",
                        lambda: (M.jsonify({"ok": False, "error": "admin role required",
                                            "role_denied": True}), 403))
    r = c.get(f"/api/admin/backup-archive/{name}")
    assert r.status_code == 403
    assert r.get_json()["role_denied"] is True
    assert b"lsmenc" not in r.data


def test_backup_archive_download_is_audited():
    action, target, event = M._audit_match("GET", "/api/admin/backup-archive/lsm-auto-manager-h-1.lsmenc")
    assert (action, event) == ("backup.download", "backup")
    assert target == "lsm-auto-manager-h-1.lsmenc"


def test_backup_archive_get_passes_the_audit_hook_gate(backup_env, monkeypatch):
    """Guards the _AUDIT_GET_PATHS admission, not just the regex table: GETs
    are otherwise dropped before _audit_match ever runs."""
    c, _ = backup_env
    M._run_scheduled_backup("", 3, "")
    name = c.get("/api/admin/backup-status").get_json()["backups"][0]["file"]
    rows = []
    monkeypatch.setattr(M, "_audit_record", rows.append)
    assert c.get(f"/api/admin/backup-archive/{name}").status_code == 200
    # entry = (ts, actor, role, ip, auth, method, path, action, target, ...)
    assert [(e[7], e[8]) for e in rows] == [("backup.download", name)]
    # A sibling admin GET stays unaudited — the prefix must not widen.
    rows.clear()
    c.get("/api/admin/backup-status")
    assert rows == []


# ── #855: status carries both components ─────────────────────────────

def _fake_ae_blob(passphrase):
    import _archive
    return _archive.encrypt(_archive.pack_tar({"manifest.json": b"{}"}), passphrase or None)


def test_backup_status_lists_the_component_per_archive(backup_env, monkeypatch):
    c, _ = backup_env
    monkeypatch.setattr(M, "_ae_backup_coverage", lambda: None)
    monkeypatch.setattr(M, "_fetch_ae_export_blob", _fake_ae_blob)
    M._run_scheduled_backup("", 3, "")
    d = c.get("/api/admin/backup-status").get_json()
    comps = sorted(b["component"] for b in d["backups"])
    assert comps == ["alarm_engine", "manager"]
    assert all(b["run"] for b in d["backups"])
    assert len({b["run"] for b in d["backups"]}) == 1
    assert d["last"]["components"]["alarm_engine"]["ok"] is True
    assert d["last"]["partial"] is False
    assert d["not_covered"] == {}


def test_backup_status_says_when_the_ae_is_not_covered(backup_env, monkeypatch):
    c, _ = backup_env
    monkeypatch.setattr(M, "_alarm_engine_url", "")
    d = c.get("/api/admin/backup-status").get_json()
    assert "alarm_engine_url" in d["not_covered"]["alarm_engine"]


def test_backup_now_reports_partial_runs(backup_env, monkeypatch):
    c, _ = backup_env
    monkeypatch.setattr(M, "_ae_backup_coverage", lambda: None)
    def fetch(passphrase):
        raise M._AeBackupError("unreachable", "no route", "check the network")
    monkeypatch.setattr(M, "_fetch_ae_export_blob", fetch)
    r = c.post("/api/admin/backup-now")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d["last"]["partial"] is True
    assert d["last"]["components"]["alarm_engine"]["ok"] is False
    assert d["last"]["components"]["alarm_engine"]["remedy"] == "check the network"
