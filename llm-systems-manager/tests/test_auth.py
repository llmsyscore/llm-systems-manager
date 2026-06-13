"""
Unit tests for the manager's admin auth helpers.

Scope (now lives in the dedicated `auth` module, extracted from the manager
monolith in Tier 3 / PR M1):
- scrypt_hash / scrypt_verify — pure crypto, no I/O
- auth_runtime / auth_write — read/write of data/manager_auth.json
- auth_credential — precedence (JSON runtime > TOML > built-in default)
- auth_mode / auth_policy — TOML-pinned vs JSON-overridable

We never touch the real data/manager_auth.json — every test monkey-patches
auth.MANAGER_AUTH_FILE to a tmp_path file so the live install stays untouched.

The conftest loads manager_mod (the Flask app), which in turn calls
auth.register_auth(...) — so by the time tests run, auth's module-level
globals (DEFAULT_AUTH_HASH, _settings, MANAGER_AUTH_FILE) are populated.
"""
from __future__ import annotations

import json

import pytest

import auth
import manager_mod as M


# ── scrypt_hash / scrypt_verify ──────────────────────────────────────────────

class TestScrypt:
    def test_hash_round_trips_via_verify(self):
        h = auth.scrypt_hash("hunter2")
        assert auth.scrypt_verify("hunter2", h) is True

    def test_hash_starts_with_algo_marker(self):
        h = auth.scrypt_hash("anything")
        assert h.startswith("scrypt$")
        parts = h.split("$")
        assert len(parts) == 3  # algo $ salt_b64 $ dk_b64

    def test_wrong_password_rejected(self):
        h = auth.scrypt_hash("right")
        assert auth.scrypt_verify("wrong", h) is False
        assert auth.scrypt_verify("", h) is False

    def test_random_salt_makes_each_hash_unique(self):
        h1 = auth.scrypt_hash("same-password")
        h2 = auth.scrypt_hash("same-password")
        assert h1 != h2
        # but both verify
        assert auth.scrypt_verify("same-password", h1)
        assert auth.scrypt_verify("same-password", h2)

    def test_explicit_salt_is_deterministic(self):
        salt = b"\x01" * 16
        assert auth.scrypt_hash("p", salt=salt) == auth.scrypt_hash("p", salt=salt)

    def test_verify_rejects_bogus_stored_format(self):
        for bad in (
            "",
            "not-a-hash",
            "bcrypt$abc$def",          # wrong algo
            "scrypt$only-two-fields",
            "scrypt$bad-b64!!!$alsobad!!!",
        ):
            assert auth.scrypt_verify("any", bad) is False

    def test_default_hash_verifies_default_password(self):
        # The shipped default ("llmadmin"/"llmadmin") must round-trip via
        # the auth.DEFAULT_AUTH_HASH populated by register_auth — fresh
        # installs depend on it.
        assert auth.scrypt_verify(auth.DEFAULT_AUTH_PASSWORD, auth.DEFAULT_AUTH_HASH) is True


# ── auth_runtime / auth_write ────────────────────────────────────────────────

@pytest.fixture
def temp_auth_file(tmp_path, monkeypatch):
    """Point auth.MANAGER_AUTH_FILE at a sandbox so we never touch the live install."""
    p = tmp_path / "manager_auth.json"
    monkeypatch.setattr(auth, "MANAGER_AUTH_FILE", p)
    return p


class TestAuthRuntime:
    def test_returns_empty_when_file_missing(self, temp_auth_file):
        assert auth.auth_runtime() == {}

    def test_returns_empty_when_file_unreadable(self, temp_auth_file):
        temp_auth_file.write_text("{not valid json")
        # auth_runtime catches JSON errors and returns {} rather than crashing.
        assert auth.auth_runtime() == {}

    def test_returns_parsed_json(self, temp_auth_file):
        payload = {"mode": "disabled", "username": "alice"}
        temp_auth_file.write_text(json.dumps(payload))
        assert auth.auth_runtime() == payload


class TestAuthWrite:
    def test_writes_new_file(self, temp_auth_file):
        auth.auth_write({"username": "ops"})
        data = json.loads(temp_auth_file.read_text())
        assert data["username"] == "ops"
        assert "updated_at" in data  # auto-stamped

    def test_creates_file_with_0600_mode(self, temp_auth_file):
        auth.auth_write({"username": "ops"})
        st = temp_auth_file.stat()
        assert (st.st_mode & 0o777) == 0o600

    def test_merges_with_existing_keys(self, temp_auth_file):
        # Pre-existing key should survive a partial write — that's the whole
        # reason auth_write does read-modify-write under a lock.
        temp_auth_file.write_text(json.dumps({"mode": "trusted_cidr",
                                              "kept_extra": "yes"}))
        auth.auth_write({"username": "ops"})
        data = json.loads(temp_auth_file.read_text())
        assert data["mode"] == "trusted_cidr"
        assert data["username"] == "ops"
        assert data["kept_extra"] == "yes"


# ── auth_credential precedence ──────────────────────────────────────────────

class TestAuthCredential:
    def test_falls_back_to_default_when_nothing_configured(
        self, temp_auth_file, monkeypatch
    ):
        # No JSON file, TOML has no password_hash → return built-in default
        monkeypatch.setattr(M.settings.manager.auth, "password_hash", "")
        monkeypatch.setattr(M.settings.manager.auth, "username", "")
        user, h, is_default = auth.auth_credential()
        assert user == auth.DEFAULT_AUTH_USER
        assert h == auth.DEFAULT_AUTH_HASH
        assert is_default is True

    def test_toml_wins_over_default(self, temp_auth_file, monkeypatch):
        # TOML has a real hash; JSON is empty → TOML used
        h = auth.scrypt_hash("toml-pw")
        monkeypatch.setattr(M.settings.manager.auth, "password_hash", h)
        monkeypatch.setattr(M.settings.manager.auth, "username", "from_toml")
        user, got_hash, is_default = auth.auth_credential()
        assert user == "from_toml"
        assert got_hash == h
        assert is_default is False

    def test_json_wins_over_toml(self, temp_auth_file, monkeypatch):
        # JSON runtime override should beat TOML
        h_toml = auth.scrypt_hash("toml-pw")
        h_json = auth.scrypt_hash("json-pw")
        monkeypatch.setattr(M.settings.manager.auth, "password_hash", h_toml)
        monkeypatch.setattr(M.settings.manager.auth, "username", "from_toml")
        temp_auth_file.write_text(json.dumps({
            "username": "from_json",
            "password_hash": h_json,
        }))
        user, got_hash, is_default = auth.auth_credential()
        assert user == "from_json"
        assert got_hash == h_json
        assert is_default is False

    def test_json_password_inherits_toml_username_when_unset(
        self, temp_auth_file, monkeypatch
    ):
        # Operator changes the password from the admin tab but not the username.
        monkeypatch.setattr(M.settings.manager.auth, "username", "from_toml")
        monkeypatch.setattr(M.settings.manager.auth, "password_hash", "")
        h_json = auth.scrypt_hash("new-pw")
        temp_auth_file.write_text(json.dumps({"password_hash": h_json}))
        user, got_hash, _ = auth.auth_credential()
        # Username comes from TOML; password from JSON.
        assert user == "from_toml"
        assert got_hash == h_json


# ── auth_mode / auth_policy ─────────────────────────────────────────────────

class TestAuthMode:
    def test_policy_defaults_to_required(self, temp_auth_file, monkeypatch):
        # Unset → 'required'
        monkeypatch.setattr(M.settings.manager.auth, "mode", "required")
        assert auth.auth_policy() == "required"

    def test_required_mode_pinned_by_toml(self, temp_auth_file, monkeypatch):
        monkeypatch.setattr(M.settings.manager.auth, "mode", "required")
        # JSON override ignored under non-auto policy
        temp_auth_file.write_text(json.dumps({"mode": "disabled"}))
        assert auth.auth_mode() == "required"

    def test_disabled_mode_pinned_by_toml(self, temp_auth_file, monkeypatch):
        monkeypatch.setattr(M.settings.manager.auth, "mode", "disabled")
        temp_auth_file.write_text(json.dumps({"mode": "required"}))
        assert auth.auth_mode() == "disabled"

    def test_trusted_cidr_mode_pinned_by_toml(self, temp_auth_file, monkeypatch):
        monkeypatch.setattr(M.settings.manager.auth, "mode", "trusted_cidr")
        temp_auth_file.write_text(json.dumps({"mode": "disabled"}))
        assert auth.auth_mode() == "trusted_cidr"

    def test_auto_policy_reads_runtime_mode(self, temp_auth_file, monkeypatch):
        monkeypatch.setattr(M.settings.manager.auth, "mode", "auto")
        temp_auth_file.write_text(json.dumps({"mode": "disabled"}))
        assert auth.auth_mode() == "disabled"

    def test_auto_policy_with_no_json_falls_back_to_required(
        self, temp_auth_file, monkeypatch
    ):
        monkeypatch.setattr(M.settings.manager.auth, "mode", "auto")
        # No JSON file at all
        assert auth.auth_mode() == "required"

    def test_auto_policy_ignores_invalid_runtime_mode(self, temp_auth_file, monkeypatch):
        monkeypatch.setattr(M.settings.manager.auth, "mode", "auto")
        temp_auth_file.write_text(json.dumps({"mode": "weird-unknown-mode"}))
        # Falls back to required since the JSON value isn't a runtime mode.
        assert auth.auth_mode() == "required"
