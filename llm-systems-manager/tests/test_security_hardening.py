"""Security-hardening bundle: scheme-aware Secure cookie (#864), agent re-auth factors (#865),
forced default-password change (#866), opt-in HSTS + cors_origins removal (#867)."""
from __future__ import annotations

import pytest

import agent_registry
import auth
import manager_mod as M
import manager_users


# ── shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def users(tmp_path, monkeypatch):
    """Fresh user store seeded with the shipped default admin; auth mode pinned to `required`."""
    manager_users.init(tmp_path / "manager_users.json", threshold=5, window_s=60, duration_s=60)
    manager_users.STORE.seed_admin(auth.DEFAULT_AUTH_USER, auth.DEFAULT_AUTH_HASH)
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
LEGACY_VERSION = "v2026.09.02-1"
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

    def test_legacy_agent_keeps_ip_match(self, registry):
        registry(_agent(LEGACY_VERSION))
        with M.app.test_client() as c:
            assert _status(c, "10.0.0.5")["token"] == "tok-secret"
            assert "token" not in _status(c, "10.0.0.99")

    def test_legacy_record_keeps_any_of_semantics(self, registry):
        registry(_agent(LEGACY_VERSION))
        with M.app.test_client() as c:
            assert _status(c, "10.0.0.5", fp="sha256:" + "00" * 32)["token"] == "tok-secret"
            assert "token" not in _status(c, "10.0.0.99", fp="sha256:" + "00" * 32)

    def test_current_record_without_stored_fp_still_accepts_ip(self, registry):
        registry(_agent(agent_registry.FP_REAUTH_FROM_VERSION, fingerprint=""))
        with M.app.test_client() as c:
            assert _status(c, "10.0.0.5")["token"] == "tok-secret"
            assert "token" not in _status(c, "10.0.0.99", fp=FP)

    def test_legacy_record_without_fp_lets_new_agent_use_ip(self, registry):
        registry(_agent(LEGACY_VERSION, fingerprint=""))
        with M.app.test_client() as c:
            assert _status(c, "10.0.0.5", fp=FP)["token"] == "tok-secret"

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

    def test_legacy_record_ip_only_reregister_still_works(self, registry):
        registry(_agent(LEGACY_VERSION, fingerprint=""))
        with M.app.test_client() as c:
            r = _register(c, "10.0.0.5", fp=FP, version=LEGACY_VERSION)
            assert r.status_code == 200 and r.get_json()["token"] == "tok-secret"


# ── #866 forced password change on default credentials ─────────────────────

class TestForcedPasswordChange:
    def _login_default(self, c):
        return c.post("/login", data={"username": auth.DEFAULT_AUTH_USER,
                                      "password": auth.DEFAULT_AUTH_PASSWORD})

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
                "current_password": auth.DEFAULT_AUTH_PASSWORD, "new_password": "much-better-pw"})
            assert r.status_code == 200 and r.get_json()["ok"] is True
            assert c.get("/api/me").status_code == 200

    def test_short_new_password_keeps_the_wall(self, users):
        with M.app.test_client() as c:
            self._login_default(c)
            r = c.post("/api/account/password", json={
                "current_password": auth.DEFAULT_AUTH_PASSWORD, "new_password": "short"})
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
        users.STORE.create("ops", auth.DEFAULT_AUTH_HASH, "admin")
        with M.app.test_client() as c:
            c.post("/login", data={"username": "ops", "password": auth.DEFAULT_AUTH_PASSWORD})
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

    def test_non_default_login_is_not_walled(self, users):
        with M.app.test_client() as c:
            c.post("/login", data={"username": "alice", "password": "pw-alice-123"})
            assert c.get("/api/me").status_code == 200

    def test_default_user_with_changed_password_is_not_walled(self, users):
        users.STORE.set_password(auth.DEFAULT_AUTH_USER, auth.scrypt_hash("rotated-pw-1"))
        with M.app.test_client() as c:
            c.post("/login", data={"username": auth.DEFAULT_AUTH_USER, "password": "rotated-pw-1"})
            assert c.get("/api/me").status_code == 200


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


def test_manager_cors_origins_is_gone():
    from config.unified_config import ManagerConfig
    import settings_catalog
    assert "cors_origins" not in ManagerConfig.model_fields
    assert "manager.cors_origins" not in {e["path"] for e in settings_catalog.CATALOG}
    assert "manager.hsts_max_age_s" in {e["path"] for e in settings_catalog.CATALOG}
