"""
companion.py — PWA companion surface (#522 phase 1).

VAPID key management, the web-push subscription store, and (registered by
register_routes) the /companion page, /manifest.webmanifest, /sw.js and
/api/companion/push/* routes.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger("llm-systems-manager")

# Hard cap on stored push subscriptions.
MAX_SUBSCRIPTIONS = 32
# Per-request timeout for each outbound web-push send.
PUSH_TIMEOUT_S = 10

_VAPID_KEY_FILE = "vapid-private.pem"
_SUBS_FILE = "push_subscriptions.json"

# Caps on the alert text the alarm engine may put in a notification (#538).
_MAX_TITLE, _MAX_BODY, _MAX_TAG = 120, 400, 80
# Tokens that read as "not configured", matching the alarm engine's own gate.
_UNSET_TOKENS = {"", "REPLACE_ME"}

# ── Release check (#541) ─────────────────────────────────────────────────────
_DEFAULT_RELEASE_REPO = "llmsyscore/llm-systems-manager"
_RELEASE_TTL_S = 24 * 3600
_RELEASE_TIMEOUT_S = 5
_RELEASE_FILE = "RELEASE"
# enabled=None means "not overridden at runtime, read the config value".
_release: dict = {"enabled": None, "checked_at": 0.0, "tag": None,
                  "body": None, "error": None}
_release_lock = threading.Lock()

_VER_RE = re.compile(r"(\d+)")
# A tag carrying any of these is a pre-release and must never be offered as an
# update, whatever GitHub marked it.
_PRE_RE = re.compile(r"[-.](rc|alpha|beta|pre)", re.I)
# git archive rewrites this via export-subst; a git checkout keeps it literal.
_SUBST_RE = re.compile(r"^\$Format:")


def _base_tag(tag: str) -> str:
    """Release portion of a tag: v1.2.0-rc1 -> v1.2.0, v1.1.1-9-gabc -> v1.1.1."""
    return str(tag or "").strip().split("-", 1)[0]


def _ver_key(tag: str) -> tuple:
    """Numeric tuple for a semver-ish tag; non-numeric parts drop."""
    return tuple(int(n) for n in _VER_RE.findall(_base_tag(tag)))


def _newer(latest: str, current: str) -> bool:
    """True only for a strictly newer FINAL release. Pre-releases never count:
    keying the whole tag would rank v1.2.0-rc1 above v1.2.0."""
    if _PRE_RE.search(str(latest or "")):
        return False
    a, b = _ver_key(latest), _ver_key(current)
    return bool(a) and bool(b) and a > b


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run(cmd: "list[str]", cwd: "str | None" = None) -> Optional[str]:
    import subprocess
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
        return out if r.returncode == 0 and out else None
    except Exception:
        return None


def _installed_release() -> dict:
    """How this install identifies itself.

    {tag, describe, ahead, source}. `source` is how it was determined:
      release-file  a packaged build (tarball / brew / deb / rpm / image) —
                    git archive's export-subst stamped the tag at build time
      git           a git checkout, via `git describe`
      dpkg / rpm    the system package database
      None          nothing could identify it
    """
    out = {"tag": None, "describe": None, "ahead": 0, "source": None}
    root = _repo_root()

    # 1. Shipped RELEASE file — the only source every packaging channel shares.
    try:
        raw = (root / _RELEASE_FILE).read_text(encoding="utf-8").strip()
        if raw and not _SUBST_RE.match(raw):
            out.update(tag=_base_tag(raw), describe=raw, source="release-file")
        # else: unsubstituted placeholder => this is a git checkout, fall through
    except Exception:
        # No RELEASE file, or it is unreadable — a normal state for a git
        # checkout. The next resolution step answers instead.
        pass

    # 2. git checkout.
    if not out["tag"] and (root / ".git").exists():
        desc = _run(["git", "describe", "--tags"], cwd=str(root))
        if desc:
            out.update(tag=_base_tag(desc), describe=desc, source="git")

    # 3. System package database.
    if not out["tag"]:
        for cmd, src in ((["dpkg-query", "-W", "-f=${Version}",
                           "llm-systems-manager"], "dpkg"),
                         (["rpm", "-q", "--qf", "%{VERSION}",
                           "llm-systems-manager"], "rpm")):
            v = _run(cmd)
            if v:
                # deb/rpm sanitize '-' to '~'; restore for comparison.
                v = v.replace("~", "-")
                out.update(tag=_base_tag("v" + v.lstrip("v")),
                           describe=v, source=src)
                break

    if out["describe"]:
        m = re.search(r"-(\d+)-g[0-9a-f]+$", out["describe"])
        if m:
            out["ahead"] = int(m.group(1))
    return out


def _install_kind() -> str:
    """Coarse install type, for the UI and for choosing a fallback check."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return "container"
    src = _installed_release()["source"]
    if src == "release-file":
        return "package"
    if src in ("dpkg", "rpm"):
        return src
    if (_repo_root() / ".git").exists():
        return "git"
    return "unknown"


def _latest_release(repo: str) -> "tuple[Optional[str], Optional[str], Optional[str]]":
    """(tag, body, error) for the repo's latest release, cached 24 h. Network
    failures degrade to the cached tag and an error string, never an exception.
    Toggling the switch off and on clears checked_at, forcing a fresh read."""
    now = time.time()
    with _release_lock:
        if _release["tag"] and now - _release["checked_at"] < _RELEASE_TTL_S:
            return _release["tag"], _release["body"], _release["error"]
    tag = body = err = None
    try:
        import requests  # lazy: only on an opted-in check
        r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest",
                         timeout=_RELEASE_TIMEOUT_S,
                         headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            data = r.json() or {}
            tag = data.get("tag_name")
            body = data.get("body") or ""
        else:
            err = f"github returned HTTP {r.status_code}"
    except Exception as e:
        err = type(e).__name__
        log.debug("release check failed: %s: %s", type(e).__name__, e)
    with _release_lock:
        _release["checked_at"] = now
        if tag:
            _release["tag"] = tag
            _release["body"] = body
        _release["error"] = err
        return _release["tag"], _release["body"], err


# ── VAPID keys ───────────────────────────────────────────────────────────────

_VAPID_LOCK = threading.Lock()


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
    Read from disk every call so it can never diverge from the signing key."""
    pem_path = ensure_vapid_key(data_dir)
    key = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode("ascii")


# ── subscriptions ────────────────────────────────────────────────────────────

# Dotted ASCII hostname; the last label must contain a letter, which is what
# rejects shorthand IPv4 (127.1, 192.168.257) alongside plain dotted quads.
_HOSTNAME_RE = re.compile(
    r"^(?=.*[a-z])[a-z0-9]([a-z0-9-]*[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_NON_PUBLIC_TLDS = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def valid_push_endpoint(endpoint: Any) -> bool:
    """True for a public https push-service URL. String-only checks; the
    server POSTs here, so anything IP-shaped or internal-looking is refused."""
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        return False
    try:
        parts = urlsplit(endpoint)
        if parts.port not in (None, 443):
            return False
    except ValueError:
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or not _HOSTNAME_RE.match(host):
        return False
    if host.endswith(_NON_PUBLIC_TLDS):
        return False
    # A last label with letters can't be an IPv4 literal, but be explicit.
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def resolves_to_public_ip(host: str) -> bool:
    """False when the name resolves to any loopback/private/link-local
    address. Unresolvable names pass — the send fails on its own."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def valid_subscription(sub: Any) -> bool:
    """Shape check for a browser PushSubscription.toJSON() payload."""
    if not isinstance(sub, dict):
        return False
    keys = sub.get("keys")
    if not valid_push_endpoint(sub.get("endpoint")):
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
                if isinstance(e, dict) and isinstance(e.get("subscription"), dict)
                and isinstance(e["subscription"].get("endpoint"), str)]

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
            existing = data.get(endpoint)
            prior = (existing.get("created")
                     if isinstance(existing, dict) else None)
            data[endpoint] = {"subscription": sub, "ua": ua[:200],
                              "created": time.time() if prior is None else prior}
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


def _push_session():
    """requests session that refuses redirects — pywebpush passes none, and a
    3xx from a push endpoint would otherwise re-target scheme/host/port."""
    global _PUSH_SESSION
    if _PUSH_SESSION is None:
        import requests

        class _NoRedirect(requests.Session):
            def request(self, *args, **kwargs):
                kwargs["allow_redirects"] = False
                return super().request(*args, **kwargs)

        _PUSH_SESSION = _NoRedirect()
    return _PUSH_SESSION


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
                timeout=PUSH_TIMEOUT_S, requests_session=_push_session())
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


def _fan_out(subs: "list[dict]", payload: str, pem: Path, contact: str) -> dict:
    """Send one payload to every subscription, pruning dead endpoints.
    Bounded parallelism: worst case ~PUSH_TIMEOUT_S per batch of 8 instead of
    timeout x subscriptions on one Cheroot worker."""
    sent = failed = pruned = 0
    with ThreadPoolExecutor(max_workers=min(8, len(subs))) as pool:
        results = list(pool.map(
            lambda s: (s, _send_one(s, payload, pem, contact)), subs))
    for sub, (ok, prune) in results:
        sent += ok
        failed += not ok
        if prune and _store.remove(sub["endpoint"]):
            pruned += 1
    return {"sent": sent, "failed": failed, "pruned": pruned}


# ── alarm-engine notify bridge (#538) ────────────────────────────────────────

def notify_token(settings: Any) -> str:
    """Bearer the alarm engine must present on the notify route: the companion
    override, else the AE management token, else its ingest token."""
    cfg = getattr(getattr(settings, "manager", None), "companion", None)
    ae = getattr(settings, "alarm_engine", None)
    for raw in (getattr(cfg, "push_notify_token", ""),
                getattr(ae, "management_token", ""),
                getattr(ae, "ingest_token", "")):
        tok = (raw or "").strip()
        if tok not in _UNSET_TOKENS:
            return tok
    return ""


def _bearer(header: str) -> str:
    h = header or ""
    return h[len("Bearer "):].strip() if h.startswith("Bearer ") else ""


def notify_path(url: Any) -> str:
    """Same-origin path the notification opens. A push must never deep-link
    off this manager, so anything else degrades to the companion root."""
    u = str(url or "")
    if u.startswith("/") and not u.startswith("//") and "\\" not in u:
        return u[:200]
    return "/companion"


# ── routes ───────────────────────────────────────────────────────────────────

# Bound by register_routes; tests monkeypatch these to a sandbox.
_data_dir: Optional[Path] = None
_store: Optional[SubscriptionStore] = None
_PUSH_SESSION: Any = None


def _manifest() -> dict:
    return {
        "id": "/companion",
        "name": "LLM Systems Manager",
        "short_name": "LLM Systems Manager",
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
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512",
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

    @app.route("/api/companion/release", methods=["GET", "PUT"])
    def companion_release():
        cfg = getattr(ctx.settings.manager, "companion", None)
        repo = getattr(cfg, "release_repo", "") or _DEFAULT_RELEASE_REPO
        if flask_request.method == "PUT":
            deny = ctx.require_admin()
            if deny is not None:
                return deny
            body = flask_request.get_json(silent=True) or {}
            if "enabled" not in body:
                return jsonify({"ok": False, "error": "enabled required"}), 400
            _release["enabled"] = bool(body["enabled"])
            _release["checked_at"] = 0.0
            return jsonify({"ok": True, "enabled": _release["enabled"]})
        enabled = _release["enabled"]
        if enabled is None:
            enabled = bool(getattr(cfg, "release_check", False))
        inst = _installed_release()
        out = {"ok": True, "enabled": enabled, "build": version,
               "installed": inst["tag"], "describe": inst["describe"],
               "ahead": inst["ahead"], "source": inst["source"],
               "install_kind": _install_kind(), "repo": repo}
        if not enabled:
            return jsonify(out)
        latest, body, err = _latest_release(repo)
        out["latest"] = latest
        if inst["tag"] and latest:
            out["update_available"] = _newer(latest, inst["tag"])
        elif latest and body:
            # Last resort for an install that can't name its release: the notes
            # of each release carry the manager build stamp, so finding ours in
            # the newest release's notes means we are already on it.
            if version and version in body:
                out["update_available"] = False
                out["note"] = "matched this build in the latest release notes"
            else:
                out["update_available"] = None
                out["note"] = "install has no release tag to compare"
        else:
            out["update_available"] = None
            out["note"] = "install has no release tag to compare"
        if err:
            out["error"] = err
        return jsonify(out)

    @app.route("/api/companion/push/public-key")
    def companion_push_key():
        return jsonify({"ok": True, "key": vapid_public_key_b64(_data_dir)})

    @app.route("/api/companion/push/subscriptions")
    def companion_push_subs():
        # Endpoint prefixes identify other people's devices, so only admins
        # see the roster; everyone else gets the count they need for the UI.
        subs = _store.list()
        if ctx.require_admin() is not None:
            return jsonify({"ok": True, "count": len(subs)})
        return jsonify({"ok": True, "count": len(subs),
                        "endpoints": [s["endpoint"][:48] for s in subs]})

    @app.route("/api/companion/push/subscribe", methods=["POST"])
    def companion_push_subscribe():
        sub = flask_request.get_json(silent=True)
        if not valid_subscription(sub):
            return jsonify({"ok": False, "error": "invalid subscription"}), 400
        host = urlsplit(sub["endpoint"]).hostname or ""
        if not resolves_to_public_ip(host):
            return jsonify({"ok": False,
                            "error": "endpoint resolves to a private address"}), 400
        ua = flask_request.headers.get("User-Agent", "")
        if not _store.add(sub, ua=ua):
            return jsonify({"ok": False,
                            "error": "subscription limit reached"}), 400
        return jsonify({"ok": True})

    @app.route("/api/companion/push/unsubscribe", methods=["POST"])
    def companion_push_unsubscribe():
        body = flask_request.get_json(silent=True)
        endpoint = (body.get("endpoint") or "") if isinstance(body, dict) else ""
        return jsonify({"ok": True, "removed": _store.remove(endpoint)})

    @app.route("/api/companion/push/test", methods=["POST"])
    def companion_push_test():
        subs = _store.list()
        body = flask_request.get_json(silent=True)
        # An explicit endpoint tests just that device, so one stale
        # subscription can't report failure for a push that arrived.
        target = (body.get("endpoint") or "") if isinstance(body, dict) else ""
        # Only an admin may fan a test out to every registered device.
        if not target and ctx.require_admin() is not None:
            return jsonify({"ok": False,
                            "error": "send the calling device's endpoint"}), 403
        if target:
            subs = [s for s in subs if s.get("endpoint") == target]
            if not subs:
                return jsonify({"ok": False,
                                "error": "this device is not subscribed"}), 404
        if not subs:
            return jsonify({"ok": False, "error": "no subscriptions"}), 400
        try:
            _webpush_funcs()  # import-check only; _send_one re-imports per send
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
        out = _fan_out(subs, payload, pem, contact)
        return jsonify({"ok": out["failed"] == 0, **out})

    @app.route("/api/companion/push/notify", methods=["POST"])
    def companion_push_notify():
        """Alarm-engine bridge (#538): fan one alert out to every subscribed
        device. Bearer-gated on the shared alarm-engine token so the VAPID
        key and the subscription store never leave the manager."""
        expected = notify_token(ctx.settings)
        provided = _bearer(flask_request.headers.get("Authorization", ""))
        # Compare as bytes: compare_digest rejects non-ASCII str outright.
        authed = bool(expected and provided and hmac.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")))
        # No token configured anywhere: fall back to an admin session rather
        # than opening a send-to-every-device route.
        if not authed and ctx.require_admin() is not None:
            if not expected:
                log.warning("companion: notify refused — no alarm-engine token "
                            "configured; set [alarm_engine].management_token")
            return jsonify({"ok": False,
                            "error": "notify authentication required"}), 403
        body = flask_request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "json object required"}), 400
        subs = _store.list()
        if not subs:
            return jsonify({"ok": True, "sent": 0, "failed": 0, "pruned": 0,
                            "subscriptions": 0})
        try:
            _webpush_funcs()  # import-check only; _send_one re-imports per send
        except ImportError:
            return jsonify({"ok": False,
                            "error": "pywebpush is not installed"}), 503
        payload = json.dumps({
            "title": str(body.get("title") or "LLM Systems Manager")[:_MAX_TITLE],
            "body": str(body.get("body") or "")[:_MAX_BODY],
            "severity": str(body.get("severity") or "info")[:16],
            "tag": str(body.get("tag") or "lsm-alert")[:_MAX_TAG],
            "url": notify_path(body.get("url")),
        })
        out = _fan_out(subs, payload, ensure_vapid_key(_data_dir),
                       _push_contact(ctx.settings))
        return jsonify({"ok": out["failed"] == 0,
                        "subscriptions": len(subs), **out})
