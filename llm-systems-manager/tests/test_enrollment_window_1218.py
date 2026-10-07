"""#1218: new machines register only while enrollment is open — mode setting, start-up
window, manual open/extend/close, and the paths the window never touches."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import agent_registry
import manager_mod as M

FP = "sha256:" + "ab" * 32
VER = agent_registry.FP_REAUTH_FROM_VERSION


def _rec(aid, hostname="box", status="pending", registered_from="10.0.0.5", fp=FP):
    ts = datetime.now(timezone.utc).isoformat()
    return {"agent_id": aid, "hostname": hostname, "os": "linux", "role": "auto",
            "bind_url": "http://10.0.0.5:8765", "fingerprint": fp, "version": VER,
            "status": status, "token": "tok-" + aid if status == "approved" else None,
            "registered_from": registered_from, "capabilities": {},
            "first_seen": ts, "last_register": ts}


@pytest.fixture
def store(monkeypatch):
    data = {"agents": {}, "global": {}}
    saved = {}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: data)
    monkeypatch.setattr(agent_registry, "save_agents", lambda d: saved.update(d))
    monkeypatch.setattr(agent_registry, "_reg_rate", {})
    monkeypatch.setattr(agent_registry, "_enroll_boot_until", None)
    monkeypatch.setattr(agent_registry, "_enroll_boot_at", None)
    data["saved"] = saved
    return data


@pytest.fixture
def mode(monkeypatch):
    cfg = agent_registry._deps.settings.manager.agents

    def _set(value, window=15):
        monkeypatch.setattr(cfg, "enrollment_mode", value)
        monkeypatch.setattr(cfg, "enrollment_window_min", window)
    _set("auto")
    return _set


def _admin_client():
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def _register(c, remote="10.0.0.9", headers=None, **over):
    body = {"hostname": "box", "os": "linux", "bind_url": "http://10.0.0.9:8765",
            "fingerprint": "sha256:" + "cd" * 32, "version": VER}
    body.update(over)
    return c.post("/api/agents/register", json=body, headers=headers or {},
                  environ_base={"REMOTE_ADDR": remote})


class TestModes:
    def test_closed_mode_refuses_new_machines_without_writing(self, store, mode):
        mode("closed")
        with M.app.test_client() as c:
            r = _register(c)
        assert r.status_code == 503
        assert r.headers["Retry-After"] == "60"
        assert "Open enrollment" in r.get_json()["error"]
        assert store["agents"] == {} and store["saved"] == {}

    def test_open_mode_accepts(self, store, mode):
        mode("open")
        with M.app.test_client() as c:
            r = _register(c)
        assert r.status_code == 200 and r.get_json()["status"] == "pending"
        assert agent_registry.enrollment_state(store) == {
            "open": True, "until": None, "mode": "open", "window_min": 15, "source": "setting"}

    def test_auto_mode_is_open_for_one_window_after_start(self, store, mode):
        mode("auto", window=20)
        t0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        agent_registry.start_enrollment_boot_window(t0)
        st = agent_registry.enrollment_state(store, t0 + timedelta(minutes=19))
        assert st["open"] and st["source"] == "startup"
        assert st["until"] == (t0 + timedelta(minutes=20)).isoformat()
        assert not agent_registry.enrollment_state(store, t0 + timedelta(minutes=20, seconds=1))["open"]

    def test_auto_mode_start_up_window_lets_a_machine_register(self, store, mode):
        mode("auto")
        agent_registry.start_enrollment_boot_window()
        with M.app.test_client() as c:
            assert _register(c).status_code == 200

    def test_bad_setting_values_fall_back(self, store, mode):
        mode("sometimes", window="x")
        assert agent_registry._enroll_settings() == ("auto", 15)
        mode("open", window=0)
        assert agent_registry._enroll_settings() == ("open", 1)

    def test_other_modes_have_no_start_up_window(self, store, mode):
        mode("closed")
        agent_registry.start_enrollment_boot_window()
        assert agent_registry._enroll_boot_until is None


class TestUntouchedPaths:
    def test_loopback_source_still_auto_approves(self, store, mode):
        mode("closed")
        with M.app.test_client() as c:
            r = _register(c, remote="127.0.0.1", bind_url="http://127.0.0.1:8765")
        assert r.status_code == 200 and r.get_json()["status"] == "approved"

    def test_approved_agent_re_registers_with_its_token(self, store, mode):
        mode("closed")
        store["agents"]["a1"] = _rec("a1", status="approved")
        with M.app.test_client() as c:
            r = _register(c, headers={"Authorization": "Bearer tok-a1"})
        assert r.status_code == 200 and r.get_json()["agent_id"] == "a1"

    def test_waiting_row_from_the_same_address_is_returned(self, store, mode):
        mode("closed")
        store["agents"]["p1"] = _rec("p1", registered_from="10.0.0.9")
        with M.app.test_client() as c:
            r = _register(c, fingerprint="sha256:" + "ee" * 32)
        assert r.status_code == 200 and r.get_json()["agent_id"] == "p1"

    def test_stale_record_still_gets_403_not_503(self, store, mode):
        mode("closed")
        store["agents"]["a1"] = _rec("a1", status="approved")
        with M.app.test_client() as c:
            assert _register(c).status_code == 403


class TestAdminRoute:
    def test_open_then_extend_then_close(self, store, mode):
        mode("closed", window=15)
        with _admin_client() as c:
            r = c.post("/api/agents/enrollment", json={"action": "open"})
            st = r.get_json()["enrollment"]
            assert r.status_code == 200 and st["open"] and st["source"] == "manual"
            first_until = datetime.fromisoformat(st["until"])
            left = (first_until - datetime.now(timezone.utc)).total_seconds()
            assert 14 * 60 < left <= 15 * 60
            assert _register(c).status_code == 200

            st2 = c.post("/api/agents/enrollment", json={"action": "open"}).get_json()["enrollment"]
            assert datetime.fromisoformat(st2["until"]) == first_until + timedelta(minutes=15)

            st3 = c.post("/api/agents/enrollment", json={"action": "close"}).get_json()["enrollment"]
            assert st3 == {"open": False, "until": None, "mode": "closed", "window_min": 15, "source": "manual"}
            assert _register(c, remote="10.0.0.10", hostname="other").status_code == 503
        assert store["saved"]["global"]["enrollment"]["state"] == "closed"

    def test_close_overrides_open_mode_until_opened_again(self, store, mode):
        mode("open")
        with _admin_client() as c:
            c.post("/api/agents/enrollment", json={"action": "close"})
            assert _register(c).status_code == 503
            c.post("/api/agents/enrollment", json={"action": "open"})
            assert _register(c).status_code == 200

    def test_bad_action_is_400(self, store, mode):
        with _admin_client() as c:
            assert c.post("/api/agents/enrollment", json={"action": "later"}).status_code == 400
            assert c.post("/api/agents/enrollment", data=b"nope").status_code == 400

    def test_without_an_admin_session_the_route_is_denied(self, store, mode):
        with M.app.test_client() as c:
            assert c.post("/api/agents/enrollment", json={"action": "open"}).status_code in (401, 403)
        assert store["saved"] == {}


class TestOverrideLifetime:
    def test_expired_manual_window_falls_back_to_the_mode(self, store, mode):
        mode("closed")
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        store["global"]["enrollment"] = {"state": "open", "until": past, "mode": "closed"}
        assert not agent_registry.enrollment_state(store)["open"]

    def test_changing_the_mode_drops_the_override(self, store, mode):
        mode("open")
        store["global"]["enrollment"] = {"state": "closed", "until": None, "mode": "closed"}
        st = agent_registry.enrollment_state(store)
        assert st["open"] and st["source"] == "setting"

    def test_in_auto_mode_a_manual_close_ends_at_the_next_manager_start(self, store, mode):
        mode("auto")
        t0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        agent_registry.start_enrollment_boot_window(t0)
        with _admin_client() as c:
            c.post("/api/agents/enrollment", json={"action": "close"})
        assert not agent_registry.enrollment_state(store, t0 + timedelta(minutes=1))["open"]
        t1 = datetime.now(timezone.utc) + timedelta(hours=1)
        agent_registry.start_enrollment_boot_window(t1)
        st = agent_registry.enrollment_state(store, t1 + timedelta(minutes=1))
        assert st["open"] and st["source"] == "startup"

    def test_in_closed_mode_a_manual_open_survives_a_manager_start(self, store, mode):
        mode("closed")
        with _admin_client() as c:
            c.post("/api/agents/enrollment", json={"action": "open"})
        agent_registry.start_enrollment_boot_window(datetime.now(timezone.utc) + timedelta(hours=1))
        assert agent_registry.enrollment_state(store)["open"]

    def test_clear_override_drops_the_manual_state(self, store, mode):
        mode("open")
        store["global"]["enrollment"] = {"state": "closed", "until": None, "mode": "open",
                                         "set_at": datetime.now(timezone.utc).isoformat()}
        assert not agent_registry.enrollment_state(store)["open"]
        agent_registry.clear_enrollment_override()
        assert "enrollment" not in store["global"] and agent_registry.enrollment_state(store)["open"]

    def test_unreadable_until_is_ignored(self, store, mode):
        mode("closed")
        store["global"]["enrollment"] = {"state": "open", "until": "soon", "mode": "closed"}
        assert not agent_registry.enrollment_state(store)["open"]

    def test_agents_list_reports_the_state(self, store, mode):
        mode("open")
        with _admin_client() as c:
            r = c.get("/api/agents")
        assert r.status_code == 200
        assert r.get_json()["enrollment"]["open"] is True
