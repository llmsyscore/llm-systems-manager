from __future__ import annotations

import base64
import io
import tarfile

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

import agent_registry  # backend dir is on sys.path via conftest
import auth
import manager_mod as M
from _pki import load_or_create_ca


def test_build_agent_tarball_bytes_contains_agent():
    tgz = agent_registry._build_agent_tarball_bytes()
    assert isinstance(tgz, bytes) and len(tgz) > 0
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as t:
        names = t.getnames()
    assert any(n == "agent" or n.startswith("agent/") for n in names)


def test_sign_tarball_verifies_against_ca(tmp_path):
    _cert, key = load_or_create_ca(tmp_path)
    data = b"hello-tarball-bytes"
    sig = base64.b64decode(agent_registry._sign_tarball(data, key))
    key.public_key().verify(sig, data, padding.PKCS1v15(), hashes.SHA256())


def test_sign_tarball_rejects_tampered(tmp_path):
    _cert, key = load_or_create_ca(tmp_path)
    sig = base64.b64decode(agent_registry._sign_tarball(b"orig", key))
    with pytest.raises(InvalidSignature):
        key.public_key().verify(sig, b"orig-tampered", padding.PKCS1v15(), hashes.SHA256())


# ── View-level: GET /api/agent-tarball ─────────────────────────────────────

@pytest.fixture
def client():
    M.app.config.update(TESTING=True)
    return M.app.test_client()


def _fake_approved_agent(monkeypatch):
    """Point both the before_request agent-token gate (auth._agent_by_token,
    bound once at startup) and the route's own lookup at the same fake."""
    fn = lambda tok: {"agent_id": "ag1", "status": "approved"} if tok == "good" else None
    monkeypatch.setattr(agent_registry, "agent_by_token", fn, raising=False)
    monkeypatch.setattr(auth, "_agent_by_token", fn, raising=False)


def test_agent_tarball_401_without_valid_token(client, monkeypatch):
    _fake_approved_agent(monkeypatch)
    r = client.get("/api/agent-tarball")
    assert r.status_code == 401
    r = client.get("/api/agent-tarball", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401


def test_agent_tarball_200_signed_and_verifies(client, monkeypatch, tmp_path):
    _fake_approved_agent(monkeypatch)
    r = client.get("/api/agent-tarball", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.data.startswith(b"\x1f\x8b")
    assert r.headers.get("X-Agent-Tarball-Sig-Alg") == "rsa-pkcs1-sha256"
    sig_b64 = r.headers.get("X-Agent-Tarball-Sig")
    assert sig_b64

    ca_cert, ca_key, _pki = agent_registry._deps.pki_ensure_ca()
    sig = base64.b64decode(sig_b64)
    ca_cert.public_key().verify(sig, r.data, padding.PKCS1v15(), hashes.SHA256())
    with tarfile.open(fileobj=io.BytesIO(r.data), mode="r:gz") as t:
        names = t.getnames()
    assert any(n == "agent" or n.startswith("agent/") for n in names)


def test_agent_tarball_500_when_signing_fails(client, monkeypatch):
    _fake_approved_agent(monkeypatch)

    def _boom():
        raise RuntimeError("pki unavailable")
    monkeypatch.setattr(agent_registry._deps, "pki_ensure_ca", _boom, raising=False)
    r = client.get("/api/agent-tarball", headers={"Authorization": "Bearer good"})
    assert r.status_code == 500
    assert r.get_json()["ok"] is False
