"""GET/PUT /api/admin/settings (#606): masking, validation, write path, audit."""
from __future__ import annotations

import pytest
import requests

import manager_mod
import settings_catalog
import settings_toml_io as sio


def _ae_unreachable(url, **kw):
    raise OSError("no route")


@pytest.fixture
def client(monkeypatch, tmp_path):
    cfg = tmp_path / "llm-systems.toml"
    cfg.write_text("[manager]\nport = 5000\n")
    monkeypatch.setattr(sio, "resolve_config_path", lambda: cfg)
    # Boot snapshot matches the sandbox file, so pending starts empty; the AE
    # probe fails by default (tests override _ae_session.get as needed).
    monkeypatch.setattr(settings_catalog, "_BOOT_FILE_VALUES",
                        settings_catalog.file_catalog_values())
    monkeypatch.setattr(manager_mod._ae_session, "get", _ae_unreachable)
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    manager_mod._SETTINGS_RESTART_PENDING.clear()
    monkeypatch.setattr(manager_mod, "_SETTINGS_AE_PENDING_FILE", tmp_path / "ae_pending.json")
    manager_mod._clear_ae_pending_all()
    manager_mod.app.config["TESTING"] = True
    with manager_mod.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c, cfg


def test_get_masks_secrets_and_reports_topology(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(manager_mod, "install_topology", lambda: {
        "ae_local_disk": True, "ae_local_unit": True,
        "ae_local_url": True, "split": False})
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
    # Both-owned secret: status comes from the LOCAL file (empty here), never
    # from the AE payload's raw value.
    assert d["secrets"]["alarm_engine.ingest_token"] == "unset"


def test_split_get_unreachable_ae(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)

    def _boom(url, **kw):
        raise OSError("no route")
    monkeypatch.setattr(manager_mod._ae_session, "get", _boom)
    d = c.get("/api/admin/settings").get_json()
    assert d["topology"]["ae_config_reachable"] is False
    err = d["topology"]["ae_config_error"]
    assert err["kind"] == "unreachable" and err["status"] is None
    # A fixed phrase, never exception-derived text (CodeQL py/stack-trace-exposure).
    assert err["detail"].startswith("the connection failed") and err["remedy"]
    assert "no route" not in err["detail"] and "OSError" not in err["detail"]
    assert "log_detail" not in err


@pytest.mark.parametrize("exc,phrase", [
    (requests.exceptions.SSLError("SENTINEL1"), "TLS handshake failed"),
    (requests.exceptions.ConnectTimeout("SENTINEL2"), "the request timed out"),
    (requests.exceptions.ConnectionError("SENTINEL3"),
     "the connection was refused"),
    (ValueError("SENTINEL4"), "the reply was not valid JSON"),
])
def test_split_get_transport_failures_use_fixed_phrases(client, monkeypatch, exc, phrase):
    c, _ = client
    _force_split(monkeypatch)

    def _boom(url, **kw):
        raise exc
    monkeypatch.setattr(manager_mod._ae_session, "get", _boom)
    err = c.get("/api/admin/settings").get_json()["topology"]["ae_config_error"]
    assert err["kind"] == "unreachable"
    assert err["detail"].startswith(phrase)
    assert str(exc) not in err["detail"]


@pytest.mark.parametrize("status,kind", [(401, "unauthorized"), (403, "unauthorized"),
                                         (404, "unsupported"), (500, "http")])
def test_split_get_reports_why_the_ae_config_api_failed(client, monkeypatch,
                                                        status, kind):
    """#761: a rejected call must name its cause, not claim 'unreachable'."""
    c, _ = client
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(ok=False, status_code=status))
    d = c.get("/api/admin/settings").get_json()
    assert d["topology"]["ae_config_reachable"] is False
    err = d["topology"]["ae_config_error"]
    assert err["kind"] == kind and err["status"] == status
    assert f"HTTP {status}" in err["detail"]
    if kind == "unauthorized":
        assert "management_token" in err["remedy"]


def test_split_get_reachable_ae_carries_no_error(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={"ok": True, "sections": {}}))
    d = c.get("/api/admin/settings").get_json()
    assert d["topology"]["ae_config_reachable"] is True
    assert "ae_config_error" not in d["topology"]


def test_split_restart_uses_ae_self_restart(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)
    called = {}
    monkeypatch.setattr(manager_mod._ae_session, "post",
                        lambda url, **kw: called.update(url=url) or _FakeResp())
    r = c.post("/api/admin/service/alarm_engine/restart")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert called["url"].endswith("/api/alarm/admin/self-restart")


def test_split_put_failed_forward_excludes_remote_only_from_applied(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)

    def _boom(url, **kw):
        raise OSError("down")
    monkeypatch.setattr(manager_mod._ae_session, "put", _boom)
    d = c.put("/api/admin/settings", json={"changes": {
        "alarm_engine.evaluation_interval": 20,
        "manager.poll_interval": 30}}).get_json()
    assert d["ok"] is True and d["ae_sync_failed"]
    assert d["applied"] == ["manager.poll_interval"]
    assert d["restart_required"] == ["manager"]


# --- derived pending-restart (#611) ---


def test_pending_clears_after_simulated_restart(client, monkeypatch):
    c, _ = client
    c.put("/api/admin/settings",
          json={"changes": {"manager.history.window_minutes": 90}})
    assert c.get("/api/admin/settings").get_json()["restart_pending"] == ["manager"]
    # A restart re-reads the file at boot: refresh the boot snapshot.
    monkeypatch.setattr(settings_catalog, "_BOOT_FILE_VALUES",
                        settings_catalog.file_catalog_values())
    manager_mod._SETTINGS_RESTART_PENDING.clear()
    assert c.get("/api/admin/settings").get_json()["restart_pending"] == []


def test_hand_edit_flags_manager_pending(client):
    c, cfg = client
    cfg.write_text("[manager]\nport = 5001\n")
    assert c.get("/api/admin/settings").get_json()["restart_pending"] == ["manager"]


def test_manager_pending_survives_inmemory_wipe(client):
    c, _ = client
    c.put("/api/admin/settings",
          json={"changes": {"manager.history.window_minutes": 90}})
    manager_mod._SETTINGS_RESTART_PENDING.clear()   # simulated manager restart amnesia
    assert c.get("/api/admin/settings").get_json()["restart_pending"] == ["manager"]


def test_ae_pending_comes_from_ae_endpoint(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={
                            "ok": True, "sections": {}, "restart_pending": True}))
    assert c.get("/api/admin/settings").get_json()["restart_pending"] == ["alarm_engine"]


def test_ae_pending_false_overrides_inmemory_flag(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)
    manager_mod._SETTINGS_RESTART_PENDING.add("alarm_engine")
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={
                            "ok": True, "sections": {}, "restart_pending": False}))
    assert c.get("/api/admin/settings").get_json()["restart_pending"] == []


def test_ae_pending_falls_back_to_inmemory_when_unreachable(client):
    c, _ = client
    manager_mod._SETTINGS_RESTART_PENDING.add("alarm_engine")
    assert c.get("/api/admin/settings").get_json()["restart_pending"] == ["alarm_engine"]


# --- shared-section drift + re-sync (#612) ---


def test_split_get_reports_drift_masking_secrets(client, monkeypatch):
    c, cfg = client
    cfg.write_text('[manager]\nport = 5000\n'
                   '[influxdb]\nhost = "local-influx"\n'
                   '[alarm_engine]\ningest_token = "LOCAL-SECRET"\n')
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={"ok": True, "sections": {
                            "influxdb": {"host": "ae-influx"},
                            "alarm_engine": {"ingest_token": "AE-SECRET"}}}))
    r = c.get("/api/admin/settings")
    d = r.get_json()
    drift = d["drift"]
    assert drift["influxdb.host"] == {"local": "local-influx", "ae": "ae-influx"}
    assert drift["alarm_engine.ingest_token"] == {
        "secret": True, "local": "set", "ae": "set"}
    assert b"LOCAL-SECRET" not in r.data and b"AE-SECRET" not in r.data


def _digest(value: str) -> str:
    return manager_mod._ae_secret_digest(value)


def test_split_drift_masked_value_without_secrets_map_compares_status(client, monkeypatch):
    c, cfg = client
    cfg.write_text('[manager]\nport = 5000\n'
                   '[alarm_engine]\ningest_token = "LOCAL-SECRET"\n')
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={
                            "ok": True,
                            "sections": {"alarm_engine": {"ingest_token": "********"}}}))
    assert c.get("/api/admin/settings").get_json()["drift"] == {}


def test_split_get_ae_masked_non_catalog_secret_never_lands_in_values(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)
    ae_only = next(e["path"] for e in settings_catalog.CATALOG
                   if e["service"] == "alarm_engine" and e["type"] == "str")
    sect, key = ae_only.rsplit(".", 1)
    node: dict = {}
    cur = node
    for part in sect.split("."):
        cur = cur.setdefault(part, {})
    cur[key] = "********"
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={
                            "ok": True, "sections": node,
                            "secrets": {ae_only: {"status": "set", "digest": "x"}}}))
    d = c.get("/api/admin/settings").get_json()
    assert ae_only not in d["values"]
    assert d["secrets"][ae_only] == "set"


def test_ae_secret_path_list_matches_catalog():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "llm-systems-alarm-engine"
           / "backend" / "alarm_engine.py").read_text()
    m = re.search(r"_AE_SECRET_PATHS = frozenset\(\{(.*?)\}\)", src, re.S)
    ae_paths = set(re.findall(r'"([^"]+)"', m.group(1)))
    catalog = {e["path"] for e in settings_catalog.CATALOG
               if e["secret"] and e["service"] in ("alarm_engine", "both")}
    assert ae_paths == catalog, "update _AE_SECRET_PATHS in alarm_engine.py (#708)"


def test_split_drift_secret_compares_masked_digest(client, monkeypatch):
    c, cfg = client
    cfg.write_text('[manager]\nport = 5000\n'
                   '[alarm_engine]\ningest_token = "LOCAL-SECRET"\n')
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod, "_AE_BEARER", "mgmt-key")
    payload = {"ok": True,
               "sections": {"alarm_engine": {"ingest_token": "********"}},
               "secrets": {"alarm_engine.ingest_token": {
                   "status": "set", "digest": _digest("LOCAL-SECRET")}}}
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload=payload))
    assert c.get("/api/admin/settings").get_json()["drift"] == {}
    payload["secrets"]["alarm_engine.ingest_token"]["digest"] = _digest("AE-OTHER")
    r = c.get("/api/admin/settings")
    assert r.get_json()["drift"]["alarm_engine.ingest_token"] == {
        "secret": True, "local": "set", "ae": "set"}
    assert b"LOCAL-SECRET" not in r.data and b"AE-OTHER" not in r.data


def test_split_drift_secret_status_mismatch(client, monkeypatch):
    c, cfg = client
    cfg.write_text('[manager]\nport = 5000\n'
                   '[alarm_engine]\ningest_token = "LOCAL-SECRET"\n')
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod, "_AE_BEARER", "mgmt-key")
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={
                            "ok": True, "sections": {"alarm_engine": {"ingest_token": ""}},
                            "secrets": {"alarm_engine.ingest_token": {
                                "status": "unset", "digest": ""}}}))
    d = c.get("/api/admin/settings").get_json()
    assert d["drift"]["alarm_engine.ingest_token"] == {
        "secret": True, "local": "set", "ae": "unset"}


def test_split_get_no_drift_when_copies_match(client, monkeypatch):
    c, cfg = client
    cfg.write_text('[manager]\nport = 5000\n[influxdb]\nhost = "same"\n')
    _force_split(monkeypatch)
    monkeypatch.setattr(manager_mod._ae_session, "get",
                        lambda url, **kw: _FakeResp(payload={"ok": True, "sections": {
                            "influxdb": {"host": "same"}}}))
    assert c.get("/api/admin/settings").get_json()["drift"] == {}


def test_resync_forwards_local_values_including_secrets(client, monkeypatch):
    c, cfg = client
    cfg.write_text('[manager]\nport = 5000\n'
                   '[influxdb]\nhost = "local-influx"\n'
                   '[alarm_engine]\ningest_token = "LOCAL-SECRET"\n')
    _force_split(monkeypatch)
    sent = {}
    monkeypatch.setattr(manager_mod._ae_session, "put",
                        lambda url, **kw: sent.update(url=url, **kw) or _FakeResp())
    d = c.put("/api/admin/settings", json={
        "resync_ae": ["influxdb.host", "alarm_engine.ingest_token"]}).get_json()
    assert d["ok"] is True
    assert sorted(d["resynced"]) == ["alarm_engine.ingest_token", "influxdb.host"]
    assert d["restart_required"] == ["alarm_engine"]
    fwd = sent["json"]["changes"]
    assert fwd == {"influxdb.host": "local-influx",
                   "alarm_engine.ingest_token": "LOCAL-SECRET"}


def test_resync_rejects_non_shared_paths(client, monkeypatch):
    c, _ = client
    _force_split(monkeypatch)
    r = c.put("/api/admin/settings", json={"resync_ae": ["manager.port"]})
    assert r.status_code == 400
    assert "manager.port" in r.get_json()["errors"]


def test_bad_resync_path_blocks_the_whole_write(client, monkeypatch):
    c, cfg = client
    _force_split(monkeypatch)
    before = cfg.read_text()
    r = c.put("/api/admin/settings", json={
        "changes": {"manager.poll_interval": 30},
        "resync_ae": ["manager.poll_interval"]})
    assert r.status_code == 400
    assert cfg.read_text() == before


def test_resync_of_locally_unset_path_becomes_ae_removal(client, monkeypatch):
    c, cfg = client
    cfg.write_text('[manager]\nport = 5000\n')   # influxdb.host unset locally
    _force_split(monkeypatch)
    sent = {}
    monkeypatch.setattr(manager_mod._ae_session, "put",
                        lambda url, **kw: sent.update(url=url, **kw) or _FakeResp())
    d = c.put("/api/admin/settings",
              json={"resync_ae": ["influxdb.host"]}).get_json()
    assert d["ok"] is True and d["resynced"] == ["influxdb.host"]
    assert sent["json"]["changes"] == {}
    assert sent["json"]["removals"] == ["influxdb.host"]


def test_hand_edited_both_path_flags_ae_pending_when_ae_unreachable(client):
    c, cfg = client
    cfg.write_text('[manager]\nport = 5000\n[influxdb]\nhost = "moved"\n')
    pending = c.get("/api/admin/settings").get_json()["restart_pending"]
    assert pending == ["alarm_engine", "manager"]


# --- nullable clearing (#613) ---


def test_put_null_clears_nullable_field(client):
    c, cfg = client
    d = c.put("/api/admin/settings",
              json={"changes": {"manager.energy.price_kwh": 0.25}}).get_json()
    assert d["ok"] is True
    assert "price_kwh = 0.25" in cfg.read_text()
    d = c.put("/api/admin/settings",
              json={"changes": {"manager.energy.price_kwh": None}}).get_json()
    assert d["ok"] is True and d["applied"] == ["manager.energy.price_kwh"]
    assert "price_kwh" not in cfg.read_text()
    g = c.get("/api/admin/settings").get_json()
    assert "manager.energy.price_kwh" not in g["values"]


def test_put_null_still_rejected_for_non_nullable(client):
    c, _ = client
    r = c.put("/api/admin/settings",
              json={"changes": {"manager.poll_interval": None}})
    assert r.status_code == 400
    assert "manager.poll_interval" in r.get_json()["errors"]
