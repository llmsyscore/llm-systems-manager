"""Multi-user management for the LLM Systems Manager (issue #125).

UserStore persists named users with an Admin/Operator role to
data/manager_users.json (0600), mirroring model_profiles.ProfileStore.
LockoutTracker holds in-memory per-username/per-IP failed-login state.
Password hashing reuses auth.scrypt_hash / scrypt_verify (stdlib scrypt).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from durable_io import write_durable  # type: ignore[import-not-found]  # sibling — leaf module

log = logging.getLogger("llm-systems-manager.users")

ROLES = ("admin", "operator")
_NAME_RE = re.compile(r"^[a-z0-9._-]{1,32}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserStore:
    def __init__(self, path: "Path | str") -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    # ── persistence ──────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            with open(self._path) as f:
                d = json.load(f)
        except (FileNotFoundError, ValueError):
            d = {}
        d.setdefault("schema_version", 1)
        d.setdefault("users", {})
        return d

    def _save(self, data: dict) -> None:
        write_durable(self._path, json.dumps(data, indent=2))

    # ── helpers ──────────────────────────────────────────────────
    @staticmethod
    def normalize(username: str) -> str:
        return (username or "").strip().lower()

    @staticmethod
    def valid_name(username: str) -> bool:
        return bool(_NAME_RE.match(username or ""))

    def _enabled_admins(self, users: dict, exclude: str = "") -> int:
        return sum(
            1 for n, u in users.items()
            if u.get("role") == "admin" and not u.get("disabled") and not u.get("reset_required") and n != exclude
        )

    # ── reads ────────────────────────────────────────────────────
    def is_empty(self) -> bool:
        with self._lock:
            return not self._load()["users"]

    def get(self, username: str) -> "dict | None":
        with self._lock:
            return self._load()["users"].get(self.normalize(username))

    def list(self) -> list:
        with self._lock:
            users = self._load()["users"]
        out = []
        for name, u in sorted(users.items()):
            row = {k: v for k, v in u.items() if k != "password_hash"}
            row["username"] = name
            out.append(row)
        return out

    def count_enabled_admins(self) -> int:
        with self._lock:
            return self._enabled_admins(self._load()["users"])

    def password_pending(self) -> bool:
        """True when an enabled admin still has a temporary password or needs a new one."""
        with self._lock:
            users = self._load()["users"]
        return any(u.get("role") == "admin" and not u.get("disabled")
                   and (u.get("must_change") or u.get("reset_required")) for u in users.values())

    def needs_reset(self) -> bool:
        """True when no enabled admin has a password to sign in with."""
        with self._lock:
            return self._enabled_admins(self._load()["users"]) == 0

    def awaiting_reset(self, is_retired) -> list:
        """Enabled admins marked reset_required or whose password is_retired(hash) accepts."""
        with self._lock:
            users = self._load()["users"]
        return [n for n, u in sorted(users.items())
                if u.get("role") == "admin" and not u.get("disabled")
                and (u.get("reset_required") or is_retired(u.get("password_hash") or ""))]

    # ── writes ───────────────────────────────────────────────────
    def create(self, username: str, password_hash: str, role: str, must_change: bool = False) -> dict:
        name = self.normalize(username)
        if not self.valid_name(name):
            raise ValueError("invalid username (use a-z 0-9 . _ - up to 32 chars)")
        if role not in ROLES:
            raise ValueError(f"invalid role (use one of {', '.join(ROLES)})")
        with self._lock:
            data = self._load()
            if name in data["users"]:
                raise ValueError("user already exists")
            ts = _now_iso()
            data["users"][name] = {
                "password_hash": password_hash, "role": role, "disabled": False,
                "created_at": ts, "updated_at": ts, "last_login": None,
                "password_changed_at": ts,
            }
            if must_change:
                data["users"][name]["must_change"] = True
            self._save(data)
            return data["users"][name]

    def _mutate(self, username: str, fn) -> dict:
        name = self.normalize(username)
        with self._lock:
            data = self._load()
            u = data["users"].get(name)
            if not u:
                raise KeyError(name)
            fn(data["users"], name, u)
            u["updated_at"] = _now_iso()
            self._save(data)
            return u

    def set_password(self, username: str, password_hash: str, must_change: bool = False) -> dict:
        def fn(_users, _n, u):
            u["password_hash"] = password_hash
            u["password_changed_at"] = _now_iso()
            u.pop("reset_required", None)
            u.pop("must_change", None)
            if must_change:
                u["must_change"] = True
        return self._mutate(username, fn)

    def set_temporary(self, username: str, password_hash: str) -> str:
        """Gives `username` a temporary password it must change at sign-in; a missing user is
        created as an admin and a disabled one is enabled. Returns the stored name."""
        name = self.normalize(username)
        with self._lock:
            if name not in self._load()["users"]:
                self.create(name, password_hash, "admin", must_change=True)
                return name
            self._mutate(name, lambda _u, _n, u: u.__setitem__("disabled", False))
            self.set_password(name, password_hash, must_change=True)
            return name

    def retire(self, is_retired) -> list:
        """Blanks every password is_retired(hash) accepts and marks the user reset_required;
        returns their names."""
        with self._lock:
            data = self._load()
            hit = [n for n, u in data["users"].items() if is_retired(u.get("password_hash") or "")]
            for n in hit:
                data["users"][n].update(password_hash="", reset_required=True, updated_at=_now_iso())
                data["users"][n].pop("must_change", None)
            if hit:
                self._save(data)
            return hit

    def set_role(self, username: str, role: str) -> dict:
        if role not in ROLES:
            raise ValueError(f"invalid role (use one of {', '.join(ROLES)})")
        def fn(users, name, u):
            if u.get("role") == "admin" and role != "admin" and self._enabled_admins(users, exclude=name) == 0:
                raise ValueError("cannot demote the last enabled admin")
            u["role"] = role
        return self._mutate(username, fn)

    def set_disabled(self, username: str, disabled: bool) -> dict:
        def fn(users, name, u):
            if disabled and u.get("role") == "admin" and not u.get("disabled") \
                    and self._enabled_admins(users, exclude=name) == 0:
                raise ValueError("cannot disable the last enabled admin")
            u["disabled"] = bool(disabled)
        return self._mutate(username, fn)

    def delete(self, username: str) -> None:
        name = self.normalize(username)
        with self._lock:
            data = self._load()
            u = data["users"].get(name)
            if not u:
                raise KeyError(name)
            if u.get("role") == "admin" and not u.get("disabled") \
                    and self._enabled_admins(data["users"], exclude=name) == 0:
                raise ValueError("cannot delete the last enabled admin")
            del data["users"][name]
            self._save(data)

    def stamp_login(self, username: str) -> None:
        try:
            self._mutate(username, lambda _u, _n, u: u.__setitem__("last_login", _now_iso()))
        except KeyError:
            pass

    def seed_admin(self, username: str, password_hash: str) -> "dict | None":
        # Lock held across check + create (RLock is reentrant) so two seeders
        # on a fresh store can't both pass the empty check and race to create.
        with self._lock:
            if self._load()["users"]:
                return None
            return self.create(username, password_hash, "admin")


class LockoutTracker:
    """In-memory failed-login tracker. Keys are opaque strings; callers track
    both a username key and an IP key. Lock = threshold failures within window."""

    def __init__(self, *, threshold: int = 5, window_s: int = 900,
                 duration_s: int = 900, clock=time.time) -> None:
        self.threshold = max(1, int(threshold))
        self.window_s = max(1, int(window_s))
        self.duration_s = max(1, int(duration_s))
        self._clock = clock
        self._fails: "dict[str, list]" = {}
        self._locked_until: "dict[str, float]" = {}
        self._lock = threading.Lock()

    def record_failure(self, key: str) -> None:
        if not key:
            return
        now = self._clock()
        with self._lock:
            fails = [t for t in self._fails.get(key, []) if now - t < self.window_s]
            fails.append(now)
            self._fails[key] = fails
            if len(fails) >= self.threshold:
                self._locked_until[key] = now + self.duration_s
                self._fails[key] = []

    def is_locked(self, key: str) -> bool:
        if not key:
            return False
        now = self._clock()
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            if until <= now:
                self._locked_until.pop(key, None)
                return False
            return True

    def retry_after(self, key: str) -> int:
        now = self._clock()
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            return int(until - now) if until > now else 0

    def fail_count(self, key: str) -> int:
        """Failed attempts in the current window; resets to 0 once locked."""
        if not key:
            return 0
        now = self._clock()
        with self._lock:
            return len([t for t in self._fails.get(key, []) if now - t < self.window_s])

    def minutes_left(self, key: str) -> "int | None":
        """Whole minutes until lockout expiry, rounded up; None when not locked."""
        secs = self.retry_after(key)
        return -(-secs // 60) if secs > 0 else None

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)


import auth  # type: ignore[import-not-found]  # sibling; for scrypt_verify (no cycle: auth imports us lazily)

STORE: "UserStore | None" = None
LOCKOUT: "LockoutTracker | None" = None

# Decoy hash verified for missing/disabled users so every login pays one scrypt,
# closing the timing side-channel that would otherwise reveal which usernames exist.
_DECOY_HASH = auth.scrypt_hash("\x00manager-users-decoy\x00")


def init(users_path, *, threshold: int, window_s: int, duration_s: int) -> None:
    # Always (re)creates STORE/LOCKOUT; production calls this once at startup.
    global STORE, LOCKOUT
    STORE = UserStore(users_path)
    LOCKOUT = LockoutTracker(threshold=threshold, window_s=window_s, duration_s=duration_s)


def authenticate(username: str, password: str, remote_ip: str) -> dict:
    """Full login decision: lockout → lookup → disabled → verify. Records a
    failure for BOTH the username and the source IP; uniform generic failures
    so the caller never reveals which key/account state tripped."""
    name = UserStore.normalize(username)
    ukey = f"user:{name}"
    # Key on the source IP only when present; a blank IP would collapse every
    # client onto one shared "ip:" bucket.
    ipkey = f"ip:{remote_ip}" if remote_ip else None
    keys = [ukey] + ([ipkey] if ipkey else [])
    if LOCKOUT and any(LOCKOUT.is_locked(k) for k in keys):
        return {"ok": False, "locked": True,
                "retry_after": max(LOCKOUT.retry_after(k) for k in keys)}
    u = STORE.get(name) if STORE else None
    # Always run one scrypt_verify (decoy when no user or no hash) so timing can't enumerate users.
    pw_ok = auth.scrypt_verify(password, (u or {}).get("password_hash") or _DECOY_HASH)
    if not u or u.get("disabled") or u.get("reset_required") or not pw_ok:
        if LOCKOUT:
            for k in keys:
                LOCKOUT.record_failure(k)
        return {"ok": False}
    if LOCKOUT:
        for k in keys:
            LOCKOUT.clear(k)
    STORE.stamp_login(name)
    return {"ok": True, "role": u.get("role", "operator"), "username": name}


from flask import g, jsonify, request as flask_request, session  # noqa: E402

MIN_PASSWORD = 8


def password_error(password: str) -> "str | None":
    """Why `password` cannot be set (too short, or the one older releases shipped), else None."""
    if len(password) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if password == auth.DEFAULT_AUTH_USER:
        return "choose a different password; this one is no longer accepted"
    return None


def bootstrap(seed_user: str, seed_hash: str, is_retired) -> "tuple[list, bool]":
    """Startup pass: seeds the first admin from a provisioned hash, then retires old shipped
    passwords. Returns (retired names, whether no admin can sign in yet)."""
    if seed_hash:
        STORE.seed_admin(seed_user, seed_hash)
    return STORE.retire(is_retired), STORE.needs_reset()


def _bad(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def register_routes(app, ctx) -> None:
    require_admin = ctx.require_admin

    @app.route("/api/admin/users", methods=["GET"])
    def admin_users_list():
        deny = require_admin()
        if deny is not None:
            return deny
        rows = STORE.list()
        for r in rows:
            key = f"user:{r['username']}"
            r["locked"] = bool(LOCKOUT and LOCKOUT.is_locked(key))
            r["failed_count"] = LOCKOUT.fail_count(key) if LOCKOUT else 0
            r["lock_minutes_left"] = LOCKOUT.minutes_left(key) if LOCKOUT else None
        return jsonify({"ok": True, "users": rows})

    @app.route("/api/admin/users", methods=["POST"])
    def admin_users_create():
        deny = require_admin()
        if deny is not None:
            return deny
        b = flask_request.get_json(silent=True) or {}
        pw = b.get("password") or ""
        if (err := password_error(pw)):
            return _bad(err)
        try:
            STORE.create(b.get("username") or "", auth.scrypt_hash(pw), b.get("role") or "operator")
        except ValueError as e:
            log.warning("user create rejected: %s", e)
            exists = "exists" in str(e)
            return _bad("user already exists" if exists else "invalid username or role",
                        409 if exists else 400)
        log.warning("user %r created from %s", b.get("username"), flask_request.remote_addr)
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>", methods=["PATCH"])
    def admin_users_patch(username):
        deny = require_admin()
        if deny is not None:
            return deny
        me = UserStore.normalize(session.get("user") or "")
        target = UserStore.normalize(username)
        if STORE.get(target) is None:
            return _bad("no such user", 404)
        b = flask_request.get_json(silent=True) or {}
        try:
            if "role" in b:
                STORE.set_role(target, b["role"])
            if "disabled" in b:
                if target == me and bool(b["disabled"]):
                    return _bad("cannot disable yourself", 409)
                STORE.set_disabled(target, bool(b["disabled"]))
            if b.get("password"):
                if (err := password_error(b["password"])):
                    return _bad(err)
                STORE.set_password(target, auth.scrypt_hash(b["password"]))
        except ValueError as e:
            log.warning("user update rejected: %s", e)
            last_admin = "last enabled admin" in str(e)
            return _bad("cannot modify the last enabled admin" if last_admin else "invalid role or request",
                        409 if last_admin else 400)
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>", methods=["DELETE"])
    def admin_users_delete(username):
        deny = require_admin()
        if deny is not None:
            return deny
        me = UserStore.normalize(session.get("user") or "")
        target = UserStore.normalize(username)
        if target == me:
            return _bad("cannot delete yourself", 409)
        try:
            STORE.delete(target)
        except KeyError:
            return _bad("no such user", 404)
        except ValueError as e:
            log.warning("user delete rejected: %s", e)
            return _bad("cannot delete the last enabled admin", 409)
        on_deleted = getattr(ctx, "on_user_deleted", None)
        if callable(on_deleted):
            on_deleted(target)
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>/unlock", methods=["POST"])
    def admin_users_unlock(username):
        deny = require_admin()
        if deny is not None:
            return deny
        if LOCKOUT:
            LOCKOUT.clear(f"user:{UserStore.normalize(username)}")
        return jsonify({"ok": True})

    @app.route("/api/me", methods=["GET"])
    def whoami():
        # Fail closed to operator so the UI never grants admin surfaces on an
        # unresolved role; effective_role returns None only for anon contexts.
        role = auth.effective_role() or "operator"
        user = getattr(g, "auth_user", None) or session.get("user")
        is_admin = role == "admin"
        # admin_access is the effective admin gate: role admin AND request IP in admin CIDR.
        admin_ip = auth.admin_ip_ok()
        return jsonify({"ok": True, "username": user, "role": role,
                        "is_admin": is_admin, "admin_ip": admin_ip,
                        "admin_access": is_admin and admin_ip,
                        "authenticated": bool(session.get("auth_ok")),
                        "bypass": not bool(session.get("auth_ok"))})

    @app.route("/api/account/password", methods=["POST"])
    def account_password():
        me = UserStore.normalize(session.get("user") or "")
        u = STORE.get(me) if me else None
        if not u:
            return _bad("no current user (login required)", 403)
        b = flask_request.get_json(silent=True) or {}
        if not auth.scrypt_verify(b.get("current_password") or "", u.get("password_hash", "")):
            return jsonify({"ok": False, "error": "current password is incorrect",
                            "field": "current_password"}), 403
        new = b.get("new_password") or ""
        if (err := password_error(new)):
            return _bad(err)
        if u.get("must_change") and new == b.get("current_password"):
            return _bad("choose a password different from the temporary one")
        STORE.set_password(me, auth.scrypt_hash(new))
        return jsonify({"ok": True})
