"""#1204: the manager signs an agent's own CSR and sends no private key; a bad CSR falls back."""
from __future__ import annotations

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import _pki
import agent_registry

AID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    return _pki.load_or_create_ca(tmp_path_factory.mktemp("ca"))


def _csr(key=None) -> tuple:
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ignored")]))
           .sign(key, hashes.SHA256()))
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


def _pub(obj) -> bytes:
    return obj.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


def test_sign_with_public_key_returns_no_private_key(ca):
    key, _ = _csr()
    pem, key_pem = _pki.sign_agent_cert(ca[0], ca[1], agent_id=AID, hostname="box",
                                        ip_san="10.0.0.5", public_key=key.public_key())
    assert key_pem is None
    cert = x509.load_pem_x509_certificate(pem.encode())
    assert _pub(cert) == _pub(key)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == AID


def test_sign_without_public_key_keeps_generating(ca):
    pem, key_pem = _pki.sign_agent_cert(ca[0], ca[1], agent_id=AID, hostname="box", ip_san="10.0.0.5")
    assert key_pem and "PRIVATE KEY" in key_pem


@pytest.fixture
def deps(monkeypatch, ca, tmp_path):
    monkeypatch.setattr(agent_registry._deps, "pki_ensure_ca", lambda: (ca[0], ca[1], _pki))
    monkeypatch.setattr(agent_registry._deps, "data_dir", tmp_path)
    (tmp_path / "internal-ca.crt").write_text(ca[0].public_bytes(serialization.Encoding.PEM).decode())
    monkeypatch.setattr(agent_registry, "_agent_cert_sans", lambda agent, warn=False: ("box", ["10.0.0.5"]))
    return {"agent_id": AID, "hostname": "box", "registered_from": "10.0.0.5", "bind_url": "http://10.0.0.5:8765"}


def test_build_uses_csr_public_key(deps):
    key, csr_pem = _csr()
    out = agent_registry._build_and_sign_agent_cert(deps, csr_pem=csr_pem)
    assert "key_pem" not in out
    assert _pub(x509.load_pem_x509_certificate(out["cert_pem"].encode())) == _pub(key)
    assert out["ca_pem"].startswith("-----BEGIN CERTIFICATE-----")


def test_build_falls_back_on_bad_csr(deps):
    out = agent_registry._build_and_sign_agent_cert(deps, csr_pem="-----BEGIN CERTIFICATE REQUEST-----\nnope\n-----END CERTIFICATE REQUEST-----\n")
    assert out["key_pem"] and "PRIVATE KEY" in out["key_pem"]


def test_build_falls_back_on_unsupported_key_type(deps):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ignored")]))
           .sign(key, None))
    out = agent_registry._build_and_sign_agent_cert(deps, csr_pem=csr.public_bytes(serialization.Encoding.PEM).decode())
    assert out["key_pem"] and "PRIVATE KEY" in out["key_pem"]


def test_build_falls_back_on_small_rsa_key(deps):
    key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    _, csr_pem = _csr(key)
    out = agent_registry._build_and_sign_agent_cert(deps, csr_pem=csr_pem)
    assert out["key_pem"] and "PRIVATE KEY" in out["key_pem"]
    assert _pub(x509.load_pem_x509_certificate(out["cert_pem"].encode())) != _pub(key)


def test_heartbeat_bundle_passes_csr_through(deps, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent_registry, "_build_and_sign_agent_cert",
                        lambda agent, csr_pem=None: seen.update(csr=csr_pem) or {"cert_pem": "c", "ca_pem": "a", "expires_at": "x"})
    monkeypatch.setattr(agent_registry._deps.settings.manager.security, "tls_rotation_warn_days", 30)
    out = agent_registry._maybe_issue_tls_bundle(dict(deps), {"has_tls_cert": False, "csr_pem": "CSR"})
    assert seen["csr"] == "CSR" and out["reason"] == "first-issue" and "key_pem" not in out
