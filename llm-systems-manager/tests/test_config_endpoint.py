"""/api/config exposes the configured polling cadences and the auto-mode
reason so the settings drawer can explain the current interval (#817)."""
from __future__ import annotations

import pytest

import manager_mod as manager_mod  # noqa: E402  # loaded by conftest


@pytest.fixture(autouse=True)
def _restore_interval_state():
    saved = (manager_mod._current_interval, manager_mod._interval_reason, manager_mod._interval_override)
    yield
    with manager_mod._interval_lock:
        manager_mod._current_interval, manager_mod._interval_reason = saved[0], saved[1]
    with manager_mod._interval_override_lock:
        manager_mod._interval_override = saved[2]


def _client(monkeypatch):
    """Test client admitted as an admin session past the auth gate."""
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    manager_mod.app.config["TESTING"] = True
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def _cfg(client):
    r = client.get("/api/config")
    assert r.status_code == 200, r.data
    return r.get_json()


def test_config_reports_cadences_and_reason(monkeypatch):
    monkeypatch.setattr(manager_mod.settings.manager, "poll_interval", 60)
    monkeypatch.setattr(manager_mod.settings.manager, "fast_poll_interval", 15)
    with manager_mod._interval_override_lock:
        manager_mod._interval_override = None
    manager_mod.set_interval("", lms_active=False, llama_awake=False)
    c = _client(monkeypatch)
    d = _cfg(c)
    assert d["interval_mode"] == "auto"
    assert d["poll_interval_idle"] == 60
    assert d["poll_interval_active"] == 15
    assert d["poll_interval"] == 60
    assert d["interval_reason"] == "idle"

    manager_mod.set_interval("", lms_active=False, llama_awake=True)
    d = _cfg(c)
    assert d["poll_interval"] == 15
    assert d["interval_reason"] == "llama awake"


def test_manual_override_keeps_reason_but_reports_manual(monkeypatch):
    monkeypatch.setattr(manager_mod.settings.manager, "poll_interval", 60)
    monkeypatch.setattr(manager_mod.settings.manager, "fast_poll_interval", 15)
    c = _client(monkeypatch)
    r = c.post("/api/config/interval", json={"mode": "manual", "value": 7})
    assert r.status_code == 200
    try:
        d = _cfg(c)
        assert d["interval_mode"] == "manual"
        assert d["poll_interval"] == 7
        assert d["interval_override"] == 7
        assert d["poll_interval_idle"] == 60
    finally:
        c.post("/api/config/interval", json={"mode": "auto"})
    assert _cfg(c)["interval_mode"] == "auto"


def test_auto_mode_recomputes_reason(monkeypatch):
    monkeypatch.setattr(manager_mod.settings.manager, "poll_interval", 60)
    monkeypatch.setattr(manager_mod.settings.manager, "fast_poll_interval", 15)
    monkeypatch.setattr(manager_mod, "_lms_active", False)
    monkeypatch.setattr(manager_mod, "_llama_awake", False)
    c = _client(monkeypatch)
    with manager_mod._interval_lock:
        manager_mod._interval_reason = "llama awake"
    c.post("/api/config/interval", json={"mode": "manual", "value": 9})
    r = c.post("/api/config/interval", json={"mode": "auto"})
    assert r.status_code == 200
    d = _cfg(c)
    assert d["interval_mode"] == "auto"
    assert d["interval_reason"] == "idle"
    assert d["poll_interval"] == 60


def test_layout_get_migrates_retired_theme(monkeypatch, tmp_path):
    f = tmp_path / "layout.json"
    f.write_text('{"theme": "classic", "cols": 3}')
    monkeypatch.setattr(manager_mod, "LAYOUT_FILE", f)
    c = _client(monkeypatch)
    d = c.get("/api/layout").get_json()
    assert d["theme"] == "oled"
    assert d["cols"] == 3


def test_manual_interval_floor_is_the_agent_sample_cadence(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/api/config/interval", json={"mode": "manual", "value": 1})
    assert r.get_json()["value"] == 5
    r = c.post("/api/config/interval", json={"mode": "manual", "value": 900})
    assert r.get_json()["value"] == 300
    c.post("/api/config/interval", json={"mode": "auto"})
