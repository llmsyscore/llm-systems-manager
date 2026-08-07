"""
Operator-provided manager TLS cert (#523): _custom_manager_tls_files()
resolution and the _ensure_manager_server_cert() bypass.
"""
from __future__ import annotations

from pathlib import Path

import manager_mod as M


def _set(monkeypatch, crt, key):
    monkeypatch.setattr(M.settings.manager, "tls_cert_file", crt, raising=False)
    monkeypatch.setattr(M.settings.manager, "tls_key_file", key, raising=False)


class TestCustomTlsFiles:
    def test_unset_returns_none(self, monkeypatch):
        _set(monkeypatch, "", "")
        assert M._custom_manager_tls_files() is None

    def test_one_of_two_returns_none(self, monkeypatch, tmp_path):
        crt = tmp_path / "c.crt"
        crt.write_text("x")
        _set(monkeypatch, str(crt), "")
        assert M._custom_manager_tls_files() is None

    def test_unreadable_paths_return_none(self, monkeypatch, tmp_path):
        _set(monkeypatch, str(tmp_path / "no.crt"), str(tmp_path / "no.key"))
        assert M._custom_manager_tls_files() is None

    def test_absolute_pair_resolves(self, monkeypatch, tmp_path):
        crt, key = tmp_path / "c.crt", tmp_path / "k.key"
        crt.write_text("x"); key.write_text("y")
        _set(monkeypatch, str(crt), str(key))
        assert M._custom_manager_tls_files() == (crt, key)

    def test_relative_resolves_against_repo_root(self, monkeypatch, tmp_path):
        monkeypatch.setattr(M, "_REPO_ROOT_PATH", tmp_path)
        (tmp_path / "data").mkdir()
        crt, key = tmp_path / "data" / "c.crt", tmp_path / "data" / "k.key"
        crt.write_text("x"); key.write_text("y")
        _set(monkeypatch, "data/c.crt", "data/k.key")
        assert M._custom_manager_tls_files() == (crt, key)

    def test_missing_attrs_on_old_config_return_none(self, monkeypatch):
        monkeypatch.delattr(M.settings.manager, "tls_cert_file", raising=False)
        monkeypatch.delattr(M.settings.manager, "tls_key_file", raising=False)
        assert M._custom_manager_tls_files() is None


class TestEnsureCertBypass:
    def test_custom_cert_skips_auto_issue(self, monkeypatch, tmp_path):
        crt, key = tmp_path / "c.crt", tmp_path / "k.key"
        crt.write_text("x"); key.write_text("y")
        _set(monkeypatch, str(crt), str(key))
        # DATA_DIR pointed at an empty tmp dir: a run past the bypass would
        # try to generate files there; the bypass must leave it untouched.
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.setattr(M, "DATA_DIR", data)
        M._ensure_manager_server_cert()
        assert list(data.iterdir()) == []

    def test_without_custom_cert_auto_issue_runs(self, monkeypatch, tmp_path):
        _set(monkeypatch, "", "")
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.setattr(M, "DATA_DIR", data)
        M._ensure_manager_server_cert()
        assert (data / "manager-tls.crt").is_file()
        assert (data / "manager-tls.key").is_file()
