"""Host-side admin password tool: status and the temporary-password reset."""
from __future__ import annotations

import io

import pytest

import admin_password as ap
import auth
import manager_users


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "data" / "manager_users.json"
    path.parent.mkdir()
    monkeypatch.setattr(ap, "users_file", lambda: path)
    monkeypatch.setattr(ap, "_toml_auth", lambda: {"username": "llmadmin"})
    manager_users.init(path, threshold=5, window_s=60, duration_s=60)
    return manager_users.STORE


def _run(capsys, *argv):
    code = ap.main(list(argv))
    out = capsys.readouterr()
    return code, out.out.strip(), out.err.strip()


def test_status_of_an_empty_store(store, capsys, monkeypatch):
    assert _run(capsys, "status") == (0, "empty", "")
    monkeypatch.setattr(ap, "_toml_auth", lambda: {"username": "llmadmin", "password_hash": "scrypt$a$b"})
    assert _run(capsys, "status") == (0, "ok", "")


def test_reset_creates_the_admin_with_a_password_that_signs_in_once_changed(store, capsys):
    code, out, _ = _run(capsys, "reset", "--yes", "--porcelain")
    name, password = out.split()
    assert code == 0 and name == "llmadmin" and len(password) >= manager_users.MIN_PASSWORD
    u = store.get("llmadmin")
    assert u["role"] == "admin" and u["must_change"] is True and password not in str(u)
    assert manager_users.authenticate("llmadmin", password, "127.0.0.1")["ok"] is True
    assert _run(capsys, "status") == (0, "ok", "")


def test_each_reset_gives_a_new_password(store, capsys):
    first = _run(capsys, "reset", "--yes", "--porcelain")[1].split()[1]
    second = _run(capsys, "reset", "--yes", "--porcelain")[1].split()[1]
    assert first != second
    assert manager_users.authenticate("llmadmin", first, "127.0.0.1")["ok"] is False
    assert manager_users.authenticate("llmadmin", second, "127.0.0.1")["ok"] is True


OLD = auth.scrypt_hash(auth.DEFAULT_AUTH_USER)


def test_reset_picks_every_admin_that_needs_it(store, capsys):
    store.create("ops", OLD, "admin")
    store.create("ops2", OLD, "admin")
    store.create("other", auth.scrypt_hash("other-password"), "admin")
    assert _run(capsys, "status") == (0, "reset-required", "")
    assert store.retire(auth.uses_retired_password) == ["ops", "ops2"]
    assert _run(capsys, "status") == (0, "reset-required", "")
    code, out, _ = _run(capsys, "reset", "--yes", "--porcelain")
    lines = [ln.split() for ln in out.splitlines()]
    assert code == 0 and [ln[0] for ln in lines] == ["ops", "ops2"] and lines[0][1] != lines[1][1]
    for name, password in lines:
        u = store.get(name)
        assert u["must_change"] is True and "reset_required" not in u
        assert manager_users.authenticate(name, password, "127.0.0.1")["ok"] is True
    assert store.get("llmadmin") is None
    assert manager_users.authenticate("other", "other-password", "127.0.0.1")["ok"] is True
    assert _run(capsys, "status") == (0, "ok", "")


def test_disabled_and_operator_accounts_are_not_picked(store, capsys):
    store.create("gone", OLD, "admin")
    store.create("alice", auth.scrypt_hash("alice-password"), "admin")
    store.set_disabled("gone", True)
    store.create("viewer", OLD, "operator")
    assert _run(capsys, "status") == (0, "ok", "")
    assert sorted(store.retire(auth.uses_retired_password)) == ["gone", "viewer"]
    assert _run(capsys, "status") == (0, "ok", "") and not store.needs_reset()
    assert store.get("gone")["disabled"] is True


def test_reset_of_a_named_disabled_account_enables_it(store, capsys):
    store.create("alice", auth.scrypt_hash("alice-password"), "admin")
    store.create("gone", OLD, "admin")
    store.set_disabled("gone", True)
    code, out, _ = _run(capsys, "reset", "--yes", "--porcelain", "--user", "gone")
    assert code == 0 and out.split()[0] == "gone" and store.get("gone")["disabled"] is False


def test_status_when_the_only_admin_is_disabled_or_missing(store, capsys):
    store.create("viewer", auth.scrypt_hash("viewer-password"), "operator")
    assert _run(capsys, "status") == (0, "reset-required", "")
    code, out, _ = _run(capsys, "reset", "--yes", "--porcelain")
    assert code == 0 and out.split()[0] == "llmadmin" and store.get("llmadmin")["role"] == "admin"


def test_reset_does_not_add_an_admin_beside_a_working_one(store, capsys):
    store.create("alice", auth.scrypt_hash("alice-password"), "admin")
    code, _, err = _run(capsys, "reset", "--yes", "--porcelain")
    assert code == 2 and "--user" in err and "alice" in err and store.get("llmadmin") is None
    code, out, _ = _run(capsys, "reset", "--yes", "--porcelain", "--user", "alice")
    assert code == 0 and out.split()[0] == "alice"


def test_reset_needs_the_data_directory(store, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "users_file", lambda: tmp_path / "missing" / "manager_users.json")
    code, _, err = _run(capsys, "reset", "--yes")
    assert code == 2 and "does not exist" in err and not (tmp_path / "missing").exists()


def test_legacy_credential_counts_as_provisioned(store, capsys, tmp_path):
    (tmp_path / "data" / "manager_auth.json").write_text('{"password_hash": "scrypt$a$b"}')
    assert _run(capsys, "status") == (0, "ok", "")


def test_reset_names_another_user(store, capsys):
    code, out, _ = _run(capsys, "reset", "--yes", "--porcelain", "--user", "Second.Admin")
    assert code == 0 and out.split()[0] == "second.admin"
    assert _run(capsys, "reset", "--yes", "--user", "bad name")[0] == 2


def test_reset_without_a_terminal_needs_yes(store, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    code, out, err = _run(capsys, "reset")
    assert code == 2 and "--yes" in err and store.is_empty()


def test_reset_asks_first_and_stops_on_no(store, capsys, monkeypatch):
    class _Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr("sys.stdin", _Tty("n\n"))
    code, out, _ = _run(capsys, "reset")
    assert code == 1 and "Nothing changed." in out and store.is_empty()
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    code, out, _ = _run(capsys, "reset")
    assert code == 0 and "Temporary password:" in out and not store.is_empty()


def test_configured_user_reads_the_toml(tmp_path, monkeypatch):
    cfg = tmp_path / "llm-systems.toml"
    cfg.write_text('[manager.auth]\nusername = "opsadmin"\n')
    monkeypatch.setenv("LLM_SYSTEMS_CONFIG", str(cfg))
    assert ap.configured_user() == "opsadmin"
    monkeypatch.setenv("LLM_SYSTEMS_CONFIG", str(tmp_path / "missing.toml"))
    assert ap.configured_user() == auth.DEFAULT_AUTH_USER
