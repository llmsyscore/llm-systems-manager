"""AE config management endpoints (#606): auth gate, whitelist, write path."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from config.unified_config import settings
from backend import alarm_engine as ae
from backend import settings_toml_io as sio

PATH = "/api/alarm/admin/config"

SAMPLE = '''[manager]
port = 5000

[alarm_engine]
evaluation_interval = 15

[influxdb]
host = "localhost"
'''


def _set_tokens(monkeypatch, ingest="", management=""):
    monkeypatch.setattr(settings.alarm_engine, "ingest_token", ingest, raising=False)
    monkeypatch.setattr(settings.alarm_engine, "management_token", management, raising=False)


def _client():
    return TestClient(ae.app, raise_server_exceptions=False)


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(SAMPLE)
    monkeypatch.setattr(sio, "resolve_config_path", lambda: p)
    return p


def test_get_requires_management_token(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    assert _client().get(PATH).status_code == 401
    r = _client().get(PATH, headers={"Authorization": "Bearer mgmt-secret"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_get_returns_only_whitelisted_sections(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    r = _client().get(PATH, headers={"Authorization": "Bearer mgmt-secret"})
    sections = r.json()["sections"]
    assert "manager" not in sections
    assert sections["alarm_engine"]["evaluation_interval"] == 15
    assert sections["influxdb"]["host"] == "localhost"


def test_put_rejects_non_whitelisted_paths(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    before = cfg.read_text()
    r = _client().put(PATH, headers={"Authorization": "Bearer mgmt-secret"},
                      json={"changes": {"manager.port": 1}})
    assert r.status_code == 400
    assert cfg.read_text() == before


def test_put_writes_whitelisted_change(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    r = _client().put(PATH, headers={"Authorization": "Bearer mgmt-secret"},
                      json={"changes": {"alarm_engine.evaluation_interval": 20}})
    assert r.status_code == 200 and r.json()["applied"] == ["alarm_engine.evaluation_interval"]
    assert "evaluation_interval = 20" in cfg.read_text()


def test_put_requires_changes(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    r = _client().put(PATH, headers={"Authorization": "Bearer mgmt-secret"},
                      json={"changes": {}})
    assert r.status_code == 400


def test_config_endpoints_fail_closed_without_tokens(monkeypatch, cfg):
    _set_tokens(monkeypatch, ingest="", management="")
    assert _client().get(PATH).status_code == 403
    r = _client().put(PATH, json={"changes": {"alarm_engine.evaluation_interval": 21}})
    assert r.status_code == 403
    assert "evaluation_interval = 21" not in cfg.read_text()


def test_put_null_secret_clear_yields_400_not_500(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    r = _client().put(PATH, headers={"Authorization": "Bearer mgmt-secret"},
                      json={"changes": {"alarm_engine.ingest_token": None}})
    assert r.status_code == 400


def test_config_endpoints_refuse_ingest_token_fallback(monkeypatch, cfg):
    _set_tokens(monkeypatch, ingest="ingest-secret", management="")
    hdr = {"Authorization": "Bearer ingest-secret"}
    assert _client().get(PATH, headers=hdr).status_code == 403
    r = _client().put(PATH, headers=hdr,
                      json={"changes": {"alarm_engine.evaluation_interval": 22}})
    assert r.status_code == 403
    assert "evaluation_interval = 22" not in cfg.read_text()
    # Self-restart keeps its documented ingest fallback (scheduler stubbed).
    monkeypatch.setattr(ae, "_schedule_ae_self_restart", lambda *a, **k: None)
    assert _client().post("/api/alarm/admin/self-restart", headers=hdr).status_code == 200
