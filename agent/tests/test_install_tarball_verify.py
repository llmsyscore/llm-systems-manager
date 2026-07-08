from __future__ import annotations

import re
import subprocess
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = AGENT_DIR / "install" / "install.sh"


def _extract_fn() -> str:
    text = INSTALL_SH.read_text()
    m = re.search(r"^_verify_tarball_sig\(\) \{.*?^\}", text, re.S | re.M)
    assert m, "could not extract _verify_tarball_sig from install.sh"
    return m.group(0)


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
    import base64
    return base64.b64encode(sig.read_bytes()).decode()


def _run(fn: str, tarball: Path, headers: Path, ca: Path, allow: str) -> int:
    script = f'{fn}\n_verify_tarball_sig "{tarball}" "{headers}" "{ca}" "{allow}"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode


def test_valid_signature_accepts(tmp_path):
    key, ca = _make_ca(tmp_path)
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    hdr = tmp_path / "h"; hdr.write_text(f"X-Agent-Tarball-Sig: {_sign(tgz, key)}\r\n")
    assert _run(_extract_fn(), tgz, hdr, ca, "0") == 0


def test_tampered_tarball_rejects(tmp_path):
    key, ca = _make_ca(tmp_path)
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    sig_b64 = _sign(tgz, key)
    tgz.write_bytes(b"payload-bytesX")  # tamper after signing
    hdr = tmp_path / "h"; hdr.write_text(f"X-Agent-Tarball-Sig: {sig_b64}\r\n")
    assert _run(_extract_fn(), tgz, hdr, ca, "0") == 1


def test_missing_header_rejects(tmp_path):
    _key, ca = _make_ca(tmp_path)
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    hdr = tmp_path / "h"; hdr.write_text("Content-Type: application/gzip\r\n")
    assert _run(_extract_fn(), tgz, hdr, ca, "0") == 1


def test_no_ca_insecure_opt_out_accepts(tmp_path):
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    hdr = tmp_path / "h"; hdr.write_text("Content-Type: application/gzip\r\n")
    missing_ca = tmp_path / "nope.pem"
    assert _run(_extract_fn(), tgz, hdr, missing_ca, "1") == 0


def test_no_ca_mandatory_rejects(tmp_path):
    tgz = tmp_path / "agent.tar.gz"; tgz.write_bytes(b"payload-bytes")
    hdr = tmp_path / "h"; hdr.write_text("Content-Type: application/gzip\r\n")
    missing_ca = tmp_path / "nope.pem"
    assert _run(_extract_fn(), tgz, hdr, missing_ca, "0") == 1
