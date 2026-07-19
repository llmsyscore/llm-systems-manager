"""#437: Homebrew-keg control-plane restart.

_detect_brew_keg() routes admin_service_restart onto the process-exit +
AE-self-restart path with a NON-ZERO exit — brew-services systemd units are
Restart=on-failure, so a clean exit would leave the service dead. Also covers
the brew AE keg signal that surfaces the admin-tab AE restart button.
"""
from __future__ import annotations

import manager_mod


def _admin_ok(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)


def test_override_true(monkeypatch):
    monkeypatch.setenv("LSM_BREW", "1")
    assert manager_mod._detect_brew_keg() is True


def test_override_false(monkeypatch):
    monkeypatch.setenv("LSM_BREW", "0")
    assert manager_mod._detect_brew_keg() is False


def test_detected_from_cellar_path(monkeypatch):
    monkeypatch.delenv("LSM_BREW", raising=False)
    monkeypatch.setattr(
        manager_mod.os.path, "realpath",
        lambda p: "/home/linuxbrew/.linuxbrew/Cellar/llm-systems-manager/1.0.8/libexec/m.py")
    assert manager_mod._detect_brew_keg() is True


def test_not_detected_on_plain_path(monkeypatch):
    monkeypatch.delenv("LSM_BREW", raising=False)
    monkeypatch.setattr(manager_mod.os.path, "realpath",
                        lambda p: "/opt/llm-systems-manager/backend/m.py")
    assert manager_mod._detect_brew_keg() is False


def test_brew_prefix_from_cellar_path(monkeypatch):
    monkeypatch.delenv("LSM_BREW_PREFIX", raising=False)
    monkeypatch.setattr(
        manager_mod.os.path, "realpath",
        lambda p: "/opt/homebrew/Cellar/llm-systems-manager/1.0.8/libexec/m.py")
    assert manager_mod._brew_prefix() == "/opt/homebrew"


def test_brew_prefix_env_override(monkeypatch):
    monkeypatch.setenv("LSM_BREW_PREFIX", "/tmp/fakebrew")
    assert manager_mod._brew_prefix() == "/tmp/fakebrew"


def test_brew_manager_restart_exits_nonzero(monkeypatch):
    _admin_ok(monkeypatch)
    monkeypatch.setattr(manager_mod, "_CONTAINERIZED", False)
    monkeypatch.setattr(manager_mod, "_BREW_KEG", True)
    captured = {}
    monkeypatch.setattr(manager_mod, "_schedule_process_exit",
                        lambda *a, **k: captured.update(k))
    with manager_mod.app.test_request_context():
        resp = manager_mod.admin_service_restart("manager")
    body = resp.get_json()
    assert body["ok"] is True and body["restarting"] is True
    assert captured.get("code") == 1


def test_brew_ae_restart_uses_management_api(monkeypatch):
    _admin_ok(monkeypatch)
    monkeypatch.setattr(manager_mod, "_CONTAINERIZED", False)
    monkeypatch.setattr(manager_mod, "_BREW_KEG", True)
    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://127.0.0.1:8081")

    def _no_sudo(*a, **k):
        raise AssertionError("sudoers path must not be used under brew")

    monkeypatch.setattr(manager_mod, "_sudo_allows", _no_sudo)
    posts = {}

    class _Resp:
        ok = True
        status_code = 200
        text = ""

    monkeypatch.setattr(manager_mod._ae_session, "post",
                        lambda url, **k: (posts.update(url=url) or _Resp()))
    with manager_mod.app.test_request_context():
        resp = manager_mod.admin_service_restart("alarm_engine")
    assert resp.get_json()["ok"] is True
    assert posts["url"].endswith("/api/alarm/admin/self-restart")


def test_bare_metal_still_preflights_sudoers(monkeypatch):
    _admin_ok(monkeypatch)
    monkeypatch.setattr(manager_mod, "_CONTAINERIZED", False)
    monkeypatch.setattr(manager_mod, "_BREW_KEG", False)
    monkeypatch.setattr(manager_mod, "_sudo_allows",
                        lambda cmd: (False, "not permitted by sudoers"))
    with manager_mod.app.test_request_context():
        resp, status = manager_mod.admin_service_restart("manager")
    assert status == 500
    assert resp.get_json()["ok"] is False


def test_topology_ae_local_via_brew_keg(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_mod, "_BREW_KEG", True)
    monkeypatch.setenv("LSM_BREW_PREFIX", str(tmp_path))
    monkeypatch.setattr(manager_mod.os.path, "isfile", lambda p: False)
    (tmp_path / "opt" / "llm-systems-alarm-engine").mkdir(parents=True)
    assert manager_mod.install_topology()["ae_local_unit"] is True


def test_topology_no_ae_keg_stays_remote(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_mod, "_BREW_KEG", True)
    monkeypatch.setenv("LSM_BREW_PREFIX", str(tmp_path))
    monkeypatch.setattr(manager_mod.os.path, "isfile", lambda p: False)
    assert manager_mod.install_topology()["ae_local_unit"] is False
