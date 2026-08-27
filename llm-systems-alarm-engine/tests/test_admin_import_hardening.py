"""AE admin hardening (#660–#663): uvloop fallback, DB stats(), dbstats
cache invalidation on import, pre-import backup pruning."""
from __future__ import annotations

import importlib.util
import json
import os

from fastapi.testclient import TestClient

from config.unified_config import settings
from backend import alarm_engine as ae
from backend import _archive
from backend.storage.ae_alarms_db import AeAlarmsDB
from backend.storage.ae_settings_db import AeSettingsDB


def _seed_baks(dest, n):
    for i in range(n):
        p = f"{dest}.preimport.20260101-0000{i:02d}.bak"
        with open(p, "wb") as f:
            f.write(b"old")
    return sorted(os.listdir(os.path.dirname(dest)))


def test_prune_keeps_newest_n(tmp_path):
    dest = str(tmp_path / "x.db")
    with open(dest, "wb") as f:
        f.write(b"live")
    _seed_baks(dest, 7)
    (tmp_path / "x.db.20260101-000000.tmp").write_bytes(b"unrelated")
    removed = _archive.prune_preimport_backups(dest, keep=5)
    assert len(removed) == 2
    assert all(r.endswith(("000000.bak", "000001.bak")) for r in removed)
    left = [n for n in os.listdir(tmp_path) if ".preimport." in n]
    assert len(left) == 5
    assert (tmp_path / "x.db").read_bytes() == b"live"
    assert (tmp_path / "x.db.20260101-000000.tmp").exists()


def test_prune_ignores_other_dests(tmp_path):
    a, b = str(tmp_path / "a.db"), str(tmp_path / "a.db.other")
    _seed_baks(a, 3)
    _seed_baks(b, 3)
    assert _archive.prune_preimport_backups(a, keep=1)
    assert len([n for n in os.listdir(tmp_path) if n.startswith("a.db.other.preimport")]) == 3


def test_db_stats_public_api(tmp_path):
    alarms = AeAlarmsDB.open(tmp_path / "ae_alarms.db")
    st = alarms.stats()
    assert st["path"].endswith("ae_alarms.db")
    assert st["size_bytes"] > 0 and st["page_count"] >= 1
    assert st["alerts"] == 0 and st["alert_history"] == 0
    alarms.close()
    rules = AeSettingsDB.open(tmp_path / "ae_notif_rules.db")
    st = rules.stats()
    assert {"rules", "channels", "configs", "deliveries"} <= set(st)
    rules.close()


def test_collect_stats_hides_paths_and_flags_stale(tmp_path, monkeypatch):
    alarms = AeAlarmsDB.open(tmp_path / "ae_alarms.db")
    monkeypatch.setattr(ae, "ae_alarms_db", alarms)
    monkeypatch.setattr(ae, "ae_settings_db", None)
    monkeypatch.setattr(ae, "_SQLITE_STATS_STALE_UNTIL_RESTART", False)
    payload = ae._collect_sqlite_stats()
    assert payload["alarms_db"]["db"] == "ae_alarms.db"
    assert "path" not in payload["alarms_db"]
    assert "stale_until_restart" not in payload
    ae._sqlite_stats_invalidate(stale_until_restart=True)
    assert ae._collect_sqlite_stats()["stale_until_restart"] is True
    alarms.close()


def _archive_blob(files: dict) -> bytes:
    files = dict(files)
    files["manifest.json"] = json.dumps({"component": "alarm_engine"}).encode()
    return _archive.encrypt(_archive.pack_tar(files), None)


def test_import_apply_prunes_backups_and_invalidates_cache(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "llm-systems.toml"
    cfg.parent.mkdir()
    cfg.write_text("[manager]\nport = 5000\n")
    _seed_baks(str(cfg), 6)
    monkeypatch.setattr(ae, "_ae_config_path", lambda: cfg)
    monkeypatch.setattr(ae, "_ae_data_root", lambda: tmp_path)
    monkeypatch.setattr(ae, "_SQLITE_STATS_STALE_UNTIL_RESTART", False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", "", raising=False)
    ae._SQLITE_STATS_CACHE.update({"at": 9e12, "payload": {"alarms_db": {"size_bytes": 1}}})
    blob = _archive_blob({"config/llm-systems.toml": b"[manager]\nport = 5001\n"})
    client = TestClient(ae.app, raise_server_exceptions=False)
    r = client.post("/api/alarm/admin/import/apply",
                    files={"file": ("x.lsmenc", blob, "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and len(r.json()["backups"]) == 1
    assert cfg.read_text() == "[manager]\nport = 5001\n"
    baks = [n for n in os.listdir(cfg.parent) if ".preimport." in n]
    assert len(baks) == ae._AE_PREIMPORT_KEEP
    assert os.path.basename(r.json()["backups"][0]) in baks
    assert ae._SQLITE_STATS_CACHE["at"] == 0.0 and ae._SQLITE_STATS_CACHE["payload"] == {}
    assert ae._SQLITE_STATS_STALE_UNTIL_RESTART is True


def test_uvicorn_loop_falls_back_without_uvloop(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "uvloop" else real(name, *a))
    assert ae._uvicorn_loop() == "asyncio"
    monkeypatch.setattr(importlib.util, "find_spec", real)
    assert ae._uvicorn_loop() == ("uvloop" if real("uvloop") else "asyncio")
