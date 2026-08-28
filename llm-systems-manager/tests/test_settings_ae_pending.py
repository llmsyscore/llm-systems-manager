"""AE-forward retry queue (#667): failed forwards are queued, merged with
later edits, retried in the background, and surfaced on GET/PUT."""
from __future__ import annotations

import pytest

import manager_mod
from tests.test_admin_settings_routes import _force_split, client  # noqa: F401


class _FakeResp:
    ok = True
    status_code = 200


def _ae_unreachable(url, **kw):
    raise OSError("no route")


@pytest.fixture
def split_client(client, monkeypatch):  # noqa: F811
    _force_split(monkeypatch)
    return client


def test_failed_forward_is_queued_and_reported(split_client, monkeypatch):
    c, cfg = split_client
    monkeypatch.setattr(manager_mod._ae_session, "put", _ae_unreachable)
    d = c.put("/api/admin/settings", json={"changes": {
        "influxdb.host": "10.0.0.9", "alarm_engine.evaluation_interval": 20}}).get_json()
    assert d["ok"] is True and d["ae_sync_failed"]
    assert d["ae_sync_pending"] == ["alarm_engine.evaluation_interval", "influxdb.host"]
    assert 'host = "10.0.0.9"' in cfg.read_text()
    g = c.get("/api/admin/settings").get_json()
    assert g["ae_sync_pending"] == ["alarm_engine.evaluation_interval", "influxdb.host"]


def test_retry_flush_delivers_merged_queue_and_clears(split_client, monkeypatch):
    c, _ = split_client
    monkeypatch.setattr(manager_mod._ae_session, "put", _ae_unreachable)
    c.put("/api/admin/settings", json={"changes": {"influxdb.host": "10.0.0.9",
                                                    "alarm_engine.evaluation_interval": 20}})
    c.put("/api/admin/settings", json={"changes": {"influxdb.host": "10.0.0.10"}})
    manager_mod._queue_ae_pending({}, ["alarm_engine.evaluation_interval"])
    sent = {}
    monkeypatch.setattr(manager_mod._ae_session, "put",
                        lambda url, **kw: sent.update(kw) or _FakeResp())
    assert manager_mod._flush_ae_pending() is None
    assert sent["json"]["changes"] == {"influxdb.host": "10.0.0.10"}
    assert sent["json"]["removals"] == ["alarm_engine.evaluation_interval"]
    assert manager_mod._settings_ae_pending_paths() == []
    assert "alarm_engine" in manager_mod._SETTINGS_RESTART_PENDING
    assert c.get("/api/admin/settings").get_json()["ae_sync_pending"] == []


def test_next_successful_put_drains_earlier_failure(split_client, monkeypatch):
    c, _ = split_client
    monkeypatch.setattr(manager_mod._ae_session, "put", _ae_unreachable)
    c.put("/api/admin/settings", json={"changes": {"alarm_engine.evaluation_interval": 20}})
    sent = {}
    monkeypatch.setattr(manager_mod._ae_session, "put",
                        lambda url, **kw: sent.update(kw) or _FakeResp())
    d = c.put("/api/admin/settings", json={"changes": {"influxdb.host": "10.0.0.9"}}).get_json()
    assert "ae_sync_failed" not in d and d.get("ae_sync_pending", []) == []
    assert sent["json"]["changes"] == {"alarm_engine.evaluation_interval": 20, "influxdb.host": "10.0.0.9"}
    assert manager_mod._settings_ae_pending_paths() == []


def test_flush_keeps_edits_queued_during_a_retry(split_client, monkeypatch):
    c, _ = split_client
    monkeypatch.setattr(manager_mod._ae_session, "put", _ae_unreachable)
    c.put("/api/admin/settings", json={"changes": {"influxdb.host": "10.0.0.9"}})

    def _put_and_edit(url, **kw):
        manager_mod._queue_ae_pending({"influxdb.host": "10.0.0.11"}, [])
        return _FakeResp()
    monkeypatch.setattr(manager_mod._ae_session, "put", _put_and_edit)
    assert manager_mod._flush_ae_pending() is None
    assert manager_mod._settings_ae_pending_paths() == ["influxdb.host"]


def test_retry_loop_flushes_quietly_until_shutdown(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_mod, "_SETTINGS_AE_PENDING_FILE", tmp_path / "ae_pending.json")
    calls = []

    def _flush(quiet=False):
        calls.append(quiet)
        manager_mod._shutting_down = True
    monkeypatch.setattr(manager_mod, "_flush_ae_pending", _flush)
    monkeypatch.setattr(manager_mod, "_shutting_down", False)
    monkeypatch.setattr(manager_mod, "_SETTINGS_AE_RETRY_INTERVAL_S", 0.0)
    manager_mod._clear_ae_pending_all()
    manager_mod._queue_ae_pending({"influxdb.host": "x"}, [])
    manager_mod._settings_ae_retry_loop()
    assert calls == [True]
    manager_mod._clear_ae_pending_all()


def test_retry_loop_warns_while_stuck(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(manager_mod, "_SETTINGS_AE_PENDING_FILE", tmp_path / "ae_pending.json")
    monkeypatch.setattr(manager_mod, "_shutting_down", False)
    monkeypatch.setattr(manager_mod, "_SETTINGS_AE_RETRY_INTERVAL_S", 0.0)
    n = {"i": 0}

    def _flush(quiet=False):
        n["i"] += 1
        if n["i"] >= 3:
            manager_mod._shutting_down = True
        return "alarm engine unreachable"
    monkeypatch.setattr(manager_mod, "_flush_ae_pending", _flush)
    manager_mod._clear_ae_pending_all()
    manager_mod._queue_ae_pending({"influxdb.host": "x"}, [])
    manager_mod._settings_ae_retry_loop()
    stuck = [r for r in caplog.records if "still queued for the alarm engine" in r.message]
    assert len(stuck) == 1  # throttled: one warning across three failed attempts
    manager_mod._clear_ae_pending_all()


def test_rejected_batch_is_dropped_not_retried(split_client, monkeypatch, caplog):
    c, _ = split_client

    class _Rej:
        ok = False
        status_code = 400
    monkeypatch.setattr(manager_mod._ae_session, "put", lambda url, **kw: _Rej())
    d = c.put("/api/admin/settings", json={"changes": {"influxdb.host": "10.0.0.9"}}).get_json()
    assert d["ae_sync_failed"].startswith("alarm engine rejected")
    assert d["ae_sync_pending"] == []
    assert manager_mod._settings_ae_pending_paths() == []
    assert any("rejected queued edit" in r.message for r in caplog.records)


def test_manager_only_put_does_not_block_on_queue(split_client, monkeypatch):
    c, _ = split_client
    monkeypatch.setattr(manager_mod._ae_session, "put", _ae_unreachable)
    c.put("/api/admin/settings", json={"changes": {"alarm_engine.evaluation_interval": 20}})
    calls = []
    monkeypatch.setattr(manager_mod._ae_session, "put", lambda url, **kw: calls.append(url) or _FakeResp())
    d = c.put("/api/admin/settings", json={"changes": {"manager.poll_interval": 30}}).get_json()
    assert d["ok"] is True and calls == []
    assert manager_mod._settings_ae_pending_paths() == ["alarm_engine.evaluation_interval"]


def test_queue_survives_restart_via_file(split_client, monkeypatch):
    c, _ = split_client
    monkeypatch.setattr(manager_mod._ae_session, "put", _ae_unreachable)
    c.put("/api/admin/settings", json={"changes": {"alarm_engine.evaluation_interval": 20}})
    assert manager_mod._SETTINGS_AE_PENDING_FILE.exists()
    assert (manager_mod._SETTINGS_AE_PENDING_FILE.stat().st_mode & 0o777) == 0o600
    manager_mod._SETTINGS_AE_PENDING["sets"].clear()  # simulated process restart
    manager_mod._load_ae_pending()
    assert manager_mod._settings_ae_pending_paths() == ["alarm_engine.evaluation_interval"]
    manager_mod._clear_ae_pending_all()
    assert not manager_mod._SETTINGS_AE_PENDING_FILE.exists()


def test_set_beats_removal_in_one_call(split_client):
    manager_mod._queue_ae_pending({"influxdb.host": "10.0.0.9"}, ["influxdb.host"])
    assert manager_mod._SETTINGS_AE_PENDING == {"sets": {"influxdb.host": "10.0.0.9"}, "dels": []}


def test_ae_restart_flushes_queue_first_and_keeps_pending_when_it_cannot(split_client, monkeypatch):
    c, _ = split_client
    monkeypatch.setattr(manager_mod, "_CONTAINERIZED", True, raising=False)
    monkeypatch.setattr(manager_mod, "_restart_service_containerized",
                        lambda svc, **kw: manager_mod.jsonify({"ok": True, "restarting": True}))
    monkeypatch.setattr(manager_mod._ae_session, "put", _ae_unreachable)
    c.put("/api/admin/settings", json={"changes": {"alarm_engine.evaluation_interval": 20}})
    d = c.post("/api/admin/service/alarm_engine/restart").get_json()
    assert d["ok"] is True and d["ae_sync_pending"] == ["alarm_engine.evaluation_interval"]
    assert "still queued" in d["note"]
    assert "alarm_engine" in manager_mod._SETTINGS_RESTART_PENDING
    sent = {}
    monkeypatch.setattr(manager_mod._ae_session, "put",
                        lambda url, **kw: sent.update(kw) or _FakeResp())
    d = c.post("/api/admin/service/alarm_engine/restart").get_json()
    assert d["ok"] is True and "note" not in d and "ae_sync_pending" not in d
    assert sent["json"]["changes"] == {"alarm_engine.evaluation_interval": 20}
    assert "alarm_engine" not in manager_mod._SETTINGS_RESTART_PENDING


def test_rejected_key_is_isolated_and_the_rest_delivered(split_client, monkeypatch, caplog):
    c, _ = split_client
    monkeypatch.setattr(manager_mod._ae_session, "put", _ae_unreachable)
    c.put("/api/admin/settings", json={"changes": {"influxdb.host": "10.0.0.9",
                                                    "alarm_engine.evaluation_interval": 20}})
    delivered = []

    class _Rej:
        ok = False
        status_code = 400

    def _put(url, **kw):
        changes = kw["json"]["changes"]
        if "alarm_engine.evaluation_interval" in changes:
            return _Rej()
        delivered.append(dict(changes))
        return _FakeResp()
    monkeypatch.setattr(manager_mod._ae_session, "put", _put)
    err = manager_mod._flush_ae_pending()
    assert err and err.startswith("alarm engine rejected")
    assert delivered == [{"influxdb.host": "10.0.0.9"}]
    assert manager_mod._settings_ae_pending_paths() == []
    assert "alarm_engine" in manager_mod._SETTINGS_RESTART_PENDING
    assert any("dropped ['alarm_engine.evaluation_interval']" in r.message for r in caplog.records)


def test_get_exposes_retry_interval(split_client):
    c, _ = split_client
    assert c.get("/api/admin/settings").get_json()["ae_sync_retry_s"] == 30.0
