"""Internal CA + per-agent cert signing for Phase 4 #3 mutual TLS.

The manager owns the only CA. On first boot it self-signs a root cert
and writes the keypair to `data/internal-ca.crt` and `data/internal-ca.key`
(mode 0600). On each agent approval (or on demand via the admin
endpoint) it signs a 1-year leaf cert for the agent with:
  - CN = <agent_id>
  - SAN: DNS=<hostname>.agents.local + IP=<registered_from>

The agent stores cert+key, serves TLS on a second port (8443), and the
manager's outbound calls prefer https:// over http:// when the agent's
registered bind_url has a TLS counterpart. Bearer auth still primary;
TLS is transport-only.

Symbols exported here are intentionally narrow:
  - load_or_create_ca(data_dir)  -> (ca_cert, ca_key)
  - sign_agent_cert(ca_cert, ca_key, agent_id, hostname, ip_san, days=365)
        -> (cert_pem_str, key_pem_str)
  - ca_bundle_pem(data_dir) -> str  (the CA cert as PEM; agents trust this)
  - validate_cert_against_ca(cert_pem, ca_cert) -> bool

Everything else is a private helper.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path
from typing import Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
from cryptography.x509.oid import NameOID

__all__ = [
    "load_or_create_ca",
    "sign_agent_cert",
    "ca_bundle_pem",
    "validate_cert_against_ca",
    "AKI_FIX_TS",
]

# ── PKI format invariant ──────────────────────────────────────────────
# Certs issued before this timestamp need a one-time auto-reissue. Bump
# this whenever the signing/SAN logic changes in a way that requires
# existing certs to be re-signed. Three issuers consult this:
#   - agent_registry's heartbeat-driven TLS bundle reissue
#   - the manager's own HTTPS server cert (data/manager-tls.{crt,key})
#   - the AE TLS cert (data/ae-tls.{crt,key})
# All three live in different modules, so the constant belongs here in
# the canonical PKI module rather than in any one of them.
#
#   2026-05-13 01:00 UTC — added AuthorityKeyIdentifier (commit 43bd69a)
#   2026-05-13 01:30 UTC — SAN IP now derived from bind_url, not the
#       potentially stale registered_from field (commit 189a329)
#   2026-05-13 05:35 UTC — bumped to force-reissue certs issued by an
#       intermediate manager version that had AKI but still used
#       registered_from for the SAN IP. Time-window: 01:30 → 05:35.
import datetime as _dt
AKI_FIX_TS = _dt.datetime(2026, 5, 13, 5, 35, tzinfo=_dt.timezone.utc)

# ── CA key/cert names (relative to data/) ─────────────────────────────
_CA_CERT_NAME = "internal-ca.crt"
_CA_KEY_NAME  = "internal-ca.key"
# How long the root cert lives. Long enough that we don't fight cert
# rotation churn during normal operation; short enough that a leak is
# eventually self-healing.
_CA_VALIDITY_DAYS = 365 * 10
# Leaf certs are issued per-agent; default lifetime in days.
_LEAF_VALIDITY_DAYS = 365


def _generate_rsa_key() -> rsa.RSAPrivateKey:
    # 2048 is fine for a LAN-internal CA. 3072+ adds CPU cost without
    # meaningful security gain at this scope.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _build_ca_cert(ca_key: rsa.RSAPrivateKey) -> x509.Certificate:
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LLM Systems Manager"),
        x509.NameAttribute(NameOID.COMMON_NAME, "llm-systems-manager Internal CA"),
    ])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=_CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
    )
    return builder.sign(private_key=ca_key, algorithm=hashes.SHA256())


def load_or_create_ca(data_dir: Path) -> Tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Return the manager's CA cert + key, generating them on first call.

    Files are written with mode 0600 (key) and 0644 (cert). Re-reads
    don't re-generate — operator-visible churn is bad for trust roots.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    crt_path = data_dir / _CA_CERT_NAME
    key_path = data_dir / _CA_KEY_NAME

    if crt_path.exists() and key_path.exists():
        crt = x509.load_pem_x509_certificate(crt_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        return crt, key  # type: ignore[return-value]

    # Generate fresh CA. This is a one-time event for the lifetime of
    # the manager VM — file persistence is what keeps it stable across
    # restarts.
    key = _generate_rsa_key()
    crt = _build_ca_cert(key)

    # Write key first, then cert. Order doesn't matter for correctness,
    # but writing the cert first then crashing would leave us with a
    # cert nobody can use.
    key_path.write_bytes(
        key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    crt_path.write_bytes(crt.public_bytes(Encoding.PEM))
    os.chmod(crt_path, 0o644)
    return crt, key


def sign_agent_cert(
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    agent_id: str,
    hostname: str,
    ip_san: str,
    days: int = _LEAF_VALIDITY_DAYS,
    extra_dns_sans: "list[str] | None" = None,
    extra_ip_sans:  "list[str] | None" = None,
) -> Tuple[str, str]:
    """Sign a leaf cert for one agent. Returns (cert_pem, key_pem) — both
    strings ready for JSON transport.

    SAN includes the agent's hostname (as DNS) and registered IP (as
    IPAddress) so manager-side verification works regardless of how the
    agent's bind_url is expressed.

    Manager-side callers pass extra_dns_sans=["localhost"] +
    extra_ip_sans=["127.0.0.1"] so curl/openssl against localhost works
    without a -k flag.
    """
    leaf_key = _generate_rsa_key()
    now = datetime.datetime.now(datetime.timezone.utc)

    san_entries: list[x509.GeneralName] = []
    # DNS name — synthetic .agents.local TLD keeps the cert from
    # claiming any public hostname.
    san_entries.append(x509.DNSName(f"{hostname}.agents.local"))
    san_entries.append(x509.DNSName(hostname))  # also accept bare hostname
    for dns in (extra_dns_sans or []):
        if dns and dns not in (hostname, f"{hostname}.agents.local"):
            san_entries.append(x509.DNSName(dns))
    try:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(ip_san)))
    except (ValueError, TypeError):
        # If the operator supplied something funky as `registered_from`
        # we'd rather skip the IP SAN than fail outright. Manager-side
        # connections via raw IP will fail verification cleanly with a
        # log line instead of issuing a broken cert.
        pass
    for ip in (extra_ip_sans or []):
        if not ip:
            continue
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except (ValueError, TypeError):
            continue

    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LLM Systems Agent"),
        x509.NameAttribute(NameOID.COMMON_NAME, agent_id),
    ])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                # serverAuth: agent is the TLS server (manager dials it)
                # clientAuth: future mTLS — if we want the agent to
                # present its cert when calling back into the manager.
                x509.ObjectIdentifier("1.3.6.1.5.5.7.3.1"),  # serverAuth
                x509.ObjectIdentifier("1.3.6.1.5.5.7.3.2"),  # clientAuth
            ]),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        # OpenSSL 3.x (Python 3.13+) rejects leaf certs that lack an
        # Authority Key Identifier referencing the CA's Subject Key
        # Identifier with: "[SSL: CERTIFICATE_VERIFY_FAILED] Missing
        # Authority Key Identifier". Pin the AKI to the CA's SKI so
        # standard chain verification succeeds.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_cert.public_key()  # type: ignore[arg-type]
            ),
            critical=False,
        )
        # Also publish our own SKI — not strictly required for a leaf
        # but it's what every well-formed cert ships and it makes
        # `openssl x509 -text` output match expectations.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
    )
    crt = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())

    cert_pem = crt.public_bytes(Encoding.PEM).decode("ascii")
    key_pem = leaf_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


def ca_bundle_pem(data_dir: Path) -> str:
    """Return the CA cert as PEM text. Agents cache this and use it as
    their trust root when calling back to the manager."""
    crt_path = Path(data_dir) / _CA_CERT_NAME
    return crt_path.read_text() if crt_path.exists() else ""


def validate_cert_against_ca(cert_pem: str, ca_cert: x509.Certificate) -> bool:
    """Best-effort signature + expiry check. Used by the manager when
    accepting a cert presented by an agent or stored in the registry.

    Returns False on any error (cert can't parse, signature invalid,
    expired, etc.). Caller should log the reason if needed — we don't
    raise because cert validation runs on every outbound call and
    spam-throwing exceptions is unhelpful.
    """
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
        # Verify signature against the CA's public key. RSA verify
        # requires padding + hash; we sign with PKCS1v15 + SHA-256 in
        # both _build_ca_cert and sign_agent_cert.
        ca_cert.public_key().verify(  # type: ignore[union-attr]
            cert.signature,
            cert.tbs_certificate_bytes,
            padding.PKCS1v15(),
            cert.signature_hash_algorithm,  # type: ignore[arg-type]
        )
        # Window check.
        now = datetime.datetime.now(datetime.timezone.utc)
        if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
            return False
        return True
    except Exception:
        return False
