"""Heartbeat acks carry a per-agent stream key; the manager HMAC secret stays on the manager."""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

import agent_registry
import auth
import manager_mod as M
import sse_daemon

SECRET = b"s" * 32
A, B = "a" * 32, "b" * 32


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(agent_registry._deps, "manager_secret", lambda: SECRET)
    monkeypatch.setattr(M, "_manager_secret", lambda: SECRET)
    return SECRET


def _agent_accepts(key: bytes, aid: str, token: str, path: str) -> bool:
    """Mirror of the agent's _verify_stream_token with `key` as its received secret."""
    expiry, sig = token.split(".", 1)
    want = hmac.new(key, f"{aid}|{path}|{expiry}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def test_agent_keys_differ_per_agent_and_from_the_secret(secret):
    ka, kb = agent_registry._agent_stream_key(A), agent_registry._agent_stream_key(B)
    assert len(ka) == 32 and ka != kb
    assert secret not in (ka, kb)


def test_stream_token_verifies_only_under_its_own_agents_key(secret):
    tok = agent_registry.issue_stream_token(A, "/llama/log/stream", 60)
    assert _agent_accepts(agent_registry._agent_stream_key(A), A, tok, "/llama/log/stream")
    assert not _agent_accepts(agent_registry._agent_stream_key(B), A, tok, "/llama/log/stream")
    assert not _agent_accepts(secret, A, tok, "/llama/log/stream")


def test_agent_key_cannot_mint_a_token_for_another_agent(secret):
    forged = agent_registry._sign_stream_token(agent_registry._agent_stream_key(A), B, "/x", 60)
    assert not _agent_accepts(agent_registry._agent_stream_key(B), B, forged, "/x")


def test_handoff_token_needs_the_manager_secret(secret):
    tok = agent_registry.issue_handoff_token(A, sse_daemon.PATH, 60)
    assert sse_daemon._verify_handoff(tok, A, sse_daemon.PATH, secret) is True
    agent_minted = agent_registry.issue_stream_token(A, sse_daemon.PATH, 60)
    assert sse_daemon._verify_handoff(agent_minted, A, sse_daemon.PATH, secret) is False


@pytest.mark.parametrize("path", ["/ws/alarm", "/ws/openclaw"])
def test_agent_key_cannot_mint_a_ws_ticket(secret, path):
    key = agent_registry._agent_stream_key(A)
    expiry, nonce = int(time.time()) + 60, "00" * 8
    sig = hmac.new(key, f"{M._ws_ticket_subject(path)}|{expiry}|{nonce}".encode(),
                   hashlib.sha256).hexdigest()
    assert M._verify_ws_ticket(f"{expiry}.{nonce}.{sig}", path) is False
    assert M._verify_ws_ticket(M._issue_ws_ticket(path=path), path) is True


def test_heartbeat_ack_carries_the_agent_key_not_the_secret(secret, monkeypatch):
    data = {"agents": {A: {"agent_id": A, "hostname": "h", "status": "approved", "token": "tok"}},
            "global": {}}
    monkeypatch.setattr(agent_registry, "bearer_from_request", lambda: "tok")
    monkeypatch.setattr(agent_registry, "agent_by_token", lambda tok: dict(data["agents"][A]))
    monkeypatch.setattr(auth, "_bearer_from_request", lambda: "tok", raising=False)
    monkeypatch.setattr(auth, "_agent_by_token", lambda tok: dict(data["agents"][A]), raising=False)
    monkeypatch.setattr(agent_registry, "load_agents", lambda: data)
    monkeypatch.setattr(agent_registry, "save_agents", lambda d: None)
    monkeypatch.setattr(agent_registry, "_maybe_issue_tls_bundle", lambda agent, body: None)
    M.app.config["TESTING"] = True
    with M.app.test_client() as c:
        r = c.post("/api/agents/heartbeat", json={})
    assert r.status_code == 200
    sent = r.get_json()["manager_secret"]
    assert sent == agent_registry._agent_stream_key(A).hex()
    assert sent != secret.hex()


def test_signing_key_file_is_not_the_previously_distributed_one():
    assert M.MANAGER_SECRET_FILE.name != "manager_secret"
    assert "data/manager_secret" not in M._MANAGER_EXPORT_FILES
    assert M._file_category("data/manager_secret") is None
    assert M._file_category(f"data/{M.MANAGER_SECRET_FILE.name}") == "identity"
