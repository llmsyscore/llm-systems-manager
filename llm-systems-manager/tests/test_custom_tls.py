"""
Operator-provided manager TLS cert (#523): _custom_manager_tls_files()
resolution/guards, SAN extraction, and SNI hostname matching.
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

    def test_one_of_two_returns_none_and_warns(self, monkeypatch, tmp_path, caplog):
        crt = tmp_path / "c.crt"
        crt.write_text("x")
        _set(monkeypatch, str(crt), "")
        with caplog.at_level("WARNING"):
            assert M._custom_manager_tls_files() is None
        assert any("only one of" in r.message for r in caplog.records)

    def test_quiet_suppresses_warnings(self, monkeypatch, tmp_path, caplog):
        _set(monkeypatch, str(tmp_path / "c.crt"), "")
        with caplog.at_level("WARNING"):
            assert M._custom_manager_tls_files(quiet=True) is None
        assert not caplog.records

    def test_unreadable_paths_return_none(self, monkeypatch, tmp_path):
        _set(monkeypatch, str(tmp_path / "no.crt"), str(tmp_path / "no.key"))
        assert M._custom_manager_tls_files() is None

    def test_absolute_pair_resolves(self, monkeypatch, tmp_path):
        crt, key = tmp_path / "c.crt", tmp_path / "k.key"
        crt.write_text("x"); key.write_text("y")
        key.chmod(0o600)
        _set(monkeypatch, str(crt), str(key))
        assert M._custom_manager_tls_files() == (crt, key)

    def test_relative_resolves_against_repo_root(self, monkeypatch, tmp_path):
        monkeypatch.setattr(M, "_REPO_ROOT_PATH", tmp_path)
        (tmp_path / "data").mkdir()
        crt, key = tmp_path / "data" / "c.crt", tmp_path / "data" / "k.key"
        crt.write_text("x"); key.write_text("y")
        key.chmod(0o600)
        _set(monkeypatch, "data/c.crt", "data/k.key")
        assert M._custom_manager_tls_files() == (crt, key)

    def test_lax_key_perms_warn_but_still_serve(self, monkeypatch, tmp_path, caplog):
        crt, key = tmp_path / "c.crt", tmp_path / "k.key"
        crt.write_text("x"); key.write_text("y")
        key.chmod(0o644)
        _set(monkeypatch, str(crt), str(key))
        with caplog.at_level("WARNING"):
            assert M._custom_manager_tls_files() == (crt, key)
        assert any("group/world-readable" in r.message for r in caplog.records)

    def test_missing_attrs_on_old_config_return_none(self, monkeypatch):
        monkeypatch.delattr(M.settings.manager, "tls_cert_file", raising=False)
        monkeypatch.delattr(M.settings.manager, "tls_key_file", raising=False)
        assert M._custom_manager_tls_files() is None


class TestEnsureCertStillRuns:
    def test_auto_issue_runs_even_with_custom_cert(self, monkeypatch, tmp_path):
        # Internal cert must keep existing: CA-pinned agents get it via SNI default.
        crt, key = tmp_path / "c.crt", tmp_path / "k.key"
        crt.write_text("x"); key.write_text("y")
        key.chmod(0o600)
        _set(monkeypatch, str(crt), str(key))
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.setattr(M, "DATA_DIR", data)
        M._ensure_manager_server_cert()
        assert (data / "manager-tls.crt").is_file()
        assert (data / "manager-tls.key").is_file()


class TestSanExtraction:
    def test_reads_dns_sans_from_generated_cert(self, monkeypatch, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        monkeypatch.setattr(M, "DATA_DIR", data)
        _set(monkeypatch, "", "")
        M._ensure_manager_server_cert()
        sans = M._cert_san_hostnames(data / "manager-tls.crt")
        assert "localhost" in sans

    def test_garbage_returns_empty(self, tmp_path):
        p = tmp_path / "junk.crt"
        p.write_text("not a cert")
        assert M._cert_san_hostnames(p) == []


class TestSniMatches:
    def test_exact_match(self):
        assert M._sni_matches("devmgr.example.com", ["devmgr.example.com"])

    def test_case_and_trailing_dot(self):
        assert M._sni_matches("DevMgr.Example.COM.", ["devmgr.example.com"])

    def test_wildcard_one_label(self):
        assert M._sni_matches("devmgr.example.com", ["*.example.com"])

    def test_wildcard_does_not_cross_labels(self):
        assert not M._sni_matches("a.b.example.com", ["*.example.com"])

    def test_wildcard_does_not_match_apex(self):
        assert not M._sni_matches("example.com", ["*.example.com"])

    def test_no_servername_is_no_match(self):
        assert not M._sni_matches(None, ["*.example.com"])
        assert not M._sni_matches("", ["*.example.com"])

    def test_unrelated_host_is_no_match(self):
        assert not M._sni_matches("192.168.1.10", ["*.example.com"])
