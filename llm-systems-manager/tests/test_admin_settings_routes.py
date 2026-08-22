"""GET/PUT /api/admin/settings (#606): masking, validation, write path, audit."""
from __future__ import annotations

import pytest

import manager_mod
import settings_toml_io as sio


@pytest.fixture
def client(monkeypatch, tmp_path):
    cfg = tmp_path / "llm-systems.toml"
    cfg.write_text("[manager]\nport = 5000\n")
    monkeypatch.setattr(sio, "resolve_config_path", lambda: cfg)
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    manager_mod._SETTINGS_RESTART_PENDING.clear()
    manager_mod.app.config["TESTING"] = True
    with manager_mod.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c, cfg


def test_get_masks_secrets_and_reports_topology(client):
    c, _ = client
    d = c.get("/api/admin/settings").get_json()
    assert d["ok"] is True
    assert "alarm_engine.ingest_token" not in d["values"]
    assert d["secrets"]["alarm_engine.ingest_token"] in ("set", "unset")
    assert set(d["topology"]) >= {"split", "ae_config_reachable"}
    assert d["restart_pending"] == []


def test_put_writes_toml_and_flags_restart(client):
    c, cfg = client
    d = c.put("/api/admin/settings",
              json={"changes": {"manager.history.window_minutes": 90}}).get_json()
    assert d["ok"] is True
    assert d["applied"] == ["manager.history.window_minutes"]
    assert d["restart_required"] == ["manager"]
    assert "window_minutes = 90" in cfg.read_text()
    d2 = c.get("/api/admin/settings").get_json()
    assert d2["restart_pending"] == ["manager"]


def test_put_rejects_bad_values_without_writing(client):
    c, cfg = client
    before = cfg.read_text()
    r = c.put("/api/admin/settings",
              json={"changes": {"manager.port": "nope", "manager.http_threads": 2}})
    d = r.get_json()
    assert r.status_code == 400 and d["ok"] is False
    assert set(d["errors"]) == {"manager.port", "manager.http_threads"}
    assert cfg.read_text() == before


def test_put_blank_secret_is_noop_null_clears(client):
    c, cfg = client
    d = c.put("/api/admin/settings",
              json={"changes": {"manager.backup.passphrase": ""}}).get_json()
    assert d["ok"] is True and d["applied"] == []
    d = c.put("/api/admin/settings",
              json={"changes": {"manager.backup.passphrase": None}}).get_json()
    assert d["applied"] == ["manager.backup.passphrase"]
    assert 'passphrase = ""' in cfg.read_text()


def test_put_secret_value_never_echoed(client):
    c, _ = client
    r = c.put("/api/admin/settings",
              json={"changes": {"manager.discord.bot_token": "hunter2token"}})
    assert b"hunter2token" not in r.data


def test_audit_route_registered():
    assert manager_mod._audit_match("PUT", "/api/admin/settings") == ("config.settings", None)


def test_admin_gate_enforced(monkeypatch, tmp_path):
    monkeypatch.setattr(sio, "resolve_config_path", lambda: tmp_path / "x.toml")
    manager_mod.app.config["TESTING"] = True
    with manager_mod.app.test_client() as c:
        r = c.put("/api/admin/settings", json={"changes": {}})
        assert r.status_code in (401, 403)


# --- split-install behaviour (#606, Task 5) ---

class _FakeResp:
    def __init__(self, ok=True, status_code=200, payload=None):
        self.ok, self.status_code = ok, status_code
        self._payload = payload or {"ok": True}
        self.text = ""

    def json(self):
        return self._payload


def _force_split(monkeypatch):
    monkeypatch.setattr(manager_mod, "install_topology", lambda: {
        "ae_local_disk": False, "ae_local_unit": False,
        "ae_local_url": False, "split": True})
    monkeypatch.setattr(manager_mod, "_CONTAINERIZED", False, raising=False)
    monkeypatch.setattr(manager_mod, "_BREW_KEG", False, raising=False)


def test_split_put_forwards_ae_paths(client, monkeypatch):
    c, cfg = client
    _force_split(monkeypatch)
    sent = {}
    monkeypatch.setattr(manager_mod._ae_session, "put",
                        lambda url, **kw: sent.update(url=url, **kw) or _FakeResp())
    d = c.put("/api/admin/settings", json={"changes": {
        "alarm_engine.evaluation_interval": 20,
        "influxdb.host": "10.0.0.9",
        "manager.poll_interval": 30}}).get_json()
    assert d["ok"] is True and "ae_sync_failed" not in d
    assert sent["url"].endswith("/api/alarm/admin/config")
    fwd = sent["json"]["changes"]
    assert set(fwd) == {"alarm_engine.evaluation_interval", "influxdb.host"}
    text = cfg.read_text()  # local file: manager + both paths, NOT ae-only
    assert "poll_interval = 30" in text and 'host = "10.0.0.9"' in text
    assert "evaluation_interval" not in text
    assert sorted(d["restart_required"]) == ["alarm_engine", "manager"]


def test_split_put_reports_ae_sync_failure_after_local_commit(client, monkeypatch):
    c, cfg = client
    _force_split(monkeypatch)

    def _boom(url, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr(manager_mod._ae_session, "put", _boom)
    d = c.put("/api/admin/settings",
              json={"changes": {"influxdb.host": "10.0.0.9"}}).get_json()
    assert d["ok"] is True and d["ae_sync_failed"]
    assert 'host = "10.0.0.9"' in cfg.read_text()


def test_split_get_merges_ae_values_and_reachability(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={"ok": True, "sections": {
                            "alarm_engine": {"evaluation_interval": 25,
                                             "ingest_token": "SEKRIT"}}}))
    d = c.get("/api/admin/settings").get_json()
    assert d["topology"]["ae_config_reachable"] is True
    assert d["values"]["alarm_engine.evaluation_interval"] == 25
    assert "alarm_engine.ingest_token" not in d["values"]  # masked
    assert d["secrets"]["alarm_engine.ingest_token"] == "set"


def test_split_get_unreachable_ae(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)

    def _boom(url, **kw):
        raise OSError("no route")
    monkeypatch.setattr(manager_mod._ae_session, "get", _boom)
    d = c.get("/api/admin/settings").get_json()
    assert d["topology"]["ae_config_reachable"] is False


def test_split_restart_uses_ae_self_restart(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)
    called = {}
    monkeypatch.setattr(manager_mod._ae_session, "post",
                        lambda url, **kw: called.update(url=url) or _FakeResp())
    r = c.post("/api/admin/service/alarm_engine/restart")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert called["url"].endswith("/api/alarm/admin/self-restart")
