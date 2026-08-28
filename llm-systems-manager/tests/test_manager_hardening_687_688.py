"""Manager hardening (#687, #688): proxied model_id can't escape its path
prefix; the HMAC secret is minted once, atomically, and converges across callers."""
from __future__ import annotations

import os

import pytest

import auth
import manager_mod as M


# ── #687 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "x/../../agent/self-update", "../agent/restart", "/etc/passwd",
    "a\\..\\b", "a//b", "./x", "", "x/%2e%2e/y", "%2E%2E/x", "x/%2e/y",
])
def test_traversal_in_path_rejects(bad):
    assert M._traversal_in_path(bad) is True


@pytest.mark.parametrize("ok", ["normal_model", "Qwen/Qwen2.5-GGUF:Q4_K_M", "repo/file.gguf", "a.b"])
def test_traversal_in_path_accepts(ok):
    assert M._traversal_in_path(ok) is False


@pytest.fixture
def capture_proxy(monkeypatch):
    calls = []

    def fake(kind, method, path, **kw):
        calls.append((kind, method, path))
        return M.jsonify({"ok": True}), 200
    monkeypatch.setattr(M.proxies, "proxy_to_primary", fake)
    monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
    return calls


def test_llm_delete_config_rejects_traversal(capture_proxy):
    with M.app.test_client() as c:
        r = c.delete("/api/llm/config/x/../../agent/self-update")
    assert r.status_code == 400
    assert capture_proxy == []


def test_llm_delete_config_rejects_double_encoded_traversal(capture_proxy):
    with M.app.test_client() as c:
        r = c.delete("/api/llm/config/x/%252e%252e/%252e%252e/agent/restart")
    assert r.status_code == 400
    assert capture_proxy == []


def test_llm_delete_config_forwards_normal_id(capture_proxy):
    with M.app.test_client() as c:
        r = c.delete("/api/llm/config/Qwen/Qwen2.5-GGUF:Q4_K_M?delete_cache=true")
    assert r.status_code == 200
    assert capture_proxy == [("llama", "DELETE", "/llama/config/Qwen/Qwen2.5-GGUF:Q4_K_M")]


def test_agent_request_refuses_dot_segments(monkeypatch):
    import agent_registry
    called = []
    monkeypatch.setattr(agent_registry.requests, "request", lambda *a, **k: called.append(a))
    for p in ("/llama/config/../agent/restart", "/llama/config/%2e%2e/agent/restart"):
        resp, tried, err = agent_registry.agent_request("DELETE", {"callback_urls": ["http://a:1"]}, p)
        assert resp is None and tried == [] and "dot segment" in err
    assert called == []


# ── #688 ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    p = tmp_path / "data" / "manager_secret"
    monkeypatch.setattr(M, "MANAGER_SECRET_FILE", p)
    monkeypatch.setattr(M, "_MANAGER_SECRET_CACHE", {})
    return p


def test_manager_secret_persists_0600(secret_file):
    old = os.umask(0o022)
    try:
        s = M._manager_secret()
    finally:
        os.umask(old)
    assert len(s) == 32
    assert secret_file.read_bytes() == s
    assert (secret_file.stat().st_mode & 0o777) == 0o600
    assert M._manager_secret() == s
    assert [q.name for q in secret_file.parent.iterdir()] == ["manager_secret"]


def test_manager_secret_converges_on_concurrent_first_write(secret_file, monkeypatch):
    competitor = b"c" * 32
    real_urandom = os.urandom

    def racing_urandom(n):
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_bytes(competitor)  # another writer lands mid-mint
        return real_urandom(n)
    monkeypatch.setattr(M.os, "urandom", racing_urandom)
    assert M._manager_secret() == competitor
    assert secret_file.read_bytes() == competitor


def test_manager_secret_replaces_empty_file(secret_file):
    secret_file.parent.mkdir(parents=True)
    secret_file.write_bytes(b"")
    s = M._manager_secret()
    assert len(s) == 32 and secret_file.read_bytes() == s
    assert [q.name for q in secret_file.parent.iterdir()] == ["manager_secret"]


def test_manager_secret_falls_back_when_hard_links_unsupported(secret_file, monkeypatch):
    def no_link(src, dst):
        raise OSError(1, "Operation not permitted")
    monkeypatch.setattr(M.os, "link", no_link)
    s = M._manager_secret()
    assert len(s) == 32 and secret_file.read_bytes() == s
    assert [q.name for q in secret_file.parent.iterdir()] == ["manager_secret"]


def test_manager_secret_is_cached_after_first_read(secret_file):
    s = M._manager_secret()
    secret_file.write_bytes(b"z" * 32)
    assert M._manager_secret() == s


def test_manager_secret_sweeps_orphaned_temp_files(secret_file):
    secret_file.parent.mkdir(parents=True)
    (secret_file.parent / ".manager_secret.abc123.tmp").write_bytes(b"o" * 32)
    s = M._manager_secret()
    assert secret_file.read_bytes() == s
    assert [q.name for q in secret_file.parent.iterdir()] == ["manager_secret"]
