"""
companion.py — PWA companion surface (#522 phase 1).

VAPID key management, the web-push subscription store, and (registered by
register_routes) the /companion page, /manifest.webmanifest, /sw.js and
/api/companion/push/* routes.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger("llm-systems-manager")

# Hard cap on stored push subscriptions — bounds fan-out cost and abuse.
MAX_SUBSCRIPTIONS = 32

_VAPID_KEY_FILE = "vapid-private.pem"
_SUBS_FILE = "push_subscriptions.json"


# ── VAPID keys ───────────────────────────────────────────────────────────────

def ensure_vapid_key(data_dir: Path) -> Path:
    """Return the VAPID EC P-256 private key PEM path, generating it once."""
    data_dir = Path(data_dir)
    pem_path = data_dir / _VAPID_KEY_FILE
    if pem_path.exists():
        return pem_path
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    log.info("companion: generated VAPID key at %s", pem_path)
    return pem_path


def vapid_public_key_b64(data_dir: Path) -> str:
    """Application-server key: base64url (unpadded) uncompressed P-256 point."""
    pem_path = ensure_vapid_key(data_dir)
    key = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode("ascii")


# ── subscriptions ────────────────────────────────────────────────────────────

def valid_subscription(sub: Any) -> bool:
    """Shape check for a browser PushSubscription.toJSON() payload."""
    if not isinstance(sub, dict):
        return False
    endpoint = sub.get("endpoint")
    keys = sub.get("keys")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        return False
    if not isinstance(keys, dict):
        return False
    return bool(keys.get("p256dh")) and bool(keys.get("auth"))


class SubscriptionStore:
    """JSON-file store of push subscriptions, keyed by endpoint. Atomic
    writes (tmp + rename, 0600) under a lock; corrupt files read as empty."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, indent=1).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, self._path)

    def list(self) -> list[dict]:
        return [e["subscription"] for e in self._read().values()
                if isinstance(e, dict) and isinstance(e.get("subscription"), dict)]

    def count(self) -> int:
        return len(self._read())

    def add(self, sub: dict, ua: str = "") -> bool:
        if not valid_subscription(sub):
            return False
        with self._lock:
            data = self._read()
            endpoint = sub["endpoint"]
            if endpoint not in data and len(data) >= MAX_SUBSCRIPTIONS:
                return False
            import time
            data[endpoint] = {"subscription": sub, "ua": ua[:200],
                              "created": data.get(endpoint, {}).get("created")
                              or time.time()}
            self._write(data)
        return True

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            data = self._read()
            if endpoint not in data:
                return False
            del data[endpoint]
            self._write(data)
        return True
