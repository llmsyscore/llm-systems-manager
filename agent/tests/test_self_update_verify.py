# agent/tests/test_self_update_verify.py
"""#306: unit tests for _verify_tarball_signature(), the agent_self_update()
signature-check helper extracted from llm-systems-agent.py."""
from __future__ import annotations

import base64
import binascii
import re
import subprocess
from pathlib import Path
from typing import Optional

AGENT_DIR = Path(__file__).resolve().parents[1]
AGENT_PY = AGENT_DIR / "llm-systems-agent.py"


def _extract_py_func(source: Path, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", source.read_text(),
                  re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from {source.name}"
    return m.group(0)


def _verify_fn():
    ns = {"os": __import__("os"), "subprocess": subprocess,
          "base64": base64, "binascii": binascii,
          "Path": Path, "Optional": Optional}
    exec(compile(_extract_py_func(AGENT_PY, "_verify_tarball_signature"),
                 str(AGENT_PY), "exec"), ns)
    return ns["_verify_tarball_signature"]


def _make_ca(d: Path) -> tuple[Path, Path]:
    key = d / "cakey.pem"
    cert = d / "tls-ca.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "2",
                    "-subj", "/CN=test-ca"], check=True, capture_output=True)
    return key, cert


def _sign(tarball: Path, key: Path) -> str:
    sig = tarball.with_suffix(".sig")
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key),
                    "-out", str(sig), str(tarball)], check=True, capture_output=True)
    return base64.b64encode(sig.read_bytes()).decode()


def test_valid_signature_passes(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMSYS_ALLOW_INSECURE_UPDATE", raising=False)
    key, ca = _make_ca(tmp_path)
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    sig_b64 = _sign(tgz, key)
    verify_dir = tmp_path / "verify"; verify_dir.mkdir()
    ok, reason = _verify_fn()(str(tgz), sig_b64, ca, str(verify_dir))
    assert ok, reason
    assert "verified" in reason


def test_tampered_tarball_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMSYS_ALLOW_INSECURE_UPDATE", raising=False)
    key, ca = _make_ca(tmp_path)
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    sig_b64 = _sign(tgz, key)
    tgz.write_bytes(b"payload-bytesX")  # tamper after signing
    verify_dir = tmp_path / "verify"; verify_dir.mkdir()
    ok, reason = _verify_fn()(str(tgz), sig_b64, ca, str(verify_dir))
    assert not ok
    assert "FAILED" in reason


def test_missing_header_aborts(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMSYS_ALLOW_INSECURE_UPDATE", raising=False)
    _key, ca = _make_ca(tmp_path)
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    verify_dir = tmp_path / "verify"; verify_dir.mkdir()
    ok, reason = _verify_fn()(str(tgz), None, ca, str(verify_dir))
    assert not ok
    assert "did not sign" in reason


def test_malformed_base64_aborts(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMSYS_ALLOW_INSECURE_UPDATE", raising=False)
    _key, ca = _make_ca(tmp_path)
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    verify_dir = tmp_path / "verify"; verify_dir.mkdir()
    ok, reason = _verify_fn()(str(tgz), "not-valid-base64!!!", ca, str(verify_dir))
    assert not ok
    assert "malformed" in reason


def test_missing_ca_insecure_optout_passes_with_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMSYS_ALLOW_INSECURE_UPDATE", "1")
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    missing_ca = tmp_path / "nope.pem"
    verify_dir = tmp_path / "verify"; verify_dir.mkdir()
    ok, reason = _verify_fn()(str(tgz), None, missing_ca, str(verify_dir))
    assert ok
    assert "no pinned CA" in reason and "skipping" in reason


def test_missing_ca_without_optout_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMSYS_ALLOW_INSECURE_UPDATE", raising=False)
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    missing_ca = tmp_path / "nope.pem"
    verify_dir = tmp_path / "verify"; verify_dir.mkdir()
    ok, reason = _verify_fn()(str(tgz), None, missing_ca, str(verify_dir))
    assert not ok
    assert "cannot verify update signature" in reason
