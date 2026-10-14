"""#1217: a configured enrollment secret gates every new registration; token and
fingerprint re-registrations stay exempt; the ack and list payloads carry the state."""
from __future__ import annotations

import os

import pytest

import agent_registry
import manager_mod as M

FP = "sha256:" + "ab" * 32
VER = agent_registry.FP_REAUTH_FROM_VERSION
SECRET = "f" * 64


@pytest.fixture
def store(monkeypatch):
    data = {"agents": {}, "global": {}}
    saved = {}
    monkeypatch.setattr(agent_registry, "load_agents", lambda: data)
    monkeypatch.setattr(agent_registry, "save_agents", lambda d: saved.update(d))
    monkeypatch.setattr(agent_registry, "_reg_rate", {})
    cfg = agent_registry._deps.settings.manager.agents
    monkeypatch.setattr(cfg, "enrollment_mode", "open")
    monkeypatch.setattr(cfg, "enrollment_secret", "")
    data["saved"] = saved
    return data


@pytest.fixture
def secret(monkeypatch):
    def _set(value):
        monkeypatch.setattr(agent_registry._deps.settings.manager.agents, "enrollment_secret", value)
    return _set


def _register(c, remote="10.0.0.9", headers=None, **over):
    body = {"hostname": "box", "os": "linux", "bind_url": "http://10.0.0.9:8765",
            "fingerprint": "sha256:" + "cd" * 32, "version": VER}
    body.update(over)
    return c.post("/api/agents/register", json=body, headers=headers or {},
                  environ_base={"REMOTE_ADDR": remote})


def _approved(aid="a1", hostname="box", fp=FP):
    return {"agent_id": aid, "hostname": hostname, "os": "linux", "role": "auto",
            "bind_url": "http://10.0.0.9:8765", "fingerprint": fp, "version": VER,
            "status": "approved", "token": "tok-" + aid, "registered_from": "10.0.0.9",
            "capabilities": {}}


class TestUnset:
    def test_header_is_ignored_when_no_secret_is_configured(self, store):
        with M.app.test_client() as c:
            r = _register(c, headers={"X-Enrollment-Secret": "anything"})
        assert r.status_code == 200 and r.get_json()["status"] == "pending"

    def test_placeholder_counts_as_unset(self, store, secret):
        secret("REPLACE_ME")
        assert agent_registry.enrollment_secret() == ""
        with M.app.test_client() as c:
            assert _register(c).status_code == 200


class TestSet:
    def test_missing_header_is_refused_and_nothing_written(self, store, secret):
        secret(SECRET)
        with M.app.test_client() as c:
            r = _register(c)
        assert r.status_code == 403
        j = r.get_json()
        assert j["code"] == "enrollment_secret" and "Admin › Agents" in j["error"]
        assert store["agents"] == {} and store["saved"] == {}

    def test_wrong_header_is_refused(self, store, secret):
        secret(SECRET)
        with M.app.test_client() as c:
            r = _register(c, headers={"X-Enrollment-Secret": "e" * 64})
        assert r.status_code == 403 and r.get_json()["code"] == "enrollment_secret"

    def test_correct_header_is_accepted(self, store, secret):
        secret(SECRET)
        with M.app.test_client() as c:
            r = _register(c, headers={"X-Enrollment-Secret": SECRET})
        assert r.status_code == 200 and r.get_json()["status"] == "pending"
        assert len(store["agents"]) == 1

    def test_header_whitespace_is_tolerated(self, store, secret):
        secret(SECRET)
        with M.app.test_client() as c:
            r = _register(c, headers={"X-Enrollment-Secret": "  " + SECRET + " \t"})
        assert r.status_code == 200

    def test_header_surrounding_quotes_are_tolerated(self, store, secret):
        secret(SECRET)
        for quoted in ('"' + SECRET + '"', "'" + SECRET + "'", ' "' + SECRET + '" '):
            with M.app.test_client() as c:
                assert _register(c, headers={"X-Enrollment-Secret": quoted}).status_code == 200
        with M.app.test_client() as c:
            r = _register(c, headers={"X-Enrollment-Secret": '""'}, hostname="other-box", fingerprint="sha256:" + "ef" * 32)
        assert r.status_code == 403

    def test_loopback_needs_the_secret_too(self, store, secret):
        secret(SECRET)
        with M.app.test_client() as c:
            r = _register(c, remote="127.0.0.1")
        assert r.status_code == 403
        with M.app.test_client() as c:
            r = _register(c, remote="127.0.0.1", headers={"X-Enrollment-Secret": SECRET})
        assert r.status_code == 200 and r.get_json()["status"] == "approved"

    def test_token_reregistration_is_exempt(self, store, secret):
        secret(SECRET)
        store["agents"]["a1"] = _approved()
        with M.app.test_client() as c:
            r = _register(c, headers={"Authorization": "Bearer tok-a1"}, fingerprint="sha256:" + "ee" * 32)
        assert r.status_code == 200 and r.get_json()["agent_id"] == "a1"

    def test_fingerprint_reregistration_is_exempt(self, store, secret):
        secret(SECRET)
        store["agents"]["a1"] = _approved()
        with M.app.test_client() as c:
            r = _register(c, fingerprint=FP)
        assert r.status_code == 200 and r.get_json()["agent_id"] == "a1"

    def test_repeat_pending_from_same_address_is_refused_without_secret(self, store, secret):
        secret(SECRET)
        with M.app.test_client() as c:
            assert _register(c, headers={"X-Enrollment-Secret": SECRET}).status_code == 200
            assert _register(c, fingerprint="sha256:" + "ee" * 32).status_code == 403
            assert len(store["agents"]) == 1


def _admin_client():
    c = M.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


class TestPayloads:
    def test_list_reports_requirement_and_fingerprint_fields(self, store, secret, monkeypatch):
        secret(SECRET)
        monkeypatch.setattr(agent_registry, "ca_fingerprint", lambda: "sha256:" + "0" * 64)
        monkeypatch.setattr(agent_registry, "manager_https_url", lambda: "https://10.0.0.1:5443")
        c = _admin_client()
        j = c.get("/api/agents").get_json()
        assert j["enrollment_secret_required"] is True
        assert j["ca_fingerprint"] == "sha256:" + "0" * 64
        assert j["manager_https_url"] == "https://10.0.0.1:5443"
        secret("")
        assert c.get("/api/agents").get_json()["enrollment_secret_required"] is False

    def test_heartbeat_ack_never_carries_the_secret(self, store, secret, monkeypatch):
        secret(SECRET)
        store["agents"]["a1"] = _approved()
        agent = store["agents"]["a1"]
        monkeypatch.setattr(agent_registry, "agent_by_token", lambda tok: agent)
        monkeypatch.setattr(M.auth, "_agent_by_token", lambda tok: agent, raising=False)
        monkeypatch.setattr(agent_registry, "_maybe_issue_tls_bundle", lambda a, b: None)
        with M.app.test_client() as c:
            r = c.post("/api/agents/heartbeat", json={"agent_id": "a1"},
                       headers={"Authorization": "Bearer tok-a1"},
                       environ_base={"REMOTE_ADDR": "10.0.0.9"})
        assert r.status_code == 200 and "enrollment_secret" not in r.get_json()
        assert SECRET not in r.get_data(as_text=True)


class TestAdminRoutes:
    def test_reveal_needs_admin(self, store):
        with M.app.test_client() as c:
            assert c.get("/api/agents/enrollment-secret").status_code in (401, 403)

    def test_reveal_returns_value_and_set_flag(self, store, secret):
        secret(SECRET)
        j = _admin_client().get("/api/agents/enrollment-secret").get_json()
        assert j == {"ok": True, "secret": SECRET, "set": True}
        secret("")
        assert _admin_client().get("/api/agents/enrollment-secret").get_json()["set"] is False

    def test_rotate_writes_toml_and_hot_reloads(self, store, monkeypatch):
        written = {}
        monkeypatch.setattr(M.settings_toml_io, "apply_patches",
                            lambda changes, removals=None: written.update(changes))
        reloaded = []
        monkeypatch.setattr(M, "_agents_reload_config", lambda: reloaded.append(1))
        r = _admin_client().post("/api/agents/enrollment-secret", json={"action": "rotate"})
        j = r.get_json()
        assert r.status_code == 200 and j["set"] is True
        assert len(j["secret"]) == 64 and int(j["secret"], 16) >= 0
        assert written == {"manager.agents.enrollment_secret": j["secret"]}
        assert reloaded == [1]

    def test_rotate_rejects_other_actions(self, store):
        r = _admin_client().post("/api/agents/enrollment-secret", json={"action": "clear"})
        assert r.status_code == 400


    def test_rotate_is_audited_without_the_secret(self, store, monkeypatch):
        assert M._audit_match("POST", "/api/agents/enrollment-secret") == (
            "agent.enrollment-secret", None, "agent.lifecycle")
        assert M._audit_match("GET", "/api/agents/enrollment-secret") is None
        monkeypatch.setattr(M.settings_toml_io, "apply_patches", lambda changes, removals=None: None)
        monkeypatch.setattr(M, "_agents_reload_config", lambda: None)
        rows = []
        monkeypatch.setattr(M, "_audit_record", rows.append)
        monkeypatch.setitem(M._AUDIT_CFG, "save_automated", True)
        r = _admin_client().post("/api/agents/enrollment-secret", json={"action": "rotate"})
        new = r.get_json()["secret"]
        assert len(rows) == 1 and rows[0][7] == "agent.enrollment-secret"
        assert new not in repr(rows[0])


class TestPkiCa:
    def test_ca_route_is_public_and_reports_fingerprint(self, store, monkeypatch):
        import hashlib
        from cryptography.hazmat.primitives.serialization import Encoding
        ca_cert, _key, _pki = M._pki_ensure_ca()
        want = "sha256:" + hashlib.sha256(ca_cert.public_bytes(Encoding.DER)).hexdigest()
        monkeypatch.setattr(agent_registry, "manager_https_url", lambda: "https://10.0.0.1:5443")
        with M.app.test_client() as c:
            r = c.get("/api/pki/ca", environ_base={"REMOTE_ADDR": "10.0.0.9"})
        j = r.get_json()
        assert r.status_code == 200 and j["fingerprint"] == want
        assert j["ca_pem"].startswith("-----BEGIN CERTIFICATE-----")
        assert j["https_url"] == "https://10.0.0.1:5443"
        assert r.headers["Cache-Control"] == "no-store"

    def test_ca_route_is_in_the_open_and_budgeted_path_sets(self):
        import auth
        assert "/api/pki/ca" in auth.AUTH_OPEN_PATHS
        assert "/api/pki/ca" in auth._ANON_BUDGET_PATHS


def test_ca_fingerprint_cached_on_stat(tmp_path, monkeypatch):
    import hashlib
    import shutil
    import _pki
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    monkeypatch.setattr(agent_registry._deps, "data_dir", tmp_path)
    agent_registry._ca_fp_cache.clear()
    cert, _key = _pki.load_or_create_ca(tmp_path)
    want = "sha256:" + hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest()
    assert agent_registry.ca_fingerprint() == want

    def _boom(*a, **k):
        raise AssertionError("re-parsed")

    with monkeypatch.context() as m:
        m.setattr(x509, "load_pem_x509_certificate", _boom)
        assert agent_registry.ca_fingerprint() == want

    other = tmp_path / "other"
    other.mkdir()
    cert2, _k2 = _pki.load_or_create_ca(other)
    ca = tmp_path / "internal-ca.crt"
    shutil.copyfile(other / "internal-ca.crt", ca)
    os.utime(ca, ns=(1, 1))
    want2 = "sha256:" + hashlib.sha256(cert2.public_bytes(Encoding.DER)).hexdigest()
    assert want2 != want
    assert agent_registry.ca_fingerprint() == want2
    ca.unlink()
    assert agent_registry.ca_fingerprint() == ""
