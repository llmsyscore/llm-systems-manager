"""Backup/restore archive helpers for the admin tab's
Export / Import workflow.

File format (LSMENC v1):
    bytes 0..5   : magic 'LSMENC'
    byte  6      : version (1)
    byte  7      : flags (bit 0 = encrypted)
    bytes 8..23  : 16-byte salt   (only when encrypted)
    bytes 24..35 : 12-byte nonce  (only when encrypted)
    rest         : tar.gz bytes, or AES-256-GCM(tar.gz_bytes + tag)

The tar.gz inside always contains a top-level manifest.json plus the
component-specific payload files.
"""
from __future__ import annotations

import io
import os
import tarfile
import time
from typing import Optional

MAGIC = b"LSMENC"
VERSION = 1
FLAG_ENCRYPTED = 0x01
HEADER_PLAIN_LEN = 8
SALT_LEN = 16
NONCE_LEN = 12
MIN_PASSWORD_LEN = 12

# scrypt parameters — ~128 MB RAM, ~0.5s on a modern x86 host. Slow
# enough to make offline brute force expensive; fast enough that
# import doesn't feel sluggish.
SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1


def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R,
                  p=SCRYPT_P).derive(password.encode("utf-8"))


def pack_tar(files: dict[str, bytes]) -> bytes:
    """Build a tar.gz from a {arcname: bytes} mapping. mtime is fixed
    to now; mode is 0600 (file contents may include secrets)."""
    now = int(time.time())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in sorted(files):
            data = files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = now
            info.mode = 0o600
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack_tar(blob: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            out[member.name] = f.read()
    return out


def encrypt(payload: bytes, password: Optional[str]) -> bytes:
    """Wrap payload in the LSMENC envelope; encrypt with AES-256-GCM
    when a non-empty password is supplied."""
    if not password:
        return MAGIC + bytes([VERSION, 0]) + payload
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(
            f"password must be at least {MIN_PASSWORD_LEN} characters")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(password, salt)
    ct = AESGCM(key).encrypt(nonce, payload, None)
    return MAGIC + bytes([VERSION, FLAG_ENCRYPTED]) + salt + nonce + ct


def decrypt(blob: bytes, password: Optional[str]) -> bytes:
    if len(blob) < HEADER_PLAIN_LEN or blob[:6] != MAGIC:
        raise ValueError("not an LSMENC archive (bad magic)")
    version = blob[6]
    if version != VERSION:
        raise ValueError(f"unsupported archive version {version}")
    flags = blob[7]
    if not (flags & FLAG_ENCRYPTED):
        return blob[HEADER_PLAIN_LEN:]
    if not password:
        raise ValueError("archive is encrypted but no password was supplied")
    need = HEADER_PLAIN_LEN + SALT_LEN + NONCE_LEN
    if len(blob) < need:
        raise ValueError("archive truncated")
    salt = blob[HEADER_PLAIN_LEN:HEADER_PLAIN_LEN + SALT_LEN]
    nonce = blob[HEADER_PLAIN_LEN + SALT_LEN:need]
    ct = blob[need:]
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    key = _derive_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag:
        raise ValueError("decryption failed — wrong password or corrupt archive")


def sniff_encrypted(blob: bytes) -> Optional[bool]:
    """True/False if blob is an LSMENC archive (encrypted or not),
    None if it isn't our format."""
    if len(blob) < HEADER_PLAIN_LEN or blob[:6] != MAGIC:
        return None
    return bool(blob[7] & FLAG_ENCRYPTED)


SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def clear_sqlite_sidecars(db_path: str) -> None:
    """Remove WAL / SHM / journal sidecars next to db_path. Used by
    import-apply paths so a stale WAL from the *previous* on-disk DB
    can't be rolled forward against the freshly-imported one on the
    next service start (silently masks imported rows). Idempotent —
    missing sidecars are ignored."""
    for ext in SQLITE_SIDECARS:
        try:
            os.unlink(db_path + ext)
        except FileNotFoundError:
            pass


def sqlite_snapshot(src_path: str) -> bytes:
    """Online-backup a SQLite DB to a bytes blob via the sqlite3
    backup API into an in-memory destination, then serialize. WAL is
    checkpointed transparently and the output is a single consistent
    .db (no -wal / -shm sidecars to ship)."""
    import sqlite3
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(":memory:")
    try:
        src.backup(dst)
        return bytes(dst.serialize())
    finally:
        dst.close()
        src.close()
