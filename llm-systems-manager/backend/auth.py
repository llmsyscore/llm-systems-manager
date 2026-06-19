"""Dashboard authentication for the LLM Systems Manager.

Browser sessions are gated per [manager.auth].mode (required | disabled |
trusted_cidr). Agent (bearer) calls and a small infra allowlist are never
gated. Password hashing is stdlib scrypt (no extra dependency). Credentials
resolve from the UI-managed data/manager_auth.json first, then the TOML
[manager.auth] (installer provisioning), then a built-in default of
llmadmin / llmadmin so a fresh "required" deploy logs in out of the box —
the login page nudges the operator to change it, and Admin → Authentication
edits the username / password / mode.

Wired into the Flask app by main via register_auth(app, ctx, ...). The
before_request gate, /login, /logout, and /api/admin/auth (GET/POST) all
live here. Shared cross-module deps come from `app_context.Context`;
module-specific deps (the brand palette, TOML rewrite path, agent-token
resolvers) stay as kwargs. Both are copied into module-level globals at
registration so the hot path doesn't pay an attribute lookup per request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from flask import g, jsonify, redirect, request as flask_request, session

log = logging.getLogger("llm-systems-manager.auth")

__all__ = [
    "register_auth",
    "scrypt_hash",
    "scrypt_verify",
    "auth_runtime",
    "auth_write",
    "auth_credential",
    "auth_policy",
    "auth_mode",
    "effective_role",
    "admin_ip_ok",
    "write_toml_auth_mode",
    "MIN_ADMIN_PASSWORD",
    "DEFAULT_AUTH_USER",
    "DEFAULT_AUTH_PASSWORD",
    "AUTH_OPEN_PATHS",
    "AUTH_MODES",
    "AUTH_RUNTIME_MODES",
]

# ── Public constants ──────────────────────────────────────────────────
MIN_ADMIN_PASSWORD = 8
DEFAULT_AUTH_USER = "llmadmin"
DEFAULT_AUTH_PASSWORD = "llmadmin"

# Always reachable without a session: the agent bootstrap (no bearer yet),
# health, and the auth routes themselves. Other agent endpoints are exempted
# dynamically by their bearer token (see _auth_gate).
AUTH_OPEN_PATHS = frozenset({
    "/health", "/login", "/logout", "/api/agents/register",
})

# Runtime gate behaviours vs. the TOML policy value. "auto" is a policy-only
# value (not a runtime mode): it hands live control of the mode to the
# UI-managed data/manager_auth.json. Any other TOML value pins the mode and
# the JSON `mode` is ignored — so a manual TOML edit always wins (after a
# manager restart, since the TOML loads once at import).
AUTH_RUNTIME_MODES = ("required", "disabled", "trusted_cidr")
AUTH_MODES = AUTH_RUNTIME_MODES + ("auto",)  # values selectable in the UI / TOML

# Module-level path of the UI-managed JSON credentials/mode store. Set by
# register_auth() from the data_dir kwarg so tests can monkey-patch it to
# a tmp_path file without touching the live install.
MANAGER_AUTH_FILE: Optional[Path] = None

# ── Private module state (populated by register_auth) ─────────────────
_AUTH_WRITE_LOCK = threading.Lock()

# Hash of the shipped default password, computed once at register_auth time —
# auth_credential() falls back to this when nothing is configured, so /login
# renders and login attempts don't recompute scrypt (~tens of ms) on every
# unauthenticated hit.
DEFAULT_AUTH_HASH = ""

_trusted_cidr_deny_last_log = 0.0  # throttle for the trusted_cidr deny diagnostic

# AE version cache used by the login page footer.
_AE_VERSION_CACHE: "dict[str, Any]" = {"version": "", "fetched_at": 0.0}
_AE_VERSION_LOCK = threading.Lock()
_AE_VERSION_TTL = 60.0   # seconds; AE version only changes when AE restarts

# Cross-module dependencies, set once by register_auth() and read by the
# gate / routes thereafter. Storing them as module-level globals (rather
# than walking a SimpleNamespace per request) keeps the auth gate hot
# path branch-free of attribute lookups.
_settings: Any = None
_manager_version: str = ""
_config_path: Optional[Path] = None
_agent_by_token: Callable[[str], Any] = lambda _t: None
_bearer_from_request: Callable[[], Optional[str]] = lambda: None
_admin_ip_allowed: Callable[[str], bool] = lambda _ip: False
_require_admin: Callable[[], Any] = lambda: None
_brand_palette: Callable[[], dict] = lambda: {}
_brand_logo_svg: Callable[[dict, int], str] = lambda _p, _s=66: ""
_agent_admin_allow: Callable[[], list] = lambda: []
_alarm_engine_url: Callable[[], str] = lambda: ""
_ae_session: Any = None


# ── Password hashing ──────────────────────────────────────────────────
def scrypt_hash(password: str, salt: "bytes | None" = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def scrypt_verify(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(dk_b64)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             n=2 ** 14, r=8, p=1, dklen=len(want))
        return _hmac.compare_digest(got, want)
    except Exception:
        return False


# ── Credential / mode resolution ──────────────────────────────────────
def auth_runtime() -> dict:
    """UI-managed overrides in data/manager_auth.json (mode + credential).
    When present, each field overrides the [manager.auth] TOML defaults — so
    the admin tab can change auth without rewriting the operator's config."""
    if not (MANAGER_AUTH_FILE and MANAGER_AUTH_FILE.is_file()):
        return {}
    # Retry once on JSONDecodeError to suppress the rare transient where the
    # read catches the file mid-corruption window. A second read 10ms later
    # almost always sees the recovered content.
    for attempt in (1, 2):
        try:
            return json.loads(MANAGER_AUTH_FILE.read_text()) or {}
        except json.JSONDecodeError as e:
            if attempt == 2:
                log.warning("manager_auth.json unreadable after retry: %s", e)
            else:
                time.sleep(0.01)
        except Exception as e:
            log.warning("manager_auth.json unreadable: %s", e)
            return {}
    return {}


def auth_write(updates: dict) -> None:
    """Merge updates into data/manager_auth.json (0600), preserving other keys.
    Locked read-modify-write with a pid-unique temp so concurrent admin saves
    can't lose updates or collide on the temp path."""
    if MANAGER_AUTH_FILE is None:
        raise RuntimeError("auth module not initialised (call register_auth first)")
    with _AUTH_WRITE_LOCK:
        cur = auth_runtime()
        cur.update(updates)
        cur["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = f"{MANAGER_AUTH_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(MANAGER_AUTH_FILE))


def auth_credential() -> "tuple[str, str, bool]":
    """(username, password_hash, is_default). UI-managed data/manager_auth.json
    wins (admin-tab edits always take effect); then the TOML [manager.auth]
    (installer provisioning); then the built-in llmadmin/llmadmin default."""
    a = _settings.manager.auth
    rt = auth_runtime()
    if rt.get("password_hash"):
        return (rt.get("username") or a.username or DEFAULT_AUTH_USER), rt["password_hash"], False
    if (a.password_hash or "").strip():
        return (a.username or DEFAULT_AUTH_USER), a.password_hash.strip(), False
    return (a.username or DEFAULT_AUTH_USER), DEFAULT_AUTH_HASH, True


def auth_policy() -> str:
    """The TOML [manager.auth].mode — 'auto' | one of AUTH_RUNTIME_MODES."""
    m = (_settings.manager.auth.mode or "required").strip().lower()
    return m if m in AUTH_MODES else "required"


def auth_mode() -> str:
    """Effective runtime gate mode. Under policy 'auto' the JSON wins (instant,
    UI-managed); otherwise the pinned TOML value wins."""
    policy = auth_policy()
    if policy == "auto":
        m = (auth_runtime().get("mode") or "required").strip().lower()
        return m if m in AUTH_RUNTIME_MODES else "required"
    return policy


def _bypass_role() -> str:
    """Role assigned to login-bypassed sessions (trusted_cidr/disabled)."""
    r = (getattr(_settings.manager.auth, "bypass_role", "admin") or "admin").strip().lower()
    return r if r in ("admin", "operator") else "admin"


def _live_role_for_session() -> "tuple[Optional[str], bool]":
    """Re-derive a logged-in user's role from the user store so disable / delete /
    role-change take effect on the next request, not at cookie expiry. Returns
    (role, valid): valid=False means the session subject no longer exists or is
    disabled (caller logs them out). Falls back to the cookie role when the store
    isn't wired (e.g. unit tests) or the session has no named user (legacy)."""
    user = session.get("user")
    if not user:
        return (session.get("role") or "admin"), True
    import manager_users
    if manager_users.STORE is None:
        return (session.get("role") or "admin"), True
    u = manager_users.STORE.get(user)
    if not u or u.get("disabled"):
        return None, False
    return (u.get("role") or "operator"), True


def effective_role() -> Optional[str]:
    """Requester's role: gate-resolved g.auth_role if set, else resolved from the
    session / bypass mode. None when unauthenticated — so admin gates fail closed
    on agent/anon contexts the gate exempted early (e.g. /api/remote/* handlers).
    Session roles are re-derived from the store (disabled/deleted → None)."""
    r = getattr(g, "auth_role", None)
    if r is not None:
        return r
    mode = auth_mode()
    if mode == "disabled":
        return _bypass_role()
    if mode == "trusted_cidr" and _admin_ip_allowed(flask_request.remote_addr or ""):
        return _bypass_role()
    if session.get("auth_ok") is True:
        role, valid = _live_role_for_session()
        return role if valid else None
    return None


def admin_ip_ok() -> bool:
    """True when the request's remote_addr is within the admin CIDR allowlist."""
    return bool(_admin_ip_allowed(flask_request.remote_addr or ""))


def write_toml_auth_mode(mode: str) -> None:
    """Surgically rewrite `mode = "..."` under [manager.auth] in the live TOML,
    preserving every other line/comment. The manager runs as the file owner
    (llmsys, 0600), so it can write in place. Raises if the file or the
    [manager.auth] section can't be located."""
    if _config_path is None or not Path(_config_path).is_file():
        raise RuntimeError("no TOML config file on disk to update")
    path = Path(_config_path)
    lines = path.read_text().splitlines(keepends=True)
    in_section = False
    replaced = False
    section_re = re.compile(r"^\s*\[([^\]]+)\]")
    mode_re = re.compile(r'^(\s*)mode\s*=.*$')
    for i, line in enumerate(lines):
        sec = section_re.match(line)
        if sec:
            in_section = (sec.group(1).strip() == "manager.auth")
            continue
        if in_section and mode_re.match(line):
            indent = mode_re.match(line).group(1)
            nl = "\n" if line.endswith("\n") else ""
            lines[i] = f'{indent}mode = "{mode}"{nl}'
            replaced = True
            break
    if not replaced:
        raise RuntimeError("could not locate [manager.auth] mode key in TOML")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write("".join(lines))
    os.chmod(tmp, 0o600)
    os.replace(tmp, str(path))


def _operator_denied(path: str) -> bool:
    """True when an operator-role session must be blocked from `path`. Carve-outs
    (picker list, agent infra) are allowed; agent infra reaches the gate as
    open paths and never gets here, but list-by-provider is explicitly allowed."""
    if path == "/api/agents/list-by-provider":
        return False
    if path.startswith("/api/admin/"):
        return True
    if path.startswith("/api/terminal/") or path.startswith("/api/lms/terminal/"):
        return True
    # Exact-or-slash so a future /api/agent-<x> route can't slip the deny set.
    if path == "/api/agents" or path.startswith("/api/agents/"):
        return True
    return False


# ── before_request gate ──────────────────────────────────────────────
def _auth_gate():
    mode = auth_mode()
    path = flask_request.path or "/"
    # Always-open infra paths — never gated, never role-checked.
    if path in AUTH_OPEN_PATHS or path.startswith("/api/remote/"):
        return None
    if _agent_by_token(_bearer_from_request() or ""):
        return None
    if path.endswith("/cert-bundle"):
        return None
    if path.startswith("/api/agents/") and path.endswith("/status"):
        return None
    # Resolve admission + effective role for browser / bypass requests.
    role = None
    if mode == "disabled":
        role = _bypass_role()
    elif mode == "trusted_cidr" and _admin_ip_allowed(flask_request.remote_addr or ""):
        role = _bypass_role()
    elif session.get("auth_ok") is True:
        # Re-derive role from the store so a mid-session disable/delete/demote
        # applies now; an invalidated subject is logged out and falls to 401.
        live_role, valid = _live_role_for_session()
        if not valid:
            session.clear()
        else:
            role = live_role or "admin"
    if role is None:
        # Unauthenticated: same diagnostics + 401/redirect as before.
        if mode == "trusted_cidr":
            global _trusted_cidr_deny_last_log
            _now_deny = time.time()
            if _now_deny - _trusted_cidr_deny_last_log >= 30:
                _trusted_cidr_deny_last_log = _now_deny
                log.info("trusted_cidr: login required — remote_addr=%r not in admin_cidrs=%s",
                         flask_request.remote_addr, _agent_admin_allow())
        if (path.startswith("/api/") or path.startswith("/proxy/")
                or path.startswith("/sdcpp") or path.startswith("/ws/")):
            return jsonify({"ok": False, "error": "authentication required",
                            "auth_required": True}), 401
        return redirect("/login")
    g.auth_role = role
    g.auth_user = session.get("user")
    if role != "admin" and _operator_denied(path):
        if (path.startswith("/api/") or path.startswith("/proxy/")
                or path.startswith("/sdcpp") or path.startswith("/ws/")):
            return jsonify({"ok": False, "error": "operator role: forbidden",
                            "role_denied": True}), 403
        return redirect("/")  # browser nav to an admin-only page → bounce home
    return None


# ── Login page (AE version cache + renderer + needed-here predicate) ─
def _ae_version_cached() -> str:
    """Return the AE's `__version__` as advertised by /health, cached for
    _AE_VERSION_TTL seconds so login renders don't hammer the AE. Empty
    string when AE is unreachable or the field is absent — callers render
    conditionally on truthiness rather than showing a placeholder.

    Two non-obvious bits:

    1. The TTL guard is gated on `fetched_at` ONLY, not on the cached
       value being truthy. If it required truthiness, a sustained AE
       outage (cached value = "") would defeat the rate limiter and
       every login render would block on a fresh 1.5s probe. Stamping
       fetched_at on the failure path is the deliberate negative-cache
       that protects /login from a downed-AE storm; the cost is up to
       TTL seconds of stale "" after AE recovery before the next probe
       reflects it.

    2. The lock is non-blocking: when TTL has expired and another worker
       is already probing, fresh callers return the stale cache instead
       of queueing on a 1.5s HTTP call. Login pages render fast under
       a stampede; only one thread eats the probe latency."""
    now = time.time()
    last = float(_AE_VERSION_CACHE.get("fetched_at") or 0.0)
    if now - last < _AE_VERSION_TTL and last > 0.0:
        return str(_AE_VERSION_CACHE.get("version") or "")
    if not _AE_VERSION_LOCK.acquire(blocking=False):
        # Another worker is refreshing; return whatever's cached (possibly
        # stale or empty — both are acceptable for this UI surface).
        return str(_AE_VERSION_CACHE.get("version") or "")
    try:
        # Re-check inside lock — another worker may have just refreshed
        # between our outer TTL check and our lock acquisition.
        last = float(_AE_VERSION_CACHE.get("fetched_at") or 0.0)
        if now - last < _AE_VERSION_TTL and last > 0.0:
            return str(_AE_VERSION_CACHE.get("version") or "")
        ae_url = _alarm_engine_url() or ""
        if not ae_url:
            _AE_VERSION_CACHE["fetched_at"] = time.time()
            _AE_VERSION_CACHE["version"] = ""
            return ""
        try:
            r = _ae_session.get(ae_url.rstrip("/") + "/health", timeout=1.5)
            v = (r.json() or {}).get("version") if r.ok else ""
        except Exception:
            v = ""
        _AE_VERSION_CACHE["version"] = v or ""
        _AE_VERSION_CACHE["fetched_at"] = time.time()
        return v or ""
    finally:
        _AE_VERSION_LOCK.release()


def _render_login(error: str = "") -> str:
    err_html = (f'<div class="err">{error}</div>' if error else "")
    p = _brand_palette()
    logo = _brand_logo_svg(p)
    # AE half elided rather than blank on outage — a transient outage
    # shouldn't surface as a missing-version UI artifact. The two-column
    # grid keeps labels and version strings aligned; white-space:nowrap on
    # the value column prevents hyphenated version strings (e.g. -11) from
    # wrapping mid-token at narrow widths.
    mgr_v = html.escape(_manager_version)
    ae_v  = html.escape(_ae_version_cached())
    rows = [f'<span class="lbl">Manager</span><span class="val">{mgr_v}</span>']
    if ae_v:
        rows.append(f'<span class="lbl">Alarm Engine</span><span class="val">{ae_v}</span>')
    versions_html = f'<div class="versions">{"".join(rows)}</div>'
    accent, btn_text, glow, grad = p["accent"], p["btn_text"], p["glow"], p["grad_top"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LLM Systems Manager</title>
<style>
  :root {{ --ac:{accent}; --glow:{glow}; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background: radial-gradient(1100px 560px at 50% -12%, {grad} 0%, #0c0f13 58%);
         color:#e7edf3; font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  .wrap {{ width:min(420px, 92vw); padding:0 16px; text-align:center; }}
  .brand {{ display:flex; flex-direction:column; align-items:center; gap:16px; margin-bottom:30px; }}
  .logo {{ filter: drop-shadow(0 6px 18px rgba(var(--glow),.35)); line-height:0; }}
  .appname {{ margin:0; font-size:2.15rem; font-weight:750; letter-spacing:.2px; line-height:1.1; }}
  .appname .accent {{ color: var(--ac); }}
  .tagline {{ margin:9px 0 0; font-size:.74rem; letter-spacing:.22em; text-transform:uppercase; color:#7b8a99; }}
  .card {{ background:#11161c; border:1px solid #232c36; border-radius:14px;
          padding:24px 26px; box-shadow:0 24px 60px rgba(0,0,0,.55); text-align:left; }}
  label {{ display:block; font-size:.7rem; text-transform:uppercase; letter-spacing:.1em;
           color:#8b98a8; margin:0 0 6px; }}
  input {{ width:100%; padding:12px 13px; margin:0 0 16px; background:#0b0e12; color:#e7edf3;
           border:1px solid #2a333d; border-radius:9px; font-size:.95rem; outline:none;
           transition:border-color .15s, box-shadow .15s; }}
  input:focus {{ border-color: var(--ac); box-shadow:0 0 0 3px rgba(var(--glow),.25); }}
  button {{ width:100%; padding:12px; margin-top:4px; background: var(--ac); color:{btn_text};
            border:0; border-radius:9px; font-size:.98rem; font-weight:650; cursor:pointer;
            transition:filter .15s; }}
  button:hover {{ filter:brightness(1.08); }}
  .err {{ background:#3a1f24; border:1px solid #7a3540; color:#f3b0b8; padding:9px 11px;
          border-radius:8px; font-size:.82rem; margin-bottom:16px; }}
  .versions {{ margin:20px auto 0; width:max-content;
               display:grid; grid-template-columns:auto auto; column-gap:14px; row-gap:3px;
               font-family: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace;
               font-variant-numeric: tabular-nums; }}
  .versions .lbl {{ color:#4a5563; text-align:right; text-transform:uppercase;
                    font-size:.6rem; letter-spacing:.14em; align-self:center;
                    white-space:nowrap; }}
  .versions .val {{ color:#6b7785; font-size:.72rem; letter-spacing:.02em;
                    align-self:center; white-space:nowrap; }}
</style></head><body>
  <div class="wrap">
    <div class="brand">
      <span class="logo">{logo}</span>
      <div>
        <h1 class="appname">LLM Systems <span class="accent">Manager</span></h1>
        <div class="tagline">Observability &amp; Control</div>
      </div>
    </div>
    <form class="card" method="POST" action="/login">
      {err_html}
      <label for="lf-user">Username</label>
      <input id="lf-user" name="username" type="text" autocomplete="username" required autofocus>
      <label for="lf-pass">Password</label>
      <input id="lf-pass" name="password" type="password" autocomplete="current-password" required>
      <button type="submit">Log&nbsp;in</button>
    </form>
    {versions_html}
  </div>
</body></html>"""


def _login_page_needed() -> bool:
    """Whether the login form should actually be shown for THIS request. It
    shouldn't in `disabled` mode, nor for a `trusted_cidr` request from an
    allowed IP — in those cases the gate admits the request anyway, so a login
    page (e.g. reached via /login directly or after /logout) is a confusing
    dead-end. Only `required`, or `trusted_cidr` from an untrusted IP, needs it."""
    mode = auth_mode()
    if mode == "disabled":
        return False
    if mode == "trusted_cidr" and _admin_ip_allowed(flask_request.remote_addr or ""):
        return False
    return True


# ── Route handlers ───────────────────────────────────────────────────
def _manager_login():
    if flask_request.method == "GET":
        # Don't strand the operator on a login page when this request wouldn't
        # be gated anyway (disabled / trusted-from-allowed-IP) — send them in.
        if not _login_page_needed():
            return redirect("/")
        return _render_login()
    form = flask_request.form
    username = (form.get("username") or "").strip()
    pw = form.get("password") or ""
    import manager_users  # lazy: avoids an import cycle (manager_users imports auth)
    res = manager_users.authenticate(username, pw, flask_request.remote_addr or "")
    if res.get("ok"):
        session.permanent = True
        session["auth_ok"] = True
        session["user"] = res["username"]
        session["role"] = res["role"]
        log.info("manager login OK (user=%s role=%s) from %s",
                 res["username"], res["role"], flask_request.remote_addr)
        return redirect("/")
    if res.get("locked"):
        log.warning("manager login LOCKED (user=%s) from %s", username, flask_request.remote_addr)
        return _render_login(error="Too many attempts. Try again later."), 429
    log.warning("manager login FAILED (user=%s) from %s", username, flask_request.remote_addr)
    return _render_login(error="Invalid username or password."), 401


def _manager_logout():
    session.clear()
    # Redirect to the app, not /login — the gate then decides. In required mode
    # that lands on the login form; in disabled / trusted_cidr it admits the
    # request (so "Log out" doesn't dump the operator onto a dead-end prompt).
    return redirect("/")


def _admin_auth_get():
    deny = _require_admin()
    if deny is not None:
        return deny
    policy = auth_policy()
    # is_default reflects the LOGIN store (manager_users.json) — the legacy
    # auth_credential() store is no longer what /login authenticates against.
    import manager_users
    is_default = bool(manager_users.STORE is not None and manager_users.STORE.is_default_admin(
        DEFAULT_AUTH_USER, lambda h: scrypt_verify(DEFAULT_AUTH_PASSWORD, h)))
    return jsonify({"ok": True, "mode": auth_mode(), "policy": policy,
                    "instant": policy == "auto", "is_default": is_default,
                    "modes": list(AUTH_MODES), "current_user": session.get("user")})


def _admin_auth_set():
    """Change the dashboard auth MODE from the admin tab.

    Credential management moved to the Users card / account menu (the
    manager_users store that /login authenticates against). This route is
    mode-only and no longer writes credentials — the old password write hit the
    legacy manager_auth.json that login no longer consults (#125 divergence)."""
    deny = _require_admin()
    if deny is not None:
        return deny
    body = flask_request.get_json(silent=True) or {}
    updates: dict = {}
    restart_required = False
    mode_changed = False
    requested_mode: Optional[str] = None
    if body.get("mode") is not None:
        requested_mode = str(body["mode"]).strip().lower()
        if requested_mode not in AUTH_MODES:
            return jsonify({"ok": False,
                            "error": f"invalid mode (use one of {', '.join(AUTH_MODES)})"}), 400
        policy = auth_policy()
        # Instant only when the policy hands control to the JSON and the chosen
        # value is an actual runtime mode. Selecting 'auto', or changing the mode
        # while the TOML is pinned, edits the TOML and needs a restart. No-op when
        # the value already matches (the UI always echoes the current mode).
        if policy == "auto" and requested_mode != "auto":
            if requested_mode != auth_mode():
                updates["mode"] = requested_mode
                mode_changed = True
        elif requested_mode != policy:
            try:
                write_toml_auth_mode(requested_mode)
            except Exception as e:
                log.warning("auth mode write failed: %s: %s", type(e).__name__, e)
                return jsonify({"ok": False,
                                "error": "could not write config file"}), 500
            restart_required = True
            mode_changed = True
    if not updates and not restart_required:
        return jsonify({"ok": False, "error": "nothing to update"}), 400
    if updates:
        auth_write(updates)
    if mode_changed:
        log.warning("manager auth mode set to '%s' (%s) via admin tab from %s",
                    requested_mode, "TOML/restart" if restart_required else "json/instant",
                    flask_request.remote_addr)
    return jsonify({"ok": True, "mode": auth_mode(), "policy": auth_policy(),
                    "restart_required": restart_required,
                    "restart_cmd": "sudo systemctl restart llm-systems-manager"})


# ── Public registration ──────────────────────────────────────────────
def register_auth(app, ctx, *,
                  config_path: "Path | str | None",
                  agent_by_token: Callable[[str], Any],
                  bearer_from_request: Callable[[], Optional[str]],
                  brand_palette: Callable[[], dict],
                  brand_logo_svg: Callable[[dict, int], str]) -> None:
    """Wire the auth gate + routes into `app`.

    Call once at startup after `ctx` is fully populated. The
    @before_request gate registers immediately; routes serve once
    Flask is running. Module-specific kwargs are the ones only auth
    reads (login-page brand assets, the TOML rewrite path for mode
    changes, and the two agent-token resolvers — all the cross-module
    shared deps come from `ctx`).
    """
    global MANAGER_AUTH_FILE, DEFAULT_AUTH_HASH
    global _settings, _manager_version, _config_path
    global _agent_by_token, _bearer_from_request, _admin_ip_allowed
    global _require_admin, _brand_palette, _brand_logo_svg
    global _agent_admin_allow, _ae_session, _alarm_engine_url

    MANAGER_AUTH_FILE = Path(ctx.data_dir) / "manager_auth.json"
    DEFAULT_AUTH_HASH = scrypt_hash(DEFAULT_AUTH_PASSWORD)

    _settings = ctx.settings
    _manager_version = ctx.version
    _config_path = Path(config_path) if config_path is not None else None
    _agent_by_token = agent_by_token
    _bearer_from_request = bearer_from_request
    _admin_ip_allowed = ctx.admin_ip_allowed
    _require_admin = ctx.require_admin
    _brand_palette = brand_palette
    _brand_logo_svg = brand_logo_svg
    _agent_admin_allow = ctx.agent_admin_allow
    _ae_session = ctx.ae_session
    _alarm_engine_url = ctx.alarm_engine_url

    app.before_request(_auth_gate)
    app.add_url_rule("/login", endpoint="manager_login",
                     view_func=_manager_login, methods=["GET", "POST"])
    app.add_url_rule("/logout", endpoint="manager_logout",
                     view_func=_manager_logout, methods=["GET", "POST"])
    app.add_url_rule("/api/admin/auth", endpoint="admin_auth_get",
                     view_func=_admin_auth_get, methods=["GET"])
    app.add_url_rule("/api/admin/auth", endpoint="admin_auth_set",
                     view_func=_admin_auth_set, methods=["POST"])
