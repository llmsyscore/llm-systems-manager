"""#412: containerized control-plane restart.

_detect_containerized() gates admin_service_restart onto the process-exit +
AE-self-restart path (containers have no systemd). Inputs are stubbed so the
result never depends on whether the CI runner itself is a container.
"""
from __future__ import annotations

import builtins
import io

import manager_mod


def _no_markers(monkeypatch):
    monkeypatch.delenv("LSM_CONTAINERIZED", raising=False)
    monkeypatch.setattr(manager_mod.os.path, "exists", lambda p: False)


def _fake_cgroup(monkeypatch, text):
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/1/cgroup":
            return io.StringIO(text)
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)


def test_override_true(monkeypatch):
    monkeypatch.setenv("LSM_CONTAINERIZED", "1")
    assert manager_mod._detect_containerized() is True


def test_override_false_wins_over_markers(monkeypatch):
    monkeypatch.setenv("LSM_CONTAINERIZED", "0")
    monkeypatch.setattr(manager_mod.os.path, "exists", lambda p: True)
    assert manager_mod._detect_containerized() is False


def test_dockerenv_marker(monkeypatch):
    monkeypatch.delenv("LSM_CONTAINERIZED", raising=False)
    monkeypatch.setattr(manager_mod.os.path, "exists", lambda p: p == "/.dockerenv")
    assert manager_mod._detect_containerized() is True


def test_container_cgroup(monkeypatch):
    _no_markers(monkeypatch)
    _fake_cgroup(monkeypatch, "0::/system.slice/docker-abc123.scope\n")
    assert manager_mod._detect_containerized() is True


def test_clean_host(monkeypatch):
    _no_markers(monkeypatch)
    _fake_cgroup(monkeypatch, "0::/init.scope\n")
    assert manager_mod._detect_containerized() is False


def test_restart_manager_exits_via_scheduler(monkeypatch):
    called = {}
    monkeypatch.setattr(manager_mod, "_schedule_process_exit",
                        lambda *a, **k: called.setdefault("exit", True))
    with manager_mod.app.test_request_context():
        resp = manager_mod._restart_service_containerized("manager")
    body = resp.get_json()
    assert body["ok"] is True and body["restarting"] is True
    assert called.get("exit") is True


def test_restart_ae_calls_self_endpoint(monkeypatch):
    posts = {}

    class _Resp:
        ok = True
        status_code = 200
        text = ""

    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://alarm-engine:8081")
    monkeypatch.setattr(manager_mod._ae_session, "post",
                        lambda url, **k: (posts.update(url=url) or _Resp()))
    with manager_mod.app.test_request_context():
        resp = manager_mod._restart_service_containerized("alarm_engine")
    assert resp.get_json()["ok"] is True
    assert posts["url"].endswith("/api/alarm/admin/self-restart")


def test_restart_ae_reports_upstream_failure(monkeypatch):
    class _Resp:
        ok = False
        status_code = 503
        text = "unavailable"

    monkeypatch.setattr(manager_mod, "_alarm_engine_url", "http://alarm-engine:8081")
    monkeypatch.setattr(manager_mod._ae_session, "post", lambda url, **k: _Resp())
    with manager_mod.app.test_request_context():
        resp, status = manager_mod._restart_service_containerized("alarm_engine")
    assert status == 502
    assert resp.get_json()["ok"] is False


# The restart rule must bind to admin_service_restart, not a helper defined
# just below the @app.route decorator — guards against a displaced decorator
# (which unit-testing the helper directly cannot catch).
def test_restart_route_bound_to_admin_service_restart():
    rules = [r for r in manager_mod.app.url_map.iter_rules()
             if str(r.rule) == "/api/admin/service/<svc>/restart"]
    assert rules, "restart route not registered"
    view = manager_mod.app.view_functions[rules[0].endpoint]
    assert view.__name__ == "admin_service_restart"
