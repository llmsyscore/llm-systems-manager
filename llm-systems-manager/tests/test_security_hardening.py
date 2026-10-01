"""Security-hardening bundle: scheme-aware Secure cookie (#864), agent re-auth factors (#865),
forced temporary-password change (#866, #1160), opt-in HSTS + cors_origins removal (#867)."""
from __future__ import annotations

import pytest

import agent_registry
import auth
import manager_mod as M
import manager_users


# ── shared fixtures ──────────────────────────────────────────────────────────

TEMP_PW = "temp-pw-Zx81q"

@pytest.fixture
def users(tmp_path, monkeypatch):
    """Fresh user store whose first admin has a temporary password; auth mode pinned to `required`."""
    manager_users.init(tmp_path / "manager_users.json", threshold=5, window_s=60, duration_s=60)
    manager_users.STORE.set_temporary(auth.DEFAULT_AUTH_USER, auth.scrypt_hash(TEMP_PW))
    manager_users.STORE.create("alice", auth.scrypt_hash("pw-alice-123"), "admin")
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    return manager_users


def _set_cookies(resp) -> list[str]:
    return resp.headers.getlist("Set-Cookie")


# ── #864 Secure flag follows the request scheme ─────────────────────────────

class TestSessionCookieSecure:
    def test_http_login_cookie_is_not_secure(self, users):
        with M.app.test_client() as c:
            r = c.post("/login", data={"username": "alice", "password": "pw-alice-123"})
            assert r.status_code == 302
            cookies = _set_cookies(r)
            assert cookies and all("secure" not in ck.lower() for ck in cookies)
            assert cookies[0].startswith("session=")

    def test_https_login_cookie_is_secure(self, users):
        with M.app.test_client() as c:
            r = c.post("/login", data={"username": "alice", "password": "pw-alice-123"},
                       base_url="https://localhost")
            assert r.status_code == 302
            cookies = _set_cookies(r)
            assert cookies and all("; secure" in ck.lower() for ck in cookies)
            assert all("httponly" in ck.lower() for ck in cookies)
            assert cookies[0].startswith("__Secure-session=")

    def test_plain_cookie_value_is_not_valid_as_the_tls_cookie(self, users):
        with M.app.test_client() as c:
            r = c.post("/login", data={"username": "alice", "password": "pw-alice-123"})
            value = _set_cookies(r)[0].split(";", 1)[0].split("=", 1)[1]
            c.delete_cookie("session")
            c.set_cookie("__Secure-session", value, domain="localhost")
            assert c.get("/api/me", base_url="https://localhost").status_code == 401
            c.delete_cookie("__Secure-session", domain="localhost")
            c.set_cookie("session", value, domain="localhost")
            assert c.get("/api/me").status_code == 200

    def test_tls_and_plain_sessions_are_independent(self, users):
        with M.app.test_client() as c:
            c.post("/login", data={"username": "alice", "password": "pw-alice-123"},
                   base_url="https://localhost")
            assert c.get("/api/me", base_url="https://localhost").status_code == 200
            assert c.get("/api/me").status_code == 401


# ── #865 agent re-auth factors ──────────────────────────────────────────────

FP = "sha256:" + "ab" * 32
LEGACY_VERSION = "v2026.08.15-3"
AGENT_ID = "11111111-2222-3333-4444-555555555555"


def _agent(version: str, fingerprint: str = FP, status: str = "approved") -> dict:
    return {
        "agent_id": AGENT_ID,
        "hostname": "box", "os": "linux", "role": "auto", "bind_url": "http://10.0.0.5:8765",
        "fingerprint": fingerprint, "version": version, "status": status,
        "token": "tok-secret" if status == "approved" else None,
        "registered_from": "10.0.0.5", "capabilities": {},
    }


@pytest.fixture
def registry(monkeypatch):
    saved = {}

    def install(agent: dict) -> dict:
        store = {"agents": {agent["agent_id"]: agent}, "global": {}}
        monkeypatch.setattr(agent_registry, "load_agents", lambda: store)
        monkeypatch.setattr(agent_registry, "save_agents", lambda d: saved.update(d))
        return store

    install.saved = saved  # type: ignore[attr-defined]
    return install


def _status(c, remote, fp=None):
    headers = {"X-Agent-Fingerprint": fp} if fp else {}
    return c.get(f"/api/agents/{AGENT_ID}/status", headers=headers,
                 environ_base={"REMOTE_ADDR": remote}).get_json()


class TestAgentStatusReauth:
    def test_current_agent_needs_fingerprint_not_just_ip(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION))
        with M.app.test_client() as c:
            assert "token" not in _status(c, "10.0.0.5")

    def test_current_agent_fingerprint_wins_even_from_new_ip(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION))
        with M.app.test_client() as c:
            assert _status(c, "10.0.0.99", fp=FP)["token"] == "tok-secret"

    def test_wrong_fingerprint_denied_even_from_same_ip(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION))
        with M.app.test_client() as c:
            assert "token" not in _status(c, "10.0.0.5", fp="sha256:" + "00" * 32)

    def test_legacy_agent_gets_no_token_by_ip_or_fingerprint(self, registry):
        registry(_agent(LEGACY_VERSION))
        with M.app.test_client() as c:
            assert "token" not in _status(c, "10.0.0.5")
            assert "token" not in _status(c, "10.0.0.5", fp=FP)

    def test_record_without_stored_fp_gets_no_token(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION, fingerprint=""))
        with M.app.test_client() as c:
            assert "token" not in _status(c, "10.0.0.5")
            assert "token" not in _status(c, "10.0.0.5", fp=FP)

    def test_unparseable_version_still_reauths_by_fingerprint(self, registry):
        registry(_agent("dev-build"))
        with M.app.test_client() as c:
            assert _status(c, "10.0.0.99", fp=FP)["token"] == "tok-secret"
            assert "token" not in _status(c, "10.0.0.5")

    def test_fingerprint_stored_by_a_legacy_agent_stays_refused_after_a_version_bump(self, registry):
        a = _agent(agent_registry.FP_REAUTH_FROM_VERSION)
        a["fp_version"] = LEGACY_VERSION
        registry(a)
        with M.app.test_client() as c:
            assert "token" not in _status(c, "10.0.0.5", fp=FP)

    def test_non_text_fingerprint_or_version_gets_no_token(self, registry):
        a = _agent(20260816)
        a["fingerprint"] = 12345
        registry(a)
        with M.app.test_client() as c:
            assert "token" not in _status(c, "10.0.0.5", fp="12345")

    def test_pending_agent_never_gets_a_token(self, registry):
        registry(_agent(LEGACY_VERSION, status="pending"))
        with M.app.test_client() as c:
            d = _status(c, "10.0.0.5", fp=FP)
            assert d["status"] == "pending" and "token" not in d


def _register(c, remote, fp, version=None, token=None):
    body = {"hostname": "box", "os": "linux", "bind_url": "http://10.0.0.5:8765",
            "fingerprint": fp, "version": version or agent_registry.FP_REAUTH_FROM_VERSION}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post("/api/agents/register", json=body, headers=headers,
                  environ_base={"REMOTE_ADDR": remote})


class TestAgentReregisterReauth:
    def test_ip_alone_no_longer_reauths_a_current_agent(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION))
        with M.app.test_client() as c:
            r = _register(c, "10.0.0.5", fp="sha256:" + "00" * 32)
            assert r.status_code == 403 and "token" not in r.get_json()
            assert not registry.saved

    def test_fingerprint_reauths_and_refreshes_record(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION))
        with M.app.test_client() as c:
            r = _register(c, "10.0.0.77", fp=FP)
            assert r.status_code == 200 and r.get_json()["token"] == "tok-secret"
            assert registry.saved["agents"][AGENT_ID]["registered_from"] == "10.0.0.77"

    def test_prior_token_still_reauths(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION))
        with M.app.test_client() as c:
            r = _register(c, "10.0.0.77", fp="sha256:" + "00" * 32, token="tok-secret")
            assert r.status_code == 200 and r.get_json()["token"] == "tok-secret"

    def test_reregister_survives_blank_remote_addr(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION))
        with M.app.test_client() as c:
            r = _register(c, "", fp=FP)
            assert r.status_code == 200
            assert registry.saved["agents"][AGENT_ID]["registered_from"] == "10.0.0.5"

    def test_cutoff_is_not_ahead_of_the_shipped_agent(self):
        import re
        from pathlib import Path
        src = (Path(agent_registry.__file__).resolve().parents[2] / "agent" / "llm-systems-agent.py").read_text()
        shipped = re.search(r'^VERSION = "([^"]+)"', src, re.M).group(1)
        assert agent_registry._version_key(shipped) >= agent_registry._version_key(agent_registry.FP_REAUTH_FROM_VERSION)

    def test_token_reregister_cannot_blank_the_fingerprint(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION))
        with M.app.test_client() as c:
            r = _register(c, "10.0.0.5", fp="", token="tok-secret")
            assert r.status_code == 200
            assert registry.saved["agents"][AGENT_ID]["fingerprint"] == FP

    def test_legacy_record_reregisters_only_with_its_token(self, registry):
        registry(_agent(LEGACY_VERSION))
        with M.app.test_client() as c:
            r = _register(c, "10.0.0.5", fp=FP, version=LEGACY_VERSION)
            assert r.status_code == 403 and "token" not in r.get_json()
            assert not registry.saved
            r = _register(c, "10.0.0.5", fp=FP, token="tok-secret")
            assert r.status_code == 200 and r.get_json()["token"] == "tok-secret"
            assert registry.saved["agents"][AGENT_ID]["version"] == agent_registry.FP_REAUTH_FROM_VERSION
            assert registry.saved["agents"][AGENT_ID]["fp_version"] == agent_registry.FP_REAUTH_FROM_VERSION

    def test_heartbeat_version_bump_pins_the_stored_fingerprint_version(self, registry, monkeypatch):
        store = registry(_agent(LEGACY_VERSION))
        monkeypatch.setattr(agent_registry, "agent_by_token", lambda t: store["agents"][AGENT_ID])
        monkeypatch.setattr(auth, "_agent_by_token", lambda t: store["agents"][AGENT_ID])
        monkeypatch.setattr(agent_registry, "_maybe_issue_tls_bundle", lambda agent, body: None)
        with M.app.test_client() as c:
            r = c.post("/api/agents/heartbeat", json={"version": agent_registry.FP_REAUTH_FROM_VERSION},
                       headers={"Authorization": "Bearer tok-secret"})
            assert r.status_code == 200
            assert store["agents"][AGENT_ID]["version"] == agent_registry.FP_REAUTH_FROM_VERSION
            assert store["agents"][AGENT_ID]["fp_version"] == LEGACY_VERSION
            assert "token" not in _status(c, "10.0.0.5", fp=FP)


# ── #866 / #1160 forced password change on a temporary password ────────────

class TestForcedPasswordChange:
    def _login_default(self, c):
        return c.post("/login", data={"username": auth.DEFAULT_AUTH_USER,
                                      "password": TEMP_PW})

    def test_default_login_is_walled_until_password_changes(self, users):
        with M.app.test_client() as c:
            assert self._login_default(c).status_code == 302
            r = c.get("/api/me")
            assert r.status_code == 403 and r.get_json()["password_change_required"] is True
            r = c.get("/")
            assert r.status_code == 302 and r.headers["Location"].endswith("/login")
            page = c.get("/login")
            assert page.status_code == 200 and b"new_password" in page.data
            r = c.post("/api/account/password", json={
                "current_password": TEMP_PW, "new_password": "much-better-pw"})
            assert r.status_code == 200 and r.get_json()["ok"] is True
            assert c.get("/api/me").status_code == 200

    def test_short_new_password_keeps_the_wall(self, users):
        with M.app.test_client() as c:
            self._login_default(c)
            r = c.post("/api/account/password", json={
                "current_password": TEMP_PW, "new_password": "short"})
            assert r.status_code == 400
            assert c.get("/api/me").status_code == 403

    def test_logout_is_allowed_while_walled(self, users):
        with M.app.test_client() as c:
            self._login_default(c)
            assert c.get("/logout").status_code == 302
            assert c.get("/api/me").status_code == 401

    def test_companion_destination_survives_the_wall(self, users):
        with M.app.test_client() as c:
            self._login_default(c)
            r = c.get("/companion")
            assert r.status_code == 302 and r.headers["Location"].endswith("/login?next=/companion")
            assert b'data-next="/companion"' in c.get("/login?next=/companion").data

    def test_relogin_as_another_user_clears_the_wall(self, users):
        with M.app.test_client() as c:
            self._login_default(c)
            c.post("/login", data={"username": "alice", "password": "pw-alice-123"})
            assert c.get("/api/me").status_code == 200

    def test_pre_existing_session_is_walled_too(self, users):
        with M.app.test_client() as c:
            with c.session_transaction() as sess:
                sess["auth_ok"] = True
                sess["user"] = auth.DEFAULT_AUTH_USER
                sess["role"] = "admin"
            assert c.get("/api/me").status_code == 403

    def test_renamed_admin_on_default_password_is_walled(self, users):
        users.STORE.create("ops", auth.scrypt_hash(TEMP_PW), "admin", must_change=True)
        with M.app.test_client() as c:
            c.post("/login", data={"username": "ops", "password": TEMP_PW})
            assert c.get("/api/me").status_code == 403
            assert b"<b>ops</b>" in c.get("/login").data

    def test_bypass_modes_ignore_a_stale_default_session(self, users, monkeypatch):
        monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
        with M.app.test_client() as c:
            with c.session_transaction() as sess:
                sess["auth_ok"] = True
                sess["user"] = auth.DEFAULT_AUTH_USER
            assert c.get("/api/me").status_code == 200
            assert c.get("/login").status_code == 302

    def test_the_temporary_password_cannot_be_kept(self, users):
        with M.app.test_client() as c:
            self._login_default(c)
            r = c.post("/api/account/password", json={"current_password": TEMP_PW, "new_password": TEMP_PW})
            assert r.status_code == 400
            assert c.get("/api/me").status_code == 403

    def test_non_default_login_is_not_walled(self, users):
        with M.app.test_client() as c:
            c.post("/login", data={"username": "alice", "password": "pw-alice-123"})
            assert c.get("/api/me").status_code == 200

    def test_default_user_with_changed_password_is_not_walled(self, users):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash("rotated-pw-1"))
        with M.app.test_client() as c:
            c.post("/login", data={"username": auth.DEFAULT_AUTH_USER, "password": "rotated-pw-1"})
            assert c.get("/api/me").status_code == 200


# ── #1160 no built-in admin password ────────────────────────────────────────

class TestNoBuiltInPassword:
    OLD = auth.DEFAULT_AUTH_USER   # older releases shipped the admin name as its password

    def test_no_credential_means_no_seed_hash(self, monkeypatch):
        monkeypatch.setattr(auth, "auth_runtime", lambda: {})
        monkeypatch.setattr(M.settings.manager.auth, "password_hash", "")
        assert auth.auth_credential()[1:] == ("", True)
        assert not hasattr(auth, "DEFAULT_AUTH_PASSWORD") and not hasattr(auth, "DEFAULT_AUTH_HASH")

    def test_old_shipped_password_is_retired_and_cannot_sign_in(self, users):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash(self.OLD))
        assert users.STORE.retire(auth.uses_retired_password) == [auth.DEFAULT_AUTH_USER]
        u = users.STORE.get(auth.DEFAULT_AUTH_USER)
        assert u["password_hash"] == "" and u["reset_required"] is True
        assert not users.STORE.needs_reset() and users.STORE.password_pending()   # alice can still sign in
        assert users.STORE.retire(auth.uses_retired_password) == []
        with M.app.test_client() as c:
            r = c.post("/login", data={"username": auth.DEFAULT_AUTH_USER, "password": self.OLD})
            assert r.status_code == 401
            assert c.get("/api/me").status_code == 401

    def test_other_accounts_are_untouched_by_the_retirement(self, users):
        assert users.STORE.retire(auth.uses_retired_password) == []
        with M.app.test_client() as c:
            assert c.post("/login", data={"username": "alice", "password": "pw-alice-123"}).status_code == 302

    def test_session_of_a_retired_account_is_signed_out(self, users):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash(self.OLD))
        with M.app.test_client() as c:
            with c.session_transaction() as sess:
                sess["auth_ok"] = True
                sess["user"] = auth.DEFAULT_AUTH_USER
                sess["role"] = "admin"
            users.STORE.retire(auth.uses_retired_password)
            assert c.get("/api/me").status_code == 401

    def test_login_page_says_how_to_reset_only_to_admin_addresses(self, users, monkeypatch):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash(self.OLD))
        users.STORE.set_disabled("alice", True)
        users.STORE.retire(auth.uses_retired_password)
        with M.app.test_client() as c:
            monkeypatch.setattr(auth, "_admin_ip_allowed", lambda ip: True)
            page = c.get("/login").data.decode()
            assert "admin password is not set" in page and "admin_password.py reset" in page
            monkeypatch.setattr(auth, "_admin_ip_allowed", lambda ip: False)
            page = c.get("/login").data.decode()
            assert "admin password is not set" in page and "admin_password.py" not in page

    def test_login_page_has_no_note_once_an_admin_can_sign_in(self, users):
        with M.app.test_client() as c:
            assert b"admin password is not set" not in c.get("/login").data

    def test_empty_store_shows_the_note(self, tmp_path, monkeypatch):
        manager_users.init(tmp_path / "none.json", threshold=5, window_s=60, duration_s=60)
        monkeypatch.setattr(auth, "auth_mode", lambda: "required")
        with M.app.test_client() as c:
            assert b"admin password is not set" in c.get("/login").data

    def test_no_note_while_another_admin_can_sign_in(self, users):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash(self.OLD))
        users.STORE.retire(auth.uses_retired_password)
        with M.app.test_client() as c:
            assert b"admin password is not set" not in c.get("/login").data

    def test_the_old_shipped_password_cannot_be_set_again(self, users):
        with M.app.test_client() as c:
            c.post("/login", data={"username": "alice", "password": "pw-alice-123"})
            assert c.post("/api/account/password", json={
                "current_password": "pw-alice-123", "new_password": self.OLD}).status_code == 400
            assert c.patch("/api/admin/users/alice", json={"password": self.OLD},
                           environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 400
            assert c.post("/api/admin/users", json={"username": "bob", "password": self.OLD, "role": "operator"},
                          environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code == 400
        assert users.STORE.get("bob") is None

    def test_a_retired_admin_does_not_count_as_the_last_admin(self, users):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash(self.OLD))
        users.STORE.retire(auth.uses_retired_password)
        with pytest.raises(ValueError):
            users.STORE.set_role("alice", "operator")
        with pytest.raises(ValueError):
            users.STORE.delete("alice")

    def test_startup_pass_seeds_then_retires_a_provisioned_old_password(self, tmp_path):
        manager_users.init(tmp_path / "fresh.json", threshold=5, window_s=60, duration_s=60)
        retired, no_admin = manager_users.bootstrap("llmadmin", auth.scrypt_hash(self.OLD), auth.uses_retired_password)
        assert retired == ["llmadmin"] and no_admin is True
        manager_users.init(tmp_path / "fresh2.json", threshold=5, window_s=60, duration_s=60)
        assert manager_users.bootstrap("llmadmin", "", auth.uses_retired_password) == ([], True)
        assert manager_users.STORE.is_empty()
        manager_users.init(tmp_path / "fresh3.json", threshold=5, window_s=60, duration_s=60)
        assert manager_users.bootstrap("ops", auth.scrypt_hash("a-good-password"), auth.uses_retired_password) == ([], False)
        assert manager_users.STORE.get("ops")["role"] == "admin"

    def test_account_without_a_hash_still_pays_one_scrypt(self, users, monkeypatch):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash(self.OLD))
        users.STORE.retire(auth.uses_retired_password)
        seen = []
        real = auth.scrypt_verify
        monkeypatch.setattr(auth, "scrypt_verify", lambda pw, h: seen.append(h) or real(pw, h))
        assert manager_users.authenticate(auth.DEFAULT_AUTH_USER, "x", "10.0.0.9")["ok"] is False
        assert seen == [manager_users._DECOY_HASH]

    def test_reset_gives_a_temporary_password_that_must_be_changed(self, users):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash(self.OLD))
        users.STORE.set_disabled("alice", True)
        users.STORE.retire(auth.uses_retired_password)
        assert users.STORE.needs_reset()
        users.STORE.set_temporary(auth.DEFAULT_AUTH_USER, auth.scrypt_hash("fresh-temp-1"))
        assert not users.STORE.needs_reset()
        with M.app.test_client() as c:
            assert c.post("/login", data={"username": auth.DEFAULT_AUTH_USER, "password": "fresh-temp-1"}).status_code == 302
            assert c.get("/api/me").status_code == 403
            assert c.post("/api/account/password", json={
                "current_password": "fresh-temp-1", "new_password": "my-own-password"}).status_code == 200
            assert c.get("/api/me").status_code == 200
        assert "must_change" not in users.STORE.get(auth.DEFAULT_AUTH_USER)

    def test_admin_reset_of_another_user_clears_the_flags(self, users):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash("x-new-password"))
        u = users.STORE.get(auth.DEFAULT_AUTH_USER)
        assert "must_change" not in u and "reset_required" not in u


# ── #867 HSTS opt-in + cors_origins removal ─────────────────────────────────

class TestHsts:
    def test_off_by_default(self):
        assert int(getattr(M.settings.manager, "hsts_max_age_s", 0)) == 0
        with M.app.test_client() as c:
            r = c.get("/health", base_url="https://localhost")
            assert "Strict-Transport-Security" not in r.headers

    def test_enabled_only_on_tls_responses(self, monkeypatch):
        monkeypatch.setattr(M.settings.manager, "hsts_max_age_s", 3600, raising=False)
        with M.app.test_client() as c:
            assert c.get("/health", base_url="https://localhost").headers[
                "Strict-Transport-Security"] == "max-age=3600"
            assert "Strict-Transport-Security" not in c.get("/health").headers


# ── #875 baseline security response headers ─────────────────────────────────

_BASELINE = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "frame-ancestors 'self'",
}


class TestBaselineSecurityHeaders:
    @pytest.mark.parametrize("path", ["/health", "/login", "/api/me", "/manifest.webmanifest", "/sw.js"])
    def test_present_on_every_response(self, path):
        with M.app.test_client() as c:
            r = c.get(path)
            for name, value in _BASELINE.items():
                assert r.headers.get(name) == value, (path, name, dict(r.headers))

    def test_present_on_tls_responses_too(self):
        with M.app.test_client() as c:
            r = c.get("/health", base_url="https://localhost")
            assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_route_set_header_is_not_overwritten(self):
        from flask import Response
        resp = Response("x", headers={"Content-Security-Policy": "default-src 'self'",
                                      "X-Frame-Options": "DENY"})
        out = M._baseline_security_headers(resp)
        assert out.headers["Content-Security-Policy"] == "default-src 'self'"
        assert out.headers["X-Frame-Options"] == "DENY"
        assert out.headers["X-Content-Type-Options"] == "nosniff"
        assert out.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_manager_cors_origins_is_gone():
    from config.unified_config import ManagerConfig
    import settings_catalog
    assert "cors_origins" not in ManagerConfig.model_fields
    assert "manager.cors_origins" not in {e["path"] for e in settings_catalog.CATALOG}
    assert "manager.hsts_max_age_s" in {e["path"] for e in settings_catalog.CATALOG}
