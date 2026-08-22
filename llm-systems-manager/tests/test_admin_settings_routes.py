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
