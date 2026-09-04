"""#813: [manager.backup] is hot — reloaded on save, re-read by the scheduler,
mirror directory validated at save time."""
from __future__ import annotations

import pytest

import manager_mod as M
import settings_catalog
import settings_toml_io as sio


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    path = tmp_path / "llm-systems.toml"
    path.write_text("[manager]\nport = 5000\n")
    monkeypatch.setattr(sio, "resolve_config_path", lambda: path)
    monkeypatch.setattr(settings_catalog, "_BOOT_FILE_VALUES",
                        settings_catalog.file_catalog_values())
    live = M.settings.manager.backup
    for k in ("enabled", "interval_hours", "keep_last", "passphrase", "mirror_dir"):
        monkeypatch.setattr(live, k, getattr(live, k))
    return path


@pytest.fixture
def client(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    monkeypatch.setattr(M._ae_session, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    M._SETTINGS_RESTART_PENDING.clear()
    monkeypatch.setattr(M, "_SETTINGS_AE_PENDING_FILE", tmp_path / "ae_pending.json")
    M._clear_ae_pending_all()
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c, cfg


def test_backup_entries_are_hot():
    for p in ("manager.backup.enabled", "manager.backup.interval_hours",
              "manager.backup.keep_last", "manager.backup.passphrase",
              "manager.backup.mirror_dir"):
        assert settings_catalog.is_hot(p), p


def test_reload_applies_file_values_to_live_settings(cfg, tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    cfg.write_text(f'[manager.backup]\nmirror_dir = "{mirror}"\nkeep_last = 3\n'
                   'interval_hours = 2.5\nenabled = false\npassphrase = "correct horse battery"\n')
    M._backup_reload_config()
    enabled, interval_h, keep_last, mirror_dir = M._backup_cfg()
    assert (enabled, interval_h, keep_last, mirror_dir) == (False, 2.5, 3, str(mirror))
    assert M._backup_passphrase() == "correct horse battery"


def test_put_mirror_dir_is_live_without_restart(client, tmp_path):
    c, _ = client
    mirror = tmp_path / "nas" / "lsm"
    d = c.put("/api/admin/settings",
              json={"changes": {"manager.backup.mirror_dir": str(mirror)}}).get_json()
    assert d["ok"] is True, d
    assert d["restart_required"] == []
    assert mirror.is_dir()
    assert M._backup_cfg()[3] == str(mirror)
    assert c.get("/api/admin/settings").get_json()["restart_pending"] == []


def test_put_mirror_dir_rejects_unusable_paths(client, tmp_path):
    c, cfg = client
    before = cfg.read_text()
    f = tmp_path / "afile"
    f.write_text("x")
    for bad in ("relative/dir", str(f), str(f / "child")):
        r = c.put("/api/admin/settings", json={"changes": {"manager.backup.mirror_dir": bad}})
        d = r.get_json()
        assert r.status_code == 400 and "manager.backup.mirror_dir" in d["errors"], bad
    assert cfg.read_text() == before


def test_put_mirror_dir_rejects_unwritable_dir(client, tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    c, _ = client
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        r = c.put("/api/admin/settings", json={"changes": {"manager.backup.mirror_dir": str(ro)}})
        assert r.status_code == 400
        assert "writable" in r.get_json()["errors"]["manager.backup.mirror_dir"]
    finally:
        ro.chmod(0o700)


def test_put_blank_mirror_dir_clears(client):
    c, _ = client
    d = c.put("/api/admin/settings",
              json={"changes": {"manager.backup.mirror_dir": ""}}).get_json()
    assert d["ok"] is True
    assert M._backup_cfg()[3] == ""


def test_sched_eval_follows_live_settings(cfg):
    cfg.write_text("[manager.backup]\nenabled = false\n")
    M._backup_reload_config()
    ev = M._backup_sched_eval()
    assert ev["active"] is False and "disabled" in ev["reason"]
    cfg.write_text('[manager.backup]\nenabled = true\ninterval_hours = 1\npassphrase = "short"\n')
    M._backup_reload_config()
    ev = M._backup_sched_eval()
    assert ev["active"] is False and "12-char" in ev["reason"]
    cfg.write_text("[manager.backup]\nenabled = true\ninterval_hours = 1\n")
    M._backup_reload_config()
    ev = M._backup_sched_eval()
    assert ev["active"] is True and ev["interval_s"] == 3600.0


def test_due_ts_tracks_interval_and_failures():
    assert M._backup_due_ts(0.0, True, 3600.0, 1000.0) == 1000.0
    assert M._backup_due_ts(500.0, True, 7200.0, 1000.0) == 7700.0
    assert M._backup_due_ts(500.0, False, 7200.0, 1000.0) == 4100.0
    assert M._backup_due_ts(500.0, False, 600.0, 1000.0) == 1100.0


def test_backup_now_409_reason_follows_live_settings(client, monkeypatch, tmp_path):
    c, cfg = client
    monkeypatch.setattr(M, "_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(M, "_BACKUP_STATUS_FILE", tmp_path / "backups" / "last_backup.json")
    monkeypatch.setattr(M, "_alarm_engine_url", "")
    cfg.write_text("[manager.backup]\nenabled = false\n")
    M._backup_reload_config()
    r = c.post("/api/admin/backup-now")
    assert r.status_code == 409 and "disabled" in r.get_json()["error"]
    mirror = tmp_path / "m2"
    d = c.put("/api/admin/settings", json={"changes": {
        "manager.backup.enabled": True, "manager.backup.interval_hours": 1,
        "manager.backup.mirror_dir": str(mirror)}}).get_json()
    assert d["ok"] is True, d
    r = c.post("/api/admin/backup-now")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["last"]["mirrored"] is True
    assert len(list(mirror.glob("*.lsmenc"))) == 1


def test_hand_edit_is_picked_up_by_the_scheduler_eval(cfg, tmp_path):
    import os
    M._backup_reload_config()
    assert M._backup_cfg()[3] == ""
    mirror = tmp_path / "hand"
    mirror.mkdir()
    cfg.write_text(f'[manager.backup]\nmirror_dir = "{mirror}"\n')
    os.utime(cfg, (cfg.stat().st_atime, cfg.stat().st_mtime + 5))
    ev = M._backup_sched_eval()
    assert ev["mirror_dir"] == str(mirror)
