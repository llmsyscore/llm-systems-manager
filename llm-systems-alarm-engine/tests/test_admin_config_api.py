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


def test_put_removals_delete_whitelisted_keys(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    r = _client().put(PATH, headers={"Authorization": "Bearer mgmt-secret"},
                      json={"changes": {}, "removals": ["influxdb.host"]})
    assert r.status_code == 200
    assert r.json()["applied"] == ["influxdb.host"]
    assert "host" not in cfg.read_text()


def test_put_removals_respect_the_whitelist(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    before = cfg.read_text()
    for bad in (["manager.port"], ["alarm_engine"]):
        r = _client().put(PATH, headers={"Authorization": "Bearer mgmt-secret"},
                          json={"changes": {}, "removals": bad})
        assert r.status_code == 400, bad
    assert cfg.read_text() == before


def test_get_reports_restart_pending_from_boot_drift(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    monkeypatch.setattr(ae, "_BOOT_CONFIG_SECTIONS", ae._config_sections_snapshot())
    hdr = {"Authorization": "Bearer mgmt-secret"}
    assert _client().get(PATH, headers=hdr).json()["restart_pending"] is False
    _client().put(PATH, headers=hdr,
                  json={"changes": {"alarm_engine.evaluation_interval": 45}})
    assert _client().get(PATH, headers=hdr).json()["restart_pending"] is True


def test_restart_pending_false_when_boot_snapshot_unavailable(monkeypatch, cfg):
    _set_tokens(monkeypatch, management="mgmt-secret")
    monkeypatch.setattr(ae, "_BOOT_CONFIG_SECTIONS", None)
    r = _client().get(PATH, headers={"Authorization": "Bearer mgmt-secret"})
    assert r.json()["restart_pending"] is False


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


# --- secret masking (#708) ---

SECRETS_SAMPLE = '''[alarm_engine]
ingest_token = "ING-SECRET"
management_token = "mgmt-secret"

[influxdb]
host = "localhost"

[influxdb.tokens]
metrics = "INF-SECRET"
admin = ""

[notifications.smtp]
password = "SMTP-SECRET"

[notifications.discord]
webhook_url = "https://hooks.example/SECRET"
'''


@pytest.fixture
def secrets_cfg(monkeypatch, tmp_path):
    p = tmp_path / "llm-systems.toml"
    p.write_text(SECRETS_SAMPLE)
    monkeypatch.setattr(sio, "resolve_config_path", lambda: p)
    _set_tokens(monkeypatch, management="mgmt-secret")
    return p


HDR = {"Authorization": "Bearer mgmt-secret"}


def test_get_masks_secret_leaves(secrets_cfg):
    r = _client().get(PATH, headers=HDR)
    assert r.status_code == 200
    for leaked in ("ING-SECRET", "mgmt-secret", "INF-SECRET", "SMTP-SECRET",
                   "hooks.example/SECRET"):
        assert leaked.encode() not in r.content
    s = r.json()["sections"]
    assert s["alarm_engine"]["ingest_token"] == ae._AE_SECRET_MASK
    assert s["alarm_engine"]["management_token"] == ae._AE_SECRET_MASK
    assert s["influxdb"]["tokens"]["metrics"] == ae._AE_SECRET_MASK
    assert s["notifications"]["smtp"]["password"] == ae._AE_SECRET_MASK
    assert s["notifications"]["discord"]["webhook_url"] == ae._AE_SECRET_MASK
    assert s["influxdb"]["host"] == "localhost"  # non-secret untouched
    assert s["influxdb"]["tokens"]["admin"] == ""  # unset stays literal


def test_get_reports_secret_status_and_keyed_digest(secrets_cfg):
    import hmac as _hmac
    d = _client().get(PATH, headers=HDR).json()
    sec = d["secrets"]
    assert sec["alarm_engine.ingest_token"]["status"] == "set"
    assert sec["influxdb.tokens.admin"] == {"status": "unset", "digest": ""}
    expect = _hmac.new(b"mgmt-secret", b"ING-SECRET", "sha256").hexdigest()
    assert sec["alarm_engine.ingest_token"]["digest"] == expect
    assert "influxdb.host" not in sec


def test_boot_drift_compares_raw_not_masked(monkeypatch, secrets_cfg):
    monkeypatch.setattr(ae, "_BOOT_CONFIG_SECTIONS", ae._config_sections_snapshot())
    assert _client().get(PATH, headers=HDR).json()["restart_pending"] is False


def test_put_masked_secret_is_unchanged(secrets_cfg):
    before = secrets_cfg.read_text()
    r = _client().put(PATH, headers=HDR,
                      json={"changes": {"alarm_engine.ingest_token": ae._AE_SECRET_MASK}})
    assert r.status_code == 200 and r.json()["applied"] == []
    assert secrets_cfg.read_text() == before
    r = _client().put(PATH, headers=HDR,
                      json={"changes": {"alarm_engine.ingest_token": ae._AE_SECRET_MASK,
                                        "influxdb.host": "db9"}})
    assert r.json()["applied"] == ["influxdb.host"]
    assert 'ingest_token = "ING-SECRET"' in secrets_cfg.read_text()


def test_put_real_secret_value_still_writes(secrets_cfg):
    r = _client().put(PATH, headers=HDR,
                      json={"changes": {"alarm_engine.ingest_token": "NEW-ING"}})
    assert r.status_code == 200 and r.json()["applied"] == ["alarm_engine.ingest_token"]
    assert 'ingest_token = "NEW-ING"' in secrets_cfg.read_text()


def test_heuristic_masks_unlisted_secret_named_keys():
    masked, sec = ae._mask_config_sections({
        "notifications": {"pushover": {"api_token": "PO-SECRET", "retries": 3,
                                       "backup_tokens": ["A", "B"]},
                          "timeouts": {"token_ttl_s": 30}}}, "k")
    assert masked["notifications"]["pushover"]["api_token"] == ae._AE_SECRET_MASK
    assert masked["notifications"]["pushover"]["backup_tokens"] == ae._AE_SECRET_MASK
    assert masked["notifications"]["pushover"]["retries"] == 3
    assert masked["notifications"]["timeouts"]["token_ttl_s"] == 30  # int, not a secret
    assert set(sec) == {"notifications.pushover.api_token",
                        "notifications.pushover.backup_tokens"}


def test_put_masked_echo_still_hits_the_whitelist(secrets_cfg):
    before = secrets_cfg.read_text()
    r = _client().put(PATH, headers=HDR,
                      json={"changes": {"manager.secret_token": ae._AE_SECRET_MASK,
                                        "influxdb.host": "db9"}})
    assert r.status_code == 400
    assert secrets_cfg.read_text() == before
