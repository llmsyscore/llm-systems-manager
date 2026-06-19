from __future__ import annotations
import os
import pytest
import manager_users


@pytest.fixture
def store(tmp_path):
    return manager_users.UserStore(tmp_path / "manager_users.json")


class TestUserStoreCrud:
    def test_create_and_get(self, store):
        store.create("alice", "scrypt$x$y", "operator")
        u = store.get("alice")
        assert u["role"] == "operator"
        assert u["disabled"] is False
        assert u["password_hash"] == "scrypt$x$y"

    def test_username_is_normalized_lowercase(self, store):
        store.create("Alice", "h", "admin")
        assert store.get("alice") is not None
        assert store.get("Alice") is not None  # get normalizes too

    def test_create_rejects_bad_username(self, store):
        with pytest.raises(ValueError):
            store.create("has space", "h", "admin")

    def test_create_rejects_bad_role(self, store):
        with pytest.raises(ValueError):
            store.create("bob", "h", "superuser")

    def test_create_duplicate_raises(self, store):
        store.create("bob", "h", "admin")
        with pytest.raises(ValueError):
            store.create("bob", "h2", "operator")

    def test_list_omits_password_hash(self, store):
        store.create("bob", "secret-hash", "admin")
        rows = store.list()
        assert rows and all("password_hash" not in r for r in rows)
        assert rows[0]["username"] == "bob"

    def test_set_password_updates_hash(self, store):
        store.create("bob", "old", "admin")
        store.set_password("bob", "new")
        assert store.get("bob")["password_hash"] == "new"

    def test_set_role_and_disabled(self, store):
        store.create("a", "h", "admin")
        store.create("b", "h", "operator")
        store.set_role("b", "admin")
        assert store.get("b")["role"] == "admin"
        store.set_disabled("b", True)
        assert store.get("b")["disabled"] is True

    def test_delete(self, store):
        store.create("a", "h", "admin")
        store.create("b", "h", "operator")
        store.delete("b")
        assert store.get("b") is None

    def test_stamp_login_sets_last_login(self, store):
        store.create("a", "h", "admin")
        store.stamp_login("a")
        assert store.get("a")["last_login"] is not None


class TestLastAdminInvariant:
    def _two(self, store):
        store.create("admin1", "h", "admin")
        store.create("op1", "h", "operator")
        return store

    def test_cannot_delete_last_enabled_admin(self, store):
        self._two(store)
        with pytest.raises(ValueError):
            store.delete("admin1")

    def test_cannot_demote_last_enabled_admin(self, store):
        self._two(store)
        with pytest.raises(ValueError):
            store.set_role("admin1", "operator")

    def test_cannot_disable_last_enabled_admin(self, store):
        self._two(store)
        with pytest.raises(ValueError):
            store.set_disabled("admin1", True)

    def test_second_admin_allows_demoting_first(self, store):
        self._two(store)
        store.create("admin2", "h", "admin")
        store.set_role("admin1", "operator")  # admin2 still admin → allowed
        assert store.get("admin1")["role"] == "operator"

    def test_disabled_admin_does_not_count(self, store):
        store.create("admin1", "h", "admin")
        store.create("admin2", "h", "admin")
        store.set_disabled("admin2", True)
        with pytest.raises(ValueError):
            store.delete("admin1")  # admin2 disabled → admin1 is the last ENABLED admin


class TestSeedAndPersistence:
    def test_seed_admin_populates_empty_store(self, store):
        assert store.is_empty()
        store.seed_admin("llmadmin", "scrypt$d$h")
        assert not store.is_empty()
        assert store.get("llmadmin")["role"] == "admin"

    def test_seed_admin_noop_when_not_empty(self, store):
        store.create("bob", "h", "admin")
        store.seed_admin("llmadmin", "x")  # store not empty → no change
        assert store.get("llmadmin") is None

    def test_persists_0600_across_instances(self, tmp_path):
        p = tmp_path / "manager_users.json"
        manager_users.UserStore(p).create("a", "h", "admin")
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"
        assert manager_users.UserStore(p).get("a")["role"] == "admin"

    def test_count_enabled_admins(self, store):
        store.create("a", "h", "admin")
        store.create("b", "h", "admin")
        store.create("c", "h", "operator")
        store.set_disabled("b", True)
        assert store.count_enabled_admins() == 1


class TestLockoutTracker:
    def _tr(self, **kw):
        # Injectable clock so tests don't sleep.
        clk = {"t": 1000.0}
        kw.setdefault("threshold", 3)
        kw.setdefault("window_s", 60)
        kw.setdefault("duration_s", 120)
        tr = manager_users.LockoutTracker(clock=lambda: clk["t"], **kw)
        return tr, clk

    def test_not_locked_initially(self):
        tr, _ = self._tr()
        assert tr.is_locked("alice") is False

    def test_locks_after_threshold(self):
        tr, _ = self._tr()
        for _ in range(3):
            tr.record_failure("alice")
        assert tr.is_locked("alice") is True

    def test_below_threshold_not_locked(self):
        tr, _ = self._tr()
        tr.record_failure("alice")
        tr.record_failure("alice")
        assert tr.is_locked("alice") is False

    def test_failures_outside_window_dont_count(self):
        tr, clk = self._tr()
        tr.record_failure("alice")
        tr.record_failure("alice")
        clk["t"] += 61  # window is 60s → earlier fails expire
        tr.record_failure("alice")
        assert tr.is_locked("alice") is False

    def test_lock_expires_after_duration(self):
        tr, clk = self._tr()
        for _ in range(3):
            tr.record_failure("alice")
        assert tr.is_locked("alice") is True
        clk["t"] += 121  # duration is 120s
        assert tr.is_locked("alice") is False

    def test_clear_resets(self):
        tr, _ = self._tr()
        for _ in range(3):
            tr.record_failure("alice")
        tr.clear("alice")
        assert tr.is_locked("alice") is False

    def test_keys_are_independent(self):
        tr, _ = self._tr()
        for _ in range(3):
            tr.record_failure("alice")
        assert tr.is_locked("bob") is False

    def test_retry_after_is_positive_when_locked(self):
        tr, _ = self._tr()
        for _ in range(3):
            tr.record_failure("alice")
        assert tr.retry_after("alice") > 0
        assert tr.retry_after("bob") == 0


class TestConfigDefaults:
    def test_manager_auth_has_role_and_lockout_defaults(self):
        from config.unified_config import ManagerAuth
        a = ManagerAuth()
        assert a.bypass_role == "admin"
        assert a.lockout_threshold == 5
        assert a.lockout_window_s == 900
        assert a.lockout_duration_s == 900


class TestAuthenticate:
    @pytest.fixture
    def wired(self, tmp_path):
        import auth
        manager_users.init(
            tmp_path / "manager_users.json",
            threshold=3, window_s=60, duration_s=120,
        )
        manager_users.STORE.create("alice", auth.scrypt_hash("pw-alice"), "operator")
        manager_users.STORE.create("root", auth.scrypt_hash("pw-root"), "admin")
        manager_users.LOCKOUT.clear("user:alice")
        manager_users.LOCKOUT.clear("ip:1.2.3.4")
        return manager_users

    def test_success_returns_role(self, wired):
        r = wired.authenticate("alice", "pw-alice", "1.2.3.4")
        assert r["ok"] is True and r["role"] == "operator" and r["username"] == "alice"

    def test_wrong_password_fails(self, wired):
        r = wired.authenticate("alice", "nope", "1.2.3.4")
        assert r["ok"] is False and r.get("locked") is not True

    def test_unknown_user_is_generic_failure(self, wired):
        r = wired.authenticate("ghost", "x", "1.2.3.4")
        assert r["ok"] is False

    def test_disabled_user_cannot_login(self, wired):
        wired.STORE.set_disabled("alice", True)
        r = wired.authenticate("alice", "pw-alice", "1.2.3.4")
        assert r["ok"] is False

    def test_lock_after_threshold_then_blocks_even_correct_pw(self, wired):
        for _ in range(3):
            wired.authenticate("alice", "wrong", "9.9.9.9")
        r = wired.authenticate("alice", "pw-alice", "9.9.9.9")
        assert r["ok"] is False and r["locked"] is True

    def test_success_clears_failures(self, wired):
        wired.authenticate("alice", "wrong", "5.5.5.5")
        wired.authenticate("alice", "pw-alice", "5.5.5.5")  # success clears
        # one more wrong should not be at threshold yet
        wired.authenticate("alice", "wrong", "5.5.5.5")
        assert wired.LOCKOUT.is_locked("user:alice") is False


class TestOperatorDenyMatcher:
    @pytest.mark.parametrize("path", [
        "/api/admin/users", "/api/admin/system-health", "/api/admin/export/manager",
        "/api/agents", "/api/agents/abc/approve", "/api/agents/metrics",
        "/api/terminal/create", "/api/lms/terminal/create",
        "/api/admin/push-ca-to-agents",
    ])
    def test_denied_paths(self, path):
        import auth
        assert auth._operator_denied(path) is True

    @pytest.mark.parametrize("path", [
        "/api/me", "/api/account/password", "/api/agents/list-by-provider",
        "/api/metrics", "/api/llm/models", "/api/lmstudio/metrics",
        "/api/alarm/alerts/", "/api/history",
    ])
    def test_allowed_paths(self, path):
        import auth
        assert auth._operator_denied(path) is False


class TestRequireAdminRole:
    # Unit-test _require_admin directly in a request context — no registered
    # route needed (the /api/admin/users route lands in Task 7). _require_admin
    # returns None to admit, or a (jsonify, 403) tuple to deny.
    def test_operator_role_denied(self, monkeypatch):
        import manager_mod as M
        from flask import g
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: True, raising=False)
        with M.app.test_request_context("/api/admin/x"):
            g.auth_role = "operator"
            deny = M._require_admin()
        assert deny is not None and deny[1] == 403
        assert deny[0].get_json().get("role_denied") is True

    def test_admin_role_with_ip_allowed_passes(self, monkeypatch):
        import manager_mod as M
        from flask import g
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: True, raising=False)
        with M.app.test_request_context("/api/admin/x"):
            g.auth_role = "admin"
            assert M._require_admin() is None  # role + IP both pass → admit

    def test_admin_role_but_ip_denied(self, monkeypatch):
        import manager_mod as M
        from flask import g
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: False, raising=False)
        with M.app.test_request_context("/api/admin/x"):
            g.auth_role = "admin"
            deny = M._require_admin()
        assert deny is not None and deny[1] == 403  # IP gate still applies

    def test_no_role_no_session_fails_closed(self, monkeypatch):
        # No g.auth_role and no session (an agent/anon context the gate exempted)
        # must be DENIED even on an allowed admin IP — fail closed, not open.
        import manager_mod as M
        import auth
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: True, raising=False)
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        with M.app.test_request_context("/api/admin/x"):
            deny = M._require_admin()
        assert deny is not None and deny[1] == 403

    def test_bypass_admin_mode_admits_without_session(self, monkeypatch):
        # trusted_cidr/disabled with bypass_role=admin still admits (legacy LAN).
        import manager_mod as M
        import auth
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: True, raising=False)
        monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
        monkeypatch.setattr(auth, "_bypass_role", lambda: "admin")
        with M.app.test_request_context("/api/admin/x"):
            assert M._require_admin() is None


class TestExemptedAdminRouteRoleGate:
    # /api/remote/host-metrics/last is _require_admin-gated but the gate exempts
    # the /api/remote/ prefix (never sets g.auth_role) — effective_role must still
    # resolve the session role and deny operators end-to-end on this route.
    def _client(self, tmp_path, monkeypatch, role):
        import manager_mod as M
        import auth
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: True, raising=False)
        manager_users_init_for_test(tmp_path)
        user = "llmadmin"
        if role == "operator":
            manager_users.STORE.create("op1", auth.scrypt_hash("op1pass1"), "operator")
            user = "op1"
        M.app.config.update(TESTING=True)
        c = M.app.test_client()
        with c.session_transaction() as sess:
            sess["auth_ok"] = True
            sess["role"] = role
            sess["user"] = user
        return c

    def test_operator_denied_on_exempted_admin_route(self, tmp_path, monkeypatch):
        c = self._client(tmp_path, monkeypatch, "operator")
        r = c.get("/api/remote/host-metrics/last")
        assert r.status_code == 403 and r.get_json().get("role_denied") is True

    def test_admin_allowed_on_exempted_admin_route(self, tmp_path, monkeypatch):
        c = self._client(tmp_path, monkeypatch, "admin")
        r = c.get("/api/remote/host-metrics/last")
        assert r.status_code != 403  # admin passes the role+IP gate


class TestSessionRevocation:
    # Disabling / deleting / demoting a user takes effect on the NEXT request —
    # the gate re-derives role + validity from the store, not the signed cookie.
    def _logged_in(self, tmp_path, monkeypatch, username, role):
        import manager_mod as M
        import auth
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: True, raising=False)
        manager_users_init_for_test(tmp_path)
        if username != "llmadmin":
            manager_users.STORE.create(username, auth.scrypt_hash(username + "pw1"), role)
        M.app.config.update(TESTING=True)
        c = M.app.test_client()
        with c.session_transaction() as sess:
            sess["auth_ok"] = True
            sess["role"] = role
            sess["user"] = username
        return c

    def test_disabled_user_session_revoked(self, tmp_path, monkeypatch):
        c = self._logged_in(tmp_path, monkeypatch, "op1", "operator")
        assert c.get("/api/me").status_code == 200  # works while enabled
        manager_users.STORE.set_disabled("op1", True)
        r = c.get("/api/me")
        assert r.status_code == 401 and r.get_json().get("auth_required") is True

    def test_deleted_user_session_revoked(self, tmp_path, monkeypatch):
        c = self._logged_in(tmp_path, monkeypatch, "op1", "operator")
        manager_users.STORE.delete("op1")
        assert c.get("/api/me").status_code == 401

    def test_demoted_admin_loses_admin_immediately(self, tmp_path, monkeypatch):
        c = self._logged_in(tmp_path, monkeypatch, "admin2", "admin")
        assert c.get("/api/admin/users").status_code == 200  # admin2 is admin
        manager_users.STORE.set_role("admin2", "operator")   # llmadmin still admin
        r = c.get("/api/admin/users")
        assert r.status_code == 403 and r.get_json().get("role_denied") is True


def manager_users_init_for_test(tmp_path):
    import auth
    manager_users.init(tmp_path / "mu.json", threshold=5, window_s=900, duration_s=900)
    manager_users.STORE.seed_admin("llmadmin", auth.scrypt_hash("llmadmin"))


class TestUserRoutes:
    @pytest.fixture
    def admin_client(self, tmp_path, monkeypatch):
        import manager_mod as M
        import auth
        monkeypatch.setattr(M, "_admin_ip_allowed", lambda _ip: True, raising=False)
        manager_users_init_for_test(tmp_path)
        manager_users.STORE.create("op1", auth.scrypt_hash("op1pass1"), "operator") \
            if manager_users.STORE.get("op1") is None else None
        M.app.config.update(TESTING=True)
        c = M.app.test_client()
        with c.session_transaction() as sess:
            sess["auth_ok"] = True
            sess["role"] = "admin"
            sess["user"] = "llmadmin"
        return c

    def test_list_users(self, admin_client):
        r = admin_client.get("/api/admin/users")
        assert r.status_code == 200
        names = {u["username"] for u in r.get_json()["users"]}
        assert {"llmadmin", "op1"} <= names
        assert all("password_hash" not in u for u in r.get_json()["users"])

    def test_create_user(self, admin_client):
        r = admin_client.post("/api/admin/users",
                              json={"username": "newop", "password": "longenough", "role": "operator"})
        assert r.status_code == 200 and r.get_json()["ok"] is True

    def test_create_rejects_short_password(self, admin_client):
        r = admin_client.post("/api/admin/users",
                              json={"username": "x2", "password": "short", "role": "operator"})
        assert r.status_code == 400

    def test_create_duplicate_409(self, admin_client):
        r = admin_client.post("/api/admin/users",
                              json={"username": "op1", "password": "longenough", "role": "operator"})
        assert r.status_code == 409

    def test_reset_password(self, admin_client):
        r = admin_client.patch("/api/admin/users/op1", json={"password": "brandnewpw"})
        assert r.status_code == 200

    def test_patch_invalid_role_400(self, admin_client):
        r = admin_client.patch("/api/admin/users/op1", json={"role": "superuser"})
        assert r.status_code == 400  # malformed role is a bad request, not a conflict

    def test_patch_demote_last_admin_409(self, admin_client):
        r = admin_client.patch("/api/admin/users/llmadmin", json={"role": "operator"})
        assert r.status_code == 409  # last enabled admin can't be demoted

    def test_cannot_delete_self(self, admin_client):
        r = admin_client.delete("/api/admin/users/llmadmin")
        assert r.status_code == 409  # self-delete blocked

    def test_cannot_delete_last_admin(self, admin_client):
        r = admin_client.delete("/api/admin/users/llmadmin")
        assert r.status_code == 409

    def test_delete_operator(self, admin_client):
        r = admin_client.delete("/api/admin/users/op1")
        assert r.status_code == 200

    def test_unlock(self, admin_client):
        manager_users.LOCKOUT.record_failure("user:op1")
        r = admin_client.post("/api/admin/users/op1/unlock")
        assert r.status_code == 200

    def test_me_reports_role(self, admin_client):
        r = admin_client.get("/api/me")
        d = r.get_json()
        assert d["role"] == "admin" and d["is_admin"] is True and d["username"] == "llmadmin"

    def test_me_admin_access_true_when_role_admin_and_ip_allowed(self, admin_client, monkeypatch):
        import auth
        monkeypatch.setattr(auth, "_admin_ip_allowed", lambda _ip: True, raising=False)
        d = admin_client.get("/api/me").get_json()
        assert d["is_admin"] is True
        assert d.get("admin_ip") is True
        assert d.get("admin_access") is True

    def test_me_admin_access_false_when_role_admin_but_ip_denied(self, admin_client, monkeypatch):
        # The #117 bug: admin role from outside the admin CIDR must NOT get the
        # admin tab — admin_access tracks the role AND IP gate, is_admin stays role.
        import auth
        monkeypatch.setattr(auth, "_admin_ip_allowed", lambda _ip: False, raising=False)
        d = admin_client.get("/api/me").get_json()
        assert d["is_admin"] is True
        assert d.get("admin_ip") is False
        assert d.get("admin_access") is False

    def test_me_operator_has_no_admin_access(self, tmp_path, monkeypatch):
        # Operator role never gets admin_access even from an allowed admin IP.
        import manager_mod as M
        import auth
        monkeypatch.setattr(auth, "_admin_ip_allowed", lambda _ip: True, raising=False)
        manager_users_init_for_test(tmp_path)
        manager_users.STORE.create("op1", auth.scrypt_hash("op1pass1"), "operator")
        M.app.config.update(TESTING=True)
        c = M.app.test_client()
        with c.session_transaction() as sess:
            sess["auth_ok"] = True
            sess["role"] = "operator"
            sess["user"] = "op1"
        d = c.get("/api/me").get_json()
        assert d["is_admin"] is False
        assert d.get("admin_access") is False

    def test_account_password_requires_current(self, admin_client):
        r = admin_client.post("/api/account/password",
                              json={"current_password": "wrong", "new_password": "anotherlongpw"})
        assert r.status_code == 403

    def test_admin_auth_is_default_tracks_login_store(self, admin_client):
        # The Authentication card's "default password" warning must reflect the
        # LOGIN store (manager_users.json), and clear only on a REAL password
        # change — not the legacy manager_auth.json (#125 divergence fix).
        assert admin_client.get("/api/admin/auth").get_json()["is_default"] is True
        r = admin_client.post("/api/account/password",
                              json={"current_password": "llmadmin", "new_password": "a-real-password"})
        assert r.status_code == 200
        assert admin_client.get("/api/admin/auth").get_json()["is_default"] is False


class TestAdminIpOk:
    # auth.admin_ip_ok() is the IP half of the admin gate, consumed by /api/me.
    def test_true_when_remote_in_admin_cidr(self, monkeypatch):
        import manager_mod as M
        import auth
        monkeypatch.setattr(auth, "_admin_ip_allowed", lambda _ip: True, raising=False)
        with M.app.test_request_context("/api/me", environ_base={"REMOTE_ADDR": "10.0.0.5"}):
            assert auth.admin_ip_ok() is True

    def test_false_when_remote_outside_admin_cidr(self, monkeypatch):
        import manager_mod as M
        import auth
        monkeypatch.setattr(auth, "_admin_ip_allowed", lambda _ip: False, raising=False)
        with M.app.test_request_context("/api/me", environ_base={"REMOTE_ADDR": "1.2.3.4"}):
            assert auth.admin_ip_ok() is False
