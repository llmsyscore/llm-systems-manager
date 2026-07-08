from __future__ import annotations

import base64
import io
import tarfile

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

import agent_registry  # backend dir is on sys.path via conftest
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
