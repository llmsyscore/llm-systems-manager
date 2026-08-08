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
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger("llm-systems-manager")

# Hard cap on stored push subscriptions.
MAX_SUBSCRIPTIONS = 32
# Per-request timeout for each outbound web-push send.
PUSH_TIMEOUT_S = 10

_VAPID_KEY_FILE = "vapid-private.pem"
_SUBS_FILE = "push_subscriptions.json"


# ── VAPID keys ───────────────────────────────────────────────────────────────

_VAPID_LOCK = threading.Lock()
_PUBKEY_CACHE: "dict[str, str]" = {}


def ensure_vapid_key(data_dir: Path) -> Path:
    """Return the VAPID EC P-256 private key PEM path, generating it once."""
    data_dir = Path(data_dir)
    pem_path = data_dir / _VAPID_KEY_FILE
    with _VAPID_LOCK:
        if pem_path.exists():
            return pem_path
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = pem_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)
        os.replace(tmp, pem_path)
    log.info("companion: generated VAPID key at %s", pem_path)
    return pem_path


def vapid_public_key_b64(data_dir: Path) -> str:
    """Application-server key: base64url (unpadded) uncompressed P-256 point.
    Cached per key path — the key never changes for the life of the install."""
    pem_path = ensure_vapid_key(data_dir)
    cache_key = str(pem_path)
    cached = _PUBKEY_CACHE.get(cache_key)
    if cached:
        return cached
    key = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    out = base64.urlsafe_b64encode(point).rstrip(b"=").decode("ascii")
    _PUBKEY_CACHE[cache_key] = out
    return out


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


# ── web push sending ─────────────────────────────────────────────────────────

def _webpush_funcs():
    """Import pywebpush on first use; returns (webpush, WebPushException)."""
    from pywebpush import webpush, WebPushException
    return webpush, WebPushException


def _push_contact(settings: Any) -> str:
    companion_cfg = getattr(getattr(settings, "manager", None), "companion", None)
    return (getattr(companion_cfg, "push_contact", "") or
            "mailto:admin@example.com")


def _send_one(sub: dict, payload: str, pem_path: Path,
              contact: str) -> "tuple[bool, bool]":
    """Send one notification. Returns (ok, prune) — prune on 404/410."""
    webpush, WebPushException = _webpush_funcs()
    try:
        webpush(subscription_info=sub, data=payload,
                vapid_private_key=str(pem_path),
                vapid_claims={"sub": contact}, ttl=60,
                timeout=PUSH_TIMEOUT_S)
        return True, False
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        log.warning("companion: push to %s failed (%s)",
                    sub.get("endpoint", "?")[:60], status or exc)
        return False, status in (404, 410)
    except Exception as exc:
        log.warning("companion: push to %s failed (%s)",
                    sub.get("endpoint", "?")[:60], exc)
        return False, False


# ── routes ───────────────────────────────────────────────────────────────────

# Bound by register_routes; tests monkeypatch these to a sandbox.
_data_dir: Optional[Path] = None
_store: Optional[SubscriptionStore] = None


def _manifest() -> dict:
    return {
        "id": "/companion",
        "name": "LLM Systems Manager",
        "short_name": "LLM Manager",
        "description": "Phone companion for the LLM Systems Manager dashboard",
        "start_url": "/companion",
        "scope": "/",
        "display": "standalone",
        "background_color": "#111111",
        "theme_color": "#0d0d0d",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }


def register_routes(app, ctx, static_dir: Path) -> None:
    """Mount /companion, /manifest.webmanifest, /sw.js and
    /api/companion/push/* on the manager app."""
    global _data_dir, _store
    from flask import Response, jsonify, request as flask_request

    static_dir = Path(static_dir)
    _data_dir = Path(ctx.data_dir)
    _store = SubscriptionStore(_data_dir / _SUBS_FILE)
    version = ctx.version

    def _stamped(name: str, mimetype: str) -> "Response":
        text = (static_dir / name).read_text(encoding="utf-8")
        return Response(text.replace("__MGR_VERSION__", version),
                        mimetype=mimetype)

    @app.route("/companion")
    def companion_page():
        return _stamped("companion.html", "text/html")

    @app.route("/manifest.webmanifest")
    def companion_manifest():
        return Response(json.dumps(_manifest()),
                        mimetype="application/manifest+json")

    @app.route("/sw.js")
    def companion_sw():
        resp = _stamped("sw.js", "text/javascript")
        # no-cache: browsers must revalidate the worker script on every load.
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/api/companion/push/public-key")
    def companion_push_key():
        return jsonify({"ok": True, "key": vapid_public_key_b64(_data_dir)})

    @app.route("/api/companion/push/subscriptions")
    def companion_push_subs():
        subs = _store.list()
        return jsonify({"ok": True, "count": len(subs),
                        "endpoints": [s["endpoint"][:48] for s in subs]})

    @app.route("/api/companion/push/subscribe", methods=["POST"])
    def companion_push_subscribe():
        sub = flask_request.get_json(silent=True)
        if not valid_subscription(sub):
            return jsonify({"ok": False, "error": "invalid subscription"}), 400
        ua = flask_request.headers.get("User-Agent", "")
        if not _store.add(sub, ua=ua):
            return jsonify({"ok": False,
                            "error": "subscription limit reached"}), 400
        return jsonify({"ok": True})

    @app.route("/api/companion/push/unsubscribe", methods=["POST"])
    def companion_push_unsubscribe():
        body = flask_request.get_json(silent=True) or {}
        endpoint = body.get("endpoint") or ""
        return jsonify({"ok": True, "removed": _store.remove(endpoint)})

    @app.route("/api/companion/push/test", methods=["POST"])
    def companion_push_test():
        subs = _store.list()
        if not subs:
            return jsonify({"ok": False, "error": "no subscriptions"}), 400
        try:
            webpush, WebPushException = _webpush_funcs()
        except ImportError:
            return jsonify({"ok": False,
                            "error": "pywebpush is not installed"}), 503
        pem = ensure_vapid_key(_data_dir)
        contact = _push_contact(ctx.settings)
        payload = json.dumps({
            "title": "LLM Systems Manager",
            "body": "Test notification — push is working.",
            "tag": "lsm-test", "url": "/companion",
        })
        # Bounded parallel fan-out; worst case ~PUSH_TIMEOUT_S per batch of 8
        # instead of timeout x subscriptions on one Cheroot worker.
        sent = failed = pruned = 0
        with ThreadPoolExecutor(max_workers=min(8, len(subs))) as pool:
            results = list(pool.map(
                lambda s: (s, _send_one(s, payload, pem, contact)), subs))
        for sub, (ok, prune) in results:
            sent += ok
            failed += not ok
            if prune and _store.remove(sub["endpoint"]):
                pruned += 1
        return jsonify({"ok": failed == 0, "sent": sent,
                        "failed": failed, "pruned": pruned})
