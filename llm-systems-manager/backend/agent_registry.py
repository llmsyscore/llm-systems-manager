"""Agent registry for the LLM Systems Manager.

Admin-managed self-registration + bearer-token approval. Owns:
- data/agents.json (read/write under threading locks; mtime-cached by-token index)
- /api/agents/* routes (register, status, whoami, heartbeat, approve, disable,
  delete, role-primary, llama-state, llama-pool, cert-bundle, stream-token,
  collection, global, the agents-list GET)
- /api/admin/push-ca-to-agents (force-rotate every approved agent's TLS bundle)
- Heartbeat ack assembly: manager_https_url, ingest_token, alarm_engine_url,
  proxy flags, primary-llama signal, the conditional TLS bundle issuance.
- TLS bundle issuance (_maybe_issue_tls_bundle) — sits in the heartbeat hot path,
  uses the manager's internal CA via the _pki module to sign per-agent leaf certs.
- Agent dialing helpers consumed by terminal / proxies / openclaw in PRs M3-M5:
  agent_callback_urls, agent_tls_kwargs, agent_request (multi-fallback dialer),
  primary_agent, pick_llama_agent (round-robin), browser_reachable_bind_url,
  is_local_bind_url, agent_liveness, approved_agent_caps.

Wired into the Flask app by main via register_routes(app); deps populated
via set_deps(ctx, ...). Shared cross-module deps (settings, data_dir,
version, require_admin, admin_ip_allowed, agent_admin_allow, alarm_engine_url,
manager_secret, ae_session) come from `app_context.Context`; module-specific
deps (heartbeat-ack helpers, llama wake/idle controls, infra-version cache,
PKI loader) stay as explicit kwargs. Main retains ownership of the brand
palette, manager TLS / AE TLS cert issuance, and the _pki lazy-loader
itself — agent_registry calls into main for those through ctx + kwargs,
the same pattern auth.py uses.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import secrets as _secrets
import subprocess
import threading
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import requests
from flask import Response, jsonify, request as flask_request

import provider_state  # type: ignore[import-not-found]  # sibling — leaf module, no cycle
from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling

log = logging.getLogger("llm-systems-manager.agent_registry")

__all__ = [
    "register_routes",
    "set_deps",
    "load_agents",
    "save_agents",
    "agent_by_token",
    "bearer_from_request",
    "agent_auth_gate",
    "browser_reachable_bind_url",
    "is_local_bind_url",
    "agent_callback_urls",
    "agent_tls_kwargs",
    "agent_request",
    "primary_agent",
    "pick_llama_agent",
    "pinned_llama_agent",
    "resolve_agent_by_id",
    "default_agent_id_for",
    "agent_liveness",
    "agent_host_keys",
    "colocated_infra",
    "approved_agent_caps",
    "issue_stream_token",
    "ingest_token_for_agent",
    "AGENTS_FILE",
]

# ── Module-level state ───────────────────────────────────────────────
# Path is set by set_deps(); the constant name is exported so test fixtures
# can monkey-patch it to a tmp_path file, mirroring the auth module pattern.
AGENTS_FILE: Optional[Path] = None

_agents_lock = threading.Lock()
# RLock so load_agents() can call save_agents() (which re-acquires this lock)
# from inside its own critical section during the v2 schema migration.
_agents_cache_lock = threading.RLock()
_agents_cache: "dict[str, Any]" = {"mtime": 0.0, "data": None, "by_token": {}}

# Public aliases: main keeps a few admin routes that mutate agents.json
# (llama-pins, post-restart auto-promotion) and need to coordinate with
# the same lock + reference the cert-format invariant. Exporting them as
# public symbols keeps those callsites readable without reaching into
# underscore-prefixed module privates.
agents_lock = _agents_lock

_llama_rr_lock = threading.Lock()
_llama_rr_index = 0

# Bound the manager->agent dial so an unreachable/slow callback URL fails fast
# instead of pinning a Cheroot worker thread for the full read timeout (xN
# callback URLs tried serially). Connect is capped at this; the caller's read
# timeout is preserved for slow-but-alive operations.
_AGENT_CONNECT_TIMEOUT_S = 4.0

# Throttle agents.json disk writes on the heartbeat hot path. The in-memory
# registry cache is mutated in place every beat (liveness reads stay fresh);
# the file is flushed on a material change or at most once per this interval.
_HB_FLUSH_MIN_INTERVAL_S = 30.0
_hb_last_flush_at = 0.0

# Both blank and the "REPLACE_ME" placeholder mean "ingest is open" on the AE
# side (api/auth.py:_UNSET) — normalize to blank here so agents don't carry
# a meaningless bearer over the wire when the operator hasn't filled it in.
_INGEST_TOKEN_UNSET = {"", "REPLACE_ME"}

# The "issued before this timestamp → reissue" invariant lives on the
# _pki module itself (AKI_FIX_TS) because three different issuers consult
# it: agent_registry (here), the manager's HTTPS server cert in main, and
# the AE TLS cert in main. Centralizing the constant in _pki keeps the
# three issuers reading from one source of truth.

# ── Cross-module deps (populated by set_deps) ────────────────────────
_deps = SimpleNamespace(
    settings=None,                # config.unified_config.settings singleton
    data_dir=None,                # Path
    version="",                   # manager __version__ string
    require_admin=lambda: None,   # admin-gate callable (returns Response or None)
    admin_ip_allowed=lambda _ip: False,
    agent_admin_allow=lambda: [],
    alarm_engine_url=lambda: "",  # getter; URL may auto-upgrade after registration
    request_host_no_port=lambda: "",
    rewrite_loopback_host=lambda url, host: url,
    set_llama_awake=lambda _b: None,
    get_interval=lambda: 0,
    manager_secret=lambda: b"",
    latest_agent_version=lambda: None,
    refresh_infra_versions=lambda: None,
    infra_version_get=lambda _k: None,
    hostname="",
    loopback_hosts=frozenset(),
    pki_ensure_ca=lambda: (None, None, None),
)


# ── Public API: agents.json read/write ──────────────────────────────
def _migrate_agents_schema(data: dict) -> bool:
    """In-place schema migration. Returns True if anything changed.
    v2: copy global.primary_<p>_id → global.default_<p>_id (additive alias);
    legacy keys stay so old code keeps working. Idempotent."""
    changed = False
    if data.get("schema_version") != 2:
        data["schema_version"] = 2
        changed = True
    g = data.setdefault("global", {})
    for name in ("llama", "lms"):
        prim_key = f"primary_{name}_id"
        def_key = f"default_{name}_id"
        if g.get(prim_key) and not g.get(def_key):
            g[def_key] = g[prim_key]
            changed = True
    return changed


def load_agents() -> dict:
    if AGENTS_FILE is None:
        return {"agents": {}, "global": {"auth_disabled": False}, "schema_version": 2}
    try:
        mt = AGENTS_FILE.stat().st_mtime
    except OSError:
        # File missing — return the empty-state default each call. Not
        # cached because mtime is unobservable, and the empty-state case
        # is rare + cheap.
        return {"agents": {}, "global": {"auth_disabled": False}, "schema_version": 2}
    with _agents_cache_lock:
        if mt == _agents_cache["mtime"] and _agents_cache["data"] is not None:
            return _agents_cache["data"]
        try:
            data = json.loads(AGENTS_FILE.read_text())
        except FileNotFoundError:
            data = {"agents": {}, "global": {"auth_disabled": False}, "schema_version": 2}
        except Exception as e:
            log.warning("agents.json unreadable, starting fresh: %s", e)
            data = {"agents": {}, "global": {"auth_disabled": False}, "schema_version": 2}
        # Apply v2 migration in-place; persist if anything was upgraded.
        try:
            if _migrate_agents_schema(data):
                save_agents(data)
                try:
                    mt = AGENTS_FILE.stat().st_mtime
                except OSError:
                    pass
                log.info("agents.json migrated to schema_version=2")
        except Exception as e:
            log.warning("agents.json schema migration failed: %s", e)
        _agents_cache["data"] = data
        _agents_cache["mtime"] = mt
        _agents_cache["by_token"] = {
            a["token"]: a
            for a in data.get("agents", {}).values()
            if a.get("token") and a.get("status") == "approved"
        }
        return data


def save_agents(data: dict) -> None:
    if AGENTS_FILE is None:
        raise RuntimeError("agent_registry not initialised (call set_deps first)")
    AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AGENTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    # Bearer tokens for every approved agent live in this file — restrict to
    # owner-only so a misconfigured shared host doesn't leak them. The chmod
    # runs on the tmp before os.replace so the rename is atomic AND the
    # destination's mode lands as 0o600 in one shot, matching auth.auth_write's
    # convention for data/manager_auth.json.
    import os as _os
    _os.chmod(tmp, 0o600)
    tmp.replace(AGENTS_FILE)
    # Refresh the cache eagerly so the next load_agents inside this
    # process sees its own write without paying for another mtime miss
    # → reparse cycle.
    with _agents_cache_lock:
        try:
            _agents_cache["mtime"] = AGENTS_FILE.stat().st_mtime
        except OSError:
            _agents_cache["mtime"] = 0.0
        _agents_cache["data"] = data
        _agents_cache["by_token"] = {
            a["token"]: a
            for a in data.get("agents", {}).values()
            if a.get("token") and a.get("status") == "approved"
        }


# ── Public API: bearer-token auth helpers ────────────────────────────
def agent_by_token(token: str) -> "dict | None":
    """O(1) lookup via the cached reverse index built in load_agents.
    Calls load_agents first to ensure the mtime check fires + the index
    is fresh, then reads the {token → agent} dict under the lock."""
    if not token:
        return None
    load_agents()
    with _agents_cache_lock:
        return _agents_cache["by_token"].get(token)


def bearer_from_request() -> "str | None":
    h = flask_request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[len("Bearer "):].strip()
    return None


def agent_auth_gate() -> "tuple[bool, dict | None]":
    """Validate the bearer token against the registry.

    Returns (ok, agent_dict_or_None). Requires a valid Bearer token from an
    approved agent unless the global `auth_disabled` flag is set.
    """
    data = load_agents()
    if data.get("global", {}).get("auth_disabled"):
        return True, None
    h = flask_request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return False, None
    token = h[len("Bearer "):].strip()
    for agent in data.get("agents", {}).values():
        if agent.get("token") == token:
            if agent.get("status") == "approved":
                return True, agent
            return False, agent
    return False, None


# ── Public API: bind_url helpers ─────────────────────────────────────
def browser_reachable_bind_url(agent: dict) -> str:
    """Return the agent's bind_url with the hostname swapped for the
    agent's registered IP when the host portion isn't already an IP
    literal.

    The agent's bind_url usually comes from socket.gethostname() on
    the agent host — fine for agent-to-agent calls but useless for a
    browser that doesn't know that hostname. We substitute the IP we
    saw the agent register from so the browser-side EventSource can
    connect without needing the agent's name in DNS / /etc/hosts.

    Falls back to the raw bind_url if anything is missing or unparsable.
    """
    bind_url = (agent.get("bind_url") or "").rstrip("/")
    if not bind_url:
        return ""
    try:
        from urllib.parse import urlparse, urlunparse
        from ipaddress import ip_address
        p = urlparse(bind_url)
        host = p.hostname or ""
        if not host:
            return bind_url
        # Already an IP literal — leave it alone.
        try:
            ip_address(host)
            return bind_url
        except ValueError:
            pass
        rfrom = (agent.get("registered_from") or "").strip()
        if not rfrom or ":" in rfrom and not rfrom.startswith("["):
            # ipv6 without brackets or missing — bail to raw URL.
            return bind_url
        port = f":{p.port}" if p.port else ""
        netloc = f"{rfrom}{port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment)).rstrip("/")
    except Exception:
        return bind_url


def is_local_bind_url(bind_url: str) -> bool:
    """True when the agent advertises a loopback or this-host URL — eligible for auto-approval.

    Loopback check uses _deps.loopback_hosts (the same frozenset main passes in
    for colocated_infra) so the set stays single-sourced — `0.0.0.0` and `""`
    count as loopback alongside the obvious ones. Host comparison is
    case-insensitive: socket.gethostname() can return mixed case after a
    `hostnamectl set-hostname` and urlparse preserves it, so a strict `==`
    would diverge from the agent_host_keys / colocated_infra paths.
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(bind_url).hostname or "").lower()
    except Exception:
        return False
    if host in _deps.loopback_hosts:
        return True
    return host == _deps.hostname.lower()


def agent_callback_urls(agent: dict) -> list:
    out = []
    bind = (agent.get("bind_url") or "").rstrip("/")
    src = (agent.get("registered_from") or "").strip()
    hn = (agent.get("hostname") or "").strip().lower()

    parsed = None
    bind_host = ""
    bind_host_lower = ""
    if bind:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(bind)
            bind_host = (parsed.hostname or "").strip()
            bind_host_lower = bind_host.lower()
        except Exception:
            parsed = None

    def _safe_bind_host(host_lower: str) -> bool:
        # All comparisons case-insensitive so a mixed-case hostname (from
        # socket.gethostname() on a Windows-style host, or hostnamectl with
        # capitalization) doesn't disagree with agent_host_keys, which lowercases.
        # Loopback set is sourced from _deps.loopback_hosts — the same constant
        # main owns — so 0.0.0.0 and "" count alongside 127.0.0.1/::1/localhost.
        if not host_lower:
            return False
        if host_lower in _deps.loopback_hosts:
            return True
        if src and host_lower == src.lower():
            return True
        if hn and host_lower == hn:
            return True
        return False

    if bind and _safe_bind_host(bind_host_lower):
        out.append(bind)

    if src and parsed is not None:
        with best_effort("agent callback: build src fallback url", log=log):
            from urllib.parse import urlunparse
            if bind_host_lower != src.lower():
                netloc = f"{src}:{parsed.port}" if parsed.port else src
                fallback = urlunparse((parsed.scheme or "https", netloc, parsed.path or "", "", "", ""))
                fallback = fallback.rstrip("/")
                if fallback not in out:
                    out.append(fallback)
    return out


def agent_tls_kwargs(url: str) -> dict:
    if not url.startswith("https://"):
        return {}
    ca_path = (_deps.data_dir or Path(".")) / "internal-ca.crt"
    return {"verify": str(ca_path)} if ca_path.is_file() else {}


def agent_request(method: str, agent: dict, path: str, **kwargs
                  ) -> "tuple[requests.Response | None, list, str | None]":
    urls = agent_callback_urls(agent)
    if not urls:
        return None, [], "no callback URL recorded"
    # Cap connect time so a dead callback URL fails in seconds, not the full
    # read timeout. A bare scalar timeout becomes (connect, read); an explicit
    # (connect, read) tuple is left as the caller set it. No timeout at all
    # would mean "block forever" in requests, so default the read leg to 30s.
    raw_to = kwargs.pop("timeout", 30)
    if isinstance(raw_to, (int, float)):
        kwargs["timeout"] = (min(float(raw_to), _AGENT_CONNECT_TIMEOUT_S), float(raw_to))
    else:
        kwargs["timeout"] = raw_to
    last_err = None
    tried = []
    for base in urls:
        full = f"{base}{path}"
        tried.append(full)
        call_kwargs = dict(kwargs)
        if "verify" not in call_kwargs:
            call_kwargs.update(agent_tls_kwargs(full))
        try:
            resp = requests.request(method, full, **call_kwargs)
            return resp, tried, None
        except Exception as e:
            last_err = f"{full}: {type(e).__name__}: {e}"
            continue
    return None, tried, last_err


# ── Public API: liveness + capability rollups ────────────────────────
def agent_liveness(agent: dict) -> str:
    """Returns 'live' | 'stale' | 'down' | 'pending' | 'disabled'."""
    status = agent.get("status")
    if status == "pending":
        return "pending"
    if status == "disabled":
        return "disabled"
    last = agent.get("last_heartbeat")
    if not last:
        return "down"
    try:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return "down"
    if age >= _deps.settings.manager.agents.down_after_s:
        return "down"
    if age >= _deps.settings.manager.agents.stale_after_s:
        return "stale"
    return "live"


def agent_host_keys(agent: dict) -> "set[str]":
    """Lowercase set of host identifiers for an agent: hostname,
    short hostname, bind_url host, and registered_from IP. Used to
    decide whether a service URL points at the same machine."""
    keys: "set[str]" = set()
    for h in (agent.get("hostname"), agent.get("registered_from")):
        if h:
            h = str(h).strip().lower()
            keys.add(h)
            if "." in h and not h.replace(".", "").isdigit():
                keys.add(h.split(".", 1)[0])
    with best_effort("agent host keys: parse bind_url host", log=log):
        from urllib.parse import urlparse
        bh = (urlparse(agent.get("bind_url") or "").hostname or "").lower()
        if bh:
            keys.add(bh)
    return {k for k in keys if k}


def colocated_infra(agent: dict) -> "list[dict]":
    """Return [{role, version}, ...] for core services running on the
    same host as this agent. Empty list when nothing's colocated.

    A service is "colocated" when its configured URL resolves to the
    same host the agent registered from. Special case: this manager
    process is colocated with every agent whose host keys overlap with
    this host's gethostname()/loopback set."""
    from urllib.parse import urlparse
    out: "list[dict]" = []
    keys = agent_host_keys(agent)
    if not keys:
        return out

    local_keys = set(_deps.loopback_hosts) | {
        _deps.hostname.lower(), _deps.hostname.split(".", 1)[0].lower(),
    }

    def _matches(svc_host: str) -> bool:
        svc = (svc_host or "").lower()
        if not svc:
            return False
        if svc in keys:
            return True
        # Service configured as localhost → it lives on the manager
        # host; only matches agents that are also on the manager host.
        if svc in _deps.loopback_hosts:
            return bool(keys & local_keys)
        return False

    # Manager itself — by definition runs on this host.
    if keys & local_keys:
        out.append({"role": "manager", "version": _deps.version})

    ae_url = _deps.alarm_engine_url() or ""
    if ae_url:
        ae_host = urlparse(ae_url).hostname or ""
        if _matches(ae_host):
            out.append({"role": "alarm_engine",
                        "version": _deps.infra_version_get("ae")})

    ix_host = _deps.settings.influxdb.host or ""
    if _matches(ix_host):
        out.append({"role": "influxdb",
                    "version": _deps.infra_version_get("influxdb")})

    return out


def approved_agent_caps() -> dict:
    """Capability + hostname rollup. {llama, lms, either} are booleans
    used by `/` (inline-hide injection) + `/api/config` (polled
    refresh). llama_host / lms_host carry the first approved agent's
    hostname for each capability so the frontend can backfill that
    agent's charts from the alarm engine catalog without hitting the
    admin-gated /api/agents endpoint."""
    approved = [
        a for a in ((load_agents().get("agents") or {}).values())
        if a.get("status") == "approved"
    ]
    llama = any((a.get("capabilities") or {}).get("llama") for a in approved)
    lms   = any((a.get("capabilities") or {}).get("lms")   for a in approved)
    llama_host = next(
        (a.get("hostname") for a in approved if (a.get("capabilities") or {}).get("llama")),
        None,
    )
    lms_host = next(
        (a.get("hostname") for a in approved if (a.get("capabilities") or {}).get("lms")),
        None,
    )
    return {"llama": llama, "lms": lms, "either": llama or lms,
            "llama_host": llama_host, "lms_host": lms_host}


def self_agent_id() -> "str | None":
    """agent_id of the approved agent running on the manager's own host, so
    the frontend can scope the manager-host cards by agent id (not hostname).
    None when no such agent is registered (caller degrades to no host filter)."""
    local_keys = set(_deps.loopback_hosts) | {
        _deps.hostname.lower(), _deps.hostname.split(".", 1)[0].lower(),
    }
    for aid, a in ((load_agents().get("agents") or {}).items()):
        if a.get("status") != "approved":
            continue
        if agent_host_keys(a) & local_keys:
            return aid
    return None


# ── Public API: primary-agent resolution ─────────────────────────────
def primary_agent(kind: str) -> "dict | None":
    data = load_agents()
    glob = data.get("global") or {}

    if kind == "llama":
        pool = glob.get("llama_pool") or []
        if pool:
            agents_map = data.get("agents") or {}
            for aid in pool:
                a = agents_map.get(aid)
                if a and a.get("status") == "approved":
                    return a
            # Fall through if pool exists but none is approved — let the
            # legacy primary_llama_id lookup below take over.

    aid = glob.get(f"primary_{kind}_id")
    if not aid:
        return None
    agent = (data.get("agents") or {}).get(aid)
    if not agent or agent.get("status") != "approved":
        return None
    return agent


def default_agent_id_for(provider: str) -> "str | None":
    """Pick the default agent_id for a provider. Reads global.default_<p>_id
    first (the new key PR4's "Set as Default" UI will write), falls back to
    global.primary_<p>_id (legacy), then to the first approved agent with the
    matching capability. Returns None if nothing matches."""
    data = load_agents()
    glob = data.get("global") or {}
    agents_map = data.get("agents") or {}
    aid = glob.get(f"default_{provider}_id") or glob.get(f"primary_{provider}_id")
    if aid:
        a = agents_map.get(aid)
        if a and a.get("status") == "approved":
            return aid
    cap_key = provider
    for a in agents_map.values():
        if a.get("status") != "approved":
            continue
        if (a.get("capabilities") or {}).get(cap_key):
            return a.get("agent_id")
    return None


def _set_provider_default(glob: dict, kind: str, agent_id: str) -> None:
    """Write the dashboard default for a provider. Keeps the new
    default_<kind>_id and the legacy primary_<kind>_id in lockstep so
    default_agent_id_for (reads default first) and any legacy primary reader
    never diverge. Pass "" to clear both."""
    glob[f"default_{kind}_id"] = agent_id
    glob[f"primary_{kind}_id"] = agent_id


def _default_for_agent(data: dict, agent_id: str,
                       provider_names: "list[str] | None" = None) -> "list[str]":
    """Returns the list of provider names this agent is the default for.
    Reads global.default_<p>_id first, falls back to global.primary_<p>_id
    so a manual TOML/JSON edit to either key keeps working."""
    g = data.get("global") or {}
    names = provider_names if provider_names is not None else ["llama", "lms"]
    out: "list[str]" = []
    for name in names:
        sel = g.get(f"default_{name}_id") or g.get(f"primary_{name}_id")
        if sel == agent_id:
            out.append(name)
    return out


def resolve_agent_by_id(agent_id: str,
                        capability: "str | None" = None) -> "dict | None":
    """Return the approved agent with this id, optionally requiring a
    capability. Returns None for unknown / not-approved / missing-capability
    so callers fall through to their default selector — a bad ?agent= param
    degrades to "default agent", never an error."""
    if not agent_id:
        return None
    data = load_agents()
    a = (data.get("agents") or {}).get(agent_id)
    if not a or a.get("status") != "approved":
        return None
    if capability and not (a.get("capabilities") or {}).get(capability):
        return None
    return a


def pinned_llama_agent(model_id: "str | None") -> "dict | None":
    """Pin-only lookup: the agent a model is pinned to if it's approved+live,
    else None. No pool/legacy fallback — the caller decides what to do when a
    pin is absent or unavailable. pick_llama_agent uses this for step 1."""
    if not model_id:
        return None
    data = load_agents()
    glob = data.get("global") or {}
    agents_map = data.get("agents") or {}
    pinned_id = (glob.get("llama_model_pins") or {}).get(model_id)
    if not pinned_id:
        return None
    a = agents_map.get(pinned_id)
    if a and a.get("status") == "approved" and agent_liveness(a) == "live":
        return a
    log.warning("model %s pinned to agent %s but it's not approved+live "
                "(status=%s liveness=%s); falling back",
                model_id, pinned_id,
                (a or {}).get("status"),
                agent_liveness(a) if a else "?")
    return None


def pick_llama_agent(model_id: "str | None" = None) -> "dict | None":
    data = load_agents()
    glob = data.get("global") or {}
    agents_map = data.get("agents") or {}

    # 1. Per-model pinning takes precedence.
    if model_id:
        pinned = pinned_llama_agent(model_id)
        if pinned:
            return pinned

    # 2. Pool round-robin. Filter to approved+live first; if every pool
    #    member is stale (e.g. brief manager-restart window where no
    #    heartbeats have landed yet), accept approved-but-not-live so
    #    we don't bounce everything to the legacy fallback.
    pool = glob.get("llama_pool") or []
    approved      = [agents_map[a] for a in pool
                     if a in agents_map and agents_map[a].get("status") == "approved"]
    approved_live = [a for a in approved if agent_liveness(a) == "live"]
    candidates = approved_live or approved
    if candidates:
        global _llama_rr_index
        with _llama_rr_lock:
            idx = _llama_rr_index % len(candidates)
            _llama_rr_index = (idx + 1) % len(candidates)
        return candidates[idx]

    # 3. Legacy single-primary fallback.
    return primary_agent("llama")


# ── Public API: ingest token + stream token ──────────────────────────
def ingest_token_for_agent() -> str:
    tok = (_deps.settings.alarm_engine.ingest_token or "").strip()
    return "" if tok in _INGEST_TOKEN_UNSET else tok


def issue_stream_token(agent_id: str, path: str, ttl: "int | None" = None) -> str:
    if ttl is None:
        ttl = _deps.settings.manager.security.stream_token_ttl_s
    expiry = int(time.time()) + ttl
    msg = f"{agent_id}|{path}|{expiry}".encode()
    sig = _hmac.new(_deps.manager_secret(), msg, hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


# ── Private: cert build/sign (shared by heartbeat-ack issuer + admin-tab rotation) ──
def _build_and_sign_agent_cert(agent: dict) -> "dict | None":
    """Run SAN derivation + sign + return the {cert_pem, key_pem, ca_pem, expires_at}
    block. Returns None when the PKI module is unavailable.

    Both _maybe_issue_tls_bundle (heartbeat-driven) and _agents_issue_cert
    (admin-tab manual rotation) used to open-code this with subtly different
    SAN-derivation rules — same bug fix landed twice and had to stay in lockstep.
    Centralizing the logic here keeps the SAN coverage rule (bind_url's host IP
    + registered_from, whichever is an IP) in one place.

    The audit-trail write (last_cert_issued_at + clearing force_cert_reissue)
    stays at the callers because admin rotation does NOT clear force_cert_reissue
    — only the heartbeat path does that, since the flag is heartbeat-consumed.
    """
    ca_cert, ca_key, pki = _deps.pki_ensure_ca()
    if pki is None:
        log.warning("pki_ensure_ca returned None module; cannot sign cert for agent %s",
                    agent.get("agent_id"))
        return None
    # SAN must cover EVERY IP the manager might dial back on. For local
    # installs the agent advertises bind_url=<LAN-IP> but its TCP
    # connection arrives as registered_from=127.0.0.1, and
    # agent_callback_urls's _safe_bind_host rejects the LAN-IP bind_url
    # then dials 127.0.0.1 instead — so a cert covering only one of those
    # mismatches at verify time. Put both in SAN.
    from urllib.parse import urlparse as _urlparse
    import ipaddress as _ipaddr
    bind = agent.get("bind_url") or ""
    ip_san = ""
    try:
        host = _urlparse(bind).hostname
        if host:
            _ipaddr.ip_address(host)  # raises if not an IP
            ip_san = host
    except (ValueError, TypeError):
        pass
    if not ip_san:
        ip_san = agent.get("registered_from") or ""
    extra_ips: "list[str]" = []
    rfrom = (agent.get("registered_from") or "").strip()
    if rfrom and rfrom != ip_san:
        try:
            _ipaddr.ip_address(rfrom)
            extra_ips.append(rfrom)
        except (ValueError, TypeError):
            pass
    cert_pem, key_pem = pki.sign_agent_cert(
        ca_cert, ca_key,
        agent_id=agent["agent_id"],
        hostname=agent.get("hostname") or agent["agent_id"],
        ip_san=ip_san,
        extra_ip_sans=extra_ips,
    )
    ca_pem = pki.ca_bundle_pem(_deps.data_dir)
    from cryptography import x509 as _x509
    parsed = _x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    return {
        "cert_pem": cert_pem,
        "key_pem":  key_pem,
        "ca_pem":   ca_pem,
        "expires_at": parsed.not_valid_after_utc.isoformat(),
    }


# ── Private: TLS bundle issuance ─────────────────────────────────────
def _maybe_issue_tls_bundle(agent: dict, body: dict) -> "dict | None":
    """Return a {cert_pem, key_pem, ca_pem, expires_at, reason} block to
    send back in the heartbeat ack when the agent has no cert yet OR is
    nearing expiry OR was issued before our last PKI fix. Returns None
    when the agent's current cert is still valid and well within the
    rotation window AND was issued by a current-format signer.

    Bearer auth (already enforced at the heartbeat handler) guarantees
    we're issuing only for the agent that owns the token — the signed
    cert is bound to that agent_id/hostname/IP via subject + SAN.
    """
    try:
        has_cert = bool(body.get("has_tls_cert"))
        expires_at = body.get("tls_expires_at")  # ISO-8601 from agent
        needs_new = not has_cert
        reason_hint = "first-issue"
        rotation_days_left = _deps.settings.manager.security.tls_rotation_warn_days
        if not needs_new and expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                remaining = (exp - datetime.now(timezone.utc)).total_seconds() / 86400.0
                if remaining < rotation_days_left:
                    needs_new = True
                    reason_hint = "rotation"
            except Exception:
                # Malformed timestamp — safer to reissue than ignore.
                needs_new = True
                reason_hint = "rotation"
        # Force re-issuance for certs that were issued before the last
        # PKI fix landed (their format is incompatible with modern
        # OpenSSL chain verification — see _pki.AKI_FIX_TS).
        if not needs_new:
            last_issued = agent.get("last_cert_issued_at")
            if last_issued:
                with best_effort("tls bundle: check cert issue age", log=log):
                    li = datetime.fromisoformat(last_issued.replace("Z", "+00:00"))
                    # Read the constant from the loaded _pki module — it lives
                    # there because three different cert issuers consult it.
                    _, _, _pki_for_aki = _deps.pki_ensure_ca()
                    if _pki_for_aki is not None and li < _pki_for_aki.AKI_FIX_TS:
                        needs_new = True
                        reason_hint = "format-upgrade"
        # Explicit admin-requested rotation. The /api/admin/push-ca-to-agents
        # endpoint sets this flag on every approved agent at once — useful
        # after a manager CA rotation, since the agent's stored ca_pem is
        # otherwise updated only at first issue / format-upgrade and there's
        # no other trigger that fires when the CA changes but the leaf cert
        # is still valid. Cleared below after the fresh bundle is sent so
        # subsequent heartbeats don't keep reissuing.
        if not needs_new and agent.get("force_cert_reissue"):
            needs_new = True
            reason_hint = "admin-rotation"
        # Belt-and-suspenders: if the agent's current bind_url is
        # https:// but the IP in it isn't the one we'd put in a new
        # cert's SAN, the existing cert almost certainly has a stale
        # SAN. This catches the case where _pki.AKI_FIX_TS isn't tight
        # enough (e.g., the operator restarted between two commits and
        # got an intermediate-version cert).
        if not needs_new:
            from urllib.parse import urlparse as _urlparse
            import ipaddress as _ipaddr
            bind = agent.get("bind_url") or ""
            san_ips = body.get("tls_san_ips") or []
            if isinstance(san_ips, list) and san_ips:
                # SAN must cover bind_url's host so direct dials verify, AND
                # registered_from so the agent_callback_urls loopback-fallback
                # path verifies. Either missing → reissue.
                try:
                    bind_host = _urlparse(bind).hostname
                    if bind_host and bind.startswith("https://"):
                        _ipaddr.ip_address(bind_host)
                        if bind_host not in san_ips:
                            needs_new = True
                            reason_hint = "san-mismatch"
                            log.info("agent %s cert SAN=%s lacks bind_url host %s — reissuing",
                                     agent.get("hostname"), san_ips, bind_host)
                except (ValueError, TypeError):
                    pass
                if not needs_new:
                    rfrom = (agent.get("registered_from") or "").strip()
                    try:
                        _ipaddr.ip_address(rfrom)
                        if rfrom and rfrom not in san_ips:
                            needs_new = True
                            reason_hint = "san-mismatch"
                            log.info("agent %s cert SAN=%s lacks registered_from %s — reissuing",
                                     agent.get("hostname"), san_ips, rfrom)
                    except (ValueError, TypeError):
                        pass
        if not needs_new:
            return None

        bundle = _build_and_sign_agent_cert(agent)
        if bundle is None:
            return None
        # `reason_hint` set above based on which gate triggered: first
        # issue (no cert), rotation (near expiry / malformed timestamp),
        # or format-upgrade (issued before _pki.AKI_FIX_TS).
        log.info("heartbeat-ack TLS %s for agent %s (host=%s, expires=%s)",
                 reason_hint, agent["agent_id"], agent.get("hostname"),
                 bundle["expires_at"])
        with _agents_lock:
            data = load_agents()
            a = (data.get("agents") or {}).get(agent["agent_id"])
            if a is not None:
                a["last_cert_issued_at"] = datetime.now(timezone.utc).isoformat()
                # Clear the admin-rotation flag once the fresh bundle has
                # actually been built; otherwise every subsequent heartbeat
                # would keep issuing.
                a.pop("force_cert_reissue", None)
                save_agents(data)
        return {**bundle, "reason": reason_hint}
    except Exception as e:
        log.exception("TLS bundle issuance failed for agent %s: %s",
                      agent.get("agent_id"), e)
        return None


# Backwards-compatible alias for other modules that may reference the
# legacy underscore-prefixed name.
maybe_issue_tls_bundle = _maybe_issue_tls_bundle


# ── Private: liveness watcher thread ─────────────────────────────────
def _agent_liveness_watcher() -> None:
    """Background thread: log transitions to/from down/stale, every 60s.

    Only logs when an approved agent's liveness *changes*, so a long-down
    agent doesn't spam the log every minute. State is kept in-memory.
    """
    last_seen_state: "dict[str, str]" = {}
    while True:
        try:
            time.sleep(_deps.settings.manager.agents.liveness_watch_interval_s)
            data = load_agents()
            for agent_id, agent in (data.get("agents") or {}).items():
                if agent.get("status") != "approved":
                    continue
                liveness = agent_liveness(agent)
                prev = last_seen_state.get(agent_id)
                if prev != liveness:
                    last_seen_state[agent_id] = liveness
                    if prev is None and liveness == "live":
                        # Initial sighting at startup — not noteworthy
                        continue
                    last_hb = agent.get("last_heartbeat") or "never"
                    msg = (
                        "agent liveness change: id=%s hostname=%s "
                        "%s -> %s (last_heartbeat=%s)"
                    ) % (agent_id, agent.get("hostname"),
                         prev or "<new>", liveness, last_hb)
                    if liveness == "down":
                        log.warning(msg)
                    elif liveness == "stale":
                        log.warning(msg)
                    else:
                        log.info(msg)
        except Exception:
            log.exception("agent liveness watcher tick failed")


# ── Private route handlers ───────────────────────────────────────────
def _agents_register():
    """No-auth endpoint: agent posts its identity, gets back agent_id + status."""
    try:
        body = flask_request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "invalid JSON"}), 400

    hostname = (body.get("hostname") or "").strip()
    os_ = (body.get("os") or "").strip()
    if not hostname or os_ not in ("linux", "macos"):
        return jsonify({"ok": False, "error": "hostname + valid os required"}), 400

    bind_url = body.get("bind_url", "")
    fingerprint = body.get("fingerprint", "")

    with _agents_lock:
        data = load_agents()
        # Re-registration: same (hostname, os) replaces but keeps prior status/token if approved.
        existing = None
        for aid, agent in data["agents"].items():
            if agent.get("hostname") == hostname and agent.get("os") == os_:
                existing = (aid, agent)
                break
        if existing:
            agent_id, agent = existing
            # Authenticate the re-registration before mutating any record
            # fields or returning the bearer token. Accept any of:
            #   (a) valid prior bearer token in Authorization,
            #   (b) source IP matches the original registered_from (TOFU),
            #   (c) supplied fingerprint matches the stored fingerprint.
            # Otherwise treat as an unauthenticated identity claim: do not
            # overwrite trust-bearing fields and do not return the token.
            auth_header = flask_request.headers.get("Authorization", "")
            supplied_tok = ""
            if auth_header.startswith("Bearer "):
                supplied_tok = auth_header[len("Bearer "):].strip()
            stored_tok = agent.get("token") or ""
            stored_fp = agent.get("fingerprint") or ""
            stored_from = agent.get("registered_from") or ""
            remote = flask_request.remote_addr or ""
            tok_ok = bool(stored_tok) and bool(supplied_tok) and _hmac.compare_digest(supplied_tok, stored_tok)
            ip_ok = bool(stored_from) and remote == stored_from
            fp_ok = bool(stored_fp) and bool(fingerprint) and _hmac.compare_digest(fingerprint, stored_fp)
            authenticated = tok_ok or ip_ok or fp_ok

            if authenticated:
                agent["bind_url"] = bind_url
                agent["fingerprint"] = fingerprint
                agent["version"] = body.get("version", agent.get("version"))
                agent["description"] = body.get("description", agent.get("description"))
                agent["capabilities"] = body.get("capabilities", agent.get("capabilities"))
                agent["agent_user"] = body.get("agent_user", agent.get("agent_user"))
                agent["role"] = body.get("role", agent.get("role"))
                agent["image_gen_port"] = body.get("image_gen_port", agent.get("image_gen_port"))
                # Only refresh registered_from when the source IP itself
                # already matched (or token/fp re-auth came from the same
                # subnet). This keeps a host that legitimately changed
                # IPs working when its prior token is presented, while
                # preventing an unauthenticated attacker from rewriting
                # it via hostname guessing.
                agent["registered_from"] = remote or stored_from
                agent["last_register"] = datetime.now(timezone.utc).isoformat()
                data["agents"][agent_id] = agent
                save_agents(data)
                log.info("agent re-registered: id=%s hostname=%s status=%s auth=%s",
                         agent_id, hostname, agent.get("status"),
                         "tok" if tok_ok else ("ip" if ip_ok else "fp"))
                return jsonify({
                    "ok": True,
                    "agent_id": agent_id,
                    "status": agent.get("status", "pending"),
                    "approval_url": f"/?tab=admin#agent={agent_id}",
                    **({"token": agent["token"]} if agent.get("status") == "approved" and agent.get("token") else {}),
                })

            # Unauthenticated re-registration claim — do not mutate stored
            # bind_url/fingerprint/registered_from, do not return token.
            # The legitimate agent will recover via /api/agents/<id>/status
            # (which matches on registered_from) or via admin re-approval.
            log.warning("agent re-registration rejected: id=%s hostname=%s remote=%s (no matching token/ip/fp)",
                        agent_id, hostname, remote)
            return jsonify({
                "ok": False,
                "error": "re-registration requires matching token, source IP, or fingerprint",
                "agent_id": agent_id,
                "status": agent.get("status", "pending"),
            }), 403

        agent_id = str(_uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        auto_approve = is_local_bind_url(bind_url)
        token = _secrets.token_hex(32) if auto_approve else None
        agent = {
            "agent_id": agent_id,
            "hostname": hostname,
            "os": os_,
            "role": body.get("role", "auto"),
            "bind_url": bind_url,
            "fingerprint": fingerprint,
            "description": body.get("description", ""),
            "capabilities": body.get("capabilities", {}),
            "agent_user": body.get("agent_user", ""),
            "version": body.get("version", ""),
            "image_gen_port": body.get("image_gen_port"),
            "status": "approved" if auto_approve else "pending",
            "token": token,
            "first_seen": now,
            "last_register": now,
            "last_heartbeat": None,
            "approved_at": now if auto_approve else None,
            "approved_by": "auto-local" if auto_approve else None,
            "registered_from": flask_request.remote_addr,
        }
        data["agents"][agent_id] = agent
        save_agents(data)

    log.info("agent registered: id=%s hostname=%s os=%s status=%s",
             agent_id, hostname, os_, agent["status"])
    out = {
        "ok": True,
        "agent_id": agent_id,
        "status": agent["status"],
        "approval_url": f"/?tab=admin#agent={agent_id}",
    }
    if auto_approve:
        out["token"] = token
    return jsonify(out)


def _agents_get_status(agent_id: str):
    """No-auth endpoint: agents poll this until approved. Returns token only
    if the source IP matches the original registration."""
    data = load_agents()
    agent = data.get("agents", {}).get(agent_id)
    if not agent:
        return jsonify({"ok": False, "error": "unknown agent"}), 404
    out = {"ok": True, "status": agent.get("status")}
    if agent.get("status") == "approved" and agent.get("token"):
        if (flask_request.remote_addr or "") == (agent.get("registered_from") or ""):
            out["token"] = agent["token"]
    return jsonify(out)


def _agents_whoami():
    """Token-cache validation endpoint for the agent."""
    tok = bearer_from_request()
    agent = agent_by_token(tok or "")
    if not agent:
        return jsonify({"ok": False, "error": "invalid token or not approved"}), 401
    return jsonify({
        "ok": True,
        "agent_id": agent["agent_id"],
        "status": agent["status"],
        "hostname": agent["hostname"],
    })


def _agent_tarball():
    """Stream a freshly-built tarball of <repo>/agent/ to an approved agent.
    Agent bearer auth; the agent's install.sh hits this on --update so the
    manager (not GitHub) is the source of truth. Private repo stays private,
    no GitHub credentials needed on agent hosts."""
    tok = bearer_from_request()
    agent = agent_by_token(tok or "")
    if not agent:
        return jsonify({"ok": False, "error": "invalid token or not approved"}), 401

    import pathlib
    agent_dir = pathlib.Path(__file__).resolve().parents[2] / "agent"
    if not agent_dir.is_dir():
        return jsonify({"ok": False, "error": f"agent/ not found at {agent_dir}"}), 500

    def gen():
        # Run from the parent so paths inside the tarball start with "agent/".
        proc = subprocess.Popen(
            ["tar", "-czf", "-",
             "--exclude=__pycache__", "--exclude=*.pyc",
             "agent"],
            cwd=str(agent_dir.parent),
            stdout=subprocess.PIPE,
        )
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            with best_effort("agent tar: close proc stdout", log=log):
                proc.stdout.close()
            proc.wait()

    return Response(gen(), mimetype="application/gzip", headers={
        "Content-Disposition": "attachment; filename=agent.tar.gz",
        "Cache-Control": "no-store",
    })


def _agents_heartbeat():
    """Bearer-auth endpoint: agent posts liveness + state."""
    tok = bearer_from_request()
    agent = agent_by_token(tok or "")
    if not agent:
        # Reject distinctly so the agent can clear its approved flag.
        return jsonify({"ok": False, "error": "invalid token or not approved"}), 401
    try:
        body = flask_request.get_json(force=True) or {}
    except Exception:
        body = {}
    global _hb_last_flush_at
    with _agents_lock:
        data = load_agents()
        live = data["agents"].get(agent["agent_id"])
        if live:
            prev_version = live.get("version")
            live["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            live["last_heartbeat_data"] = {
                "collection_enabled": body.get("collection_enabled"),
                "llama_state": body.get("llama_state"),
                "samples_posted": body.get("samples_posted"),
                "tls_expires_at": body.get("tls_expires_at"),
                # True when the agent reports its MANAGER_URL is https — pairs
                # with bind_url scheme in /api/admin/system-health to drive the
                # admin tab's bidirectional-TLS indicator.
                "control_channel_tls": bool(body.get("control_channel_tls")),
            }
            # Refresh the version from each heartbeat so the admin tab
            # reflects post-self-update reality without waiting for the
            # next full re-registration.
            v = body.get("version")
            version_changed = bool(v and isinstance(v, str) and v != prev_version)
            if version_changed:
                live["version"] = v
            data["agents"][agent["agent_id"]] = live
            # load_agents returns the cached dict, so the mutations above are
            # already visible to in-process liveness reads without a write.
            # Flush to disk only on a material change or once per interval.
            now_mono = time.monotonic()
            if version_changed or (now_mono - _hb_last_flush_at) >= _HB_FLUSH_MIN_INTERVAL_S:
                save_agents(data)
                _hb_last_flush_at = now_mono
        auth_disabled = bool(data.get("global", {}).get("auth_disabled", False))
        primary_llama_id = (data.get("global") or {}).get("primary_llama_id")
        llama_state = body.get("llama_state")
        if agent["agent_id"] == primary_llama_id and llama_state in ("awake", "sleeping"):
            _deps.set_llama_awake(llama_state == "awake")

    desc = agent.get("description") or ""
    log.info(
        "heartbeat agent:%s host=%s%s ip=%s collection=%s llama_state=%s samples=%s",
        agent["agent_id"][:8],
        agent.get("hostname"),
        f" desc={desc!r}" if desc else "",
        flask_request.remote_addr,
        body.get("collection_enabled"),
        body.get("llama_state"),
        body.get("samples_posted"),
    )
    tls_block = _maybe_issue_tls_bundle(agent, body)

    # Rewrite a loopback-configured AE URL to whatever hostname the
    # agent used to reach the manager — sending "localhost:8081" to a
    # remote agent would point it at its own machine.
    ae_url_for_agent = _deps.rewrite_loopback_host(
        _deps.alarm_engine_url() or "",
        _deps.request_host_no_port(),
    )
    # When the alarm engine serves TLS, advertise it as https:// so the agent
    # upgrades its metric push (it verifies the AE cert against the same CA).
    if bool(_deps.settings.alarm_engine.tls_enabled) and ae_url_for_agent.startswith("http://"):
        ae_url_for_agent = "https://" + ae_url_for_agent[len("http://"):]

    # Advertise the manager's HTTPS URL so an approved agent (which holds our
    # CA) can auto-upgrade its control channel from http://:5000 to TLS. Built
    # from the host the agent actually used + the configured tls_port; blank
    # when TLS is disabled. The agent probes it before switching, so a wrong
    # value can't strand the heartbeat.
    _mgr_tls_port = int(_deps.settings.manager.tls_port or 0)
    manager_https_url = (
        f"https://{_deps.request_host_no_port()}:{_mgr_tls_port}" if _mgr_tls_port > 0 else ""
    )

    return jsonify({
        "ok": True,
        "auth_disabled": auth_disabled,
        "manager_secret": _deps.manager_secret().hex(),
        # Agents consume this on first heartbeat after process start and
        # ignore subsequent values — restart picks up later changes.
        "alarm_engine_url": ae_url_for_agent,
        "manager_https_url": manager_https_url,
        # Shared ingest token for the alarm engine's metric POST routes. Blank
        # when ingest auth is off; agents attach it as the bearer on their
        # BufferedMetricClient pushes. Read live so a rotation propagates on the
        # next heartbeat (≤60s) without a manager restart. ingest_token_for_agent
        # normalizes the "REPLACE_ME" placeholder to blank — same set the AE's
        # require_ingest_token treats as "ingest open" — so agents never POST
        # a meaningless bearer when the operator hasn't filled it in.
        "ingest_token": ingest_token_for_agent(),
        # The list of providers this agent is the default for — agents read
        # this to decide whether to publish state-change broadcasts as the
        # provider's "default" agent. PR2: agents fall back to is_primary_llama
        # when default_for is missing (old manager). is_primary_llama is kept
        # forever as a one-bit back-compat signal — see plan §Heartbeat ack.
        "default_for": _default_for_agent(data, agent["agent_id"]),
        "is_primary_llama": bool(
            (data.get("global", {}) or {}).get("default_llama_id") == agent["agent_id"]
            or (data.get("global", {}) or {}).get("primary_llama_id") == agent["agent_id"]
        ),
        "tls": tls_block,
    })


def _agents_list():
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    data = load_agents()
    latest = _deps.latest_agent_version()
    _deps.refresh_infra_versions()
    # Redact tokens — admin UI doesn't need them after approval flow.
    safe = []
    for agent in data.get("agents", {}).values():
        a = dict(agent)
        if a.get("token"):
            a["token"] = "<redacted>"
        a["liveness"] = agent_liveness(agent)
        # Convenience flag for the admin tab — true iff the agent's
        # reported version differs from the manager's local copy.
        cur = a.get("version")
        a["update_available"] = bool(latest and cur and cur != latest)
        a["colocated_infra"] = colocated_infra(agent)
        safe.append(a)
    return jsonify({
        "agents": safe,
        "global": data.get("global", {"auth_disabled": False}),
        "latest_agent_version": latest,
    })


def _agents_approve(agent_id: str):
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    with _agents_lock:
        data = load_agents()
        agent = data["agents"].get(agent_id)
        if not agent:
            return jsonify({"ok": False, "error": "unknown agent"}), 404
        if not agent.get("token"):
            agent["token"] = _secrets.token_hex(32)
        agent["status"] = "approved"
        agent["approved_at"] = datetime.now(timezone.utc).isoformat()
        agent["approved_by"] = flask_request.remote_addr
        data["agents"][agent_id] = agent
        # Auto-promote the first approved capability holder to primary
        # so a fresh split install actually shows data on the dashboard
        # without a follow-up "set primary" click. Skipped when an
        # existing primary or pool is already in place — the operator
        # gets to make the call once more than one capable agent shows up.
        glob = data.setdefault("global", {})
        caps = agent.get("capabilities") or {}
        auto_promoted = []
        if caps.get("llama") and not glob.get("default_llama_id") \
                and not glob.get("primary_llama_id") and not glob.get("llama_pool"):
            _set_provider_default(glob, "llama", agent_id)
            auto_promoted.append("llama")
        if caps.get("lms") and not glob.get("default_lms_id") \
                and not glob.get("primary_lms_id"):
            _set_provider_default(glob, "lms", agent_id)
            auto_promoted.append("lms")
        save_agents(data)
    log.info("agent approved by %s: id=%s hostname=%s%s",
             flask_request.remote_addr, agent_id, agent.get("hostname"),
             f"; auto-primary={'+'.join(auto_promoted)}" if auto_promoted else "")
    return jsonify({"ok": True, "status": "approved",
                    "auto_primary": auto_promoted})


def _agents_disable(agent_id: str):
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    with _agents_lock:
        data = load_agents()
        agent = data["agents"].get(agent_id)
        if not agent:
            return jsonify({"ok": False, "error": "unknown agent"}), 404
        agent["status"] = "disabled"
        data["agents"][agent_id] = agent
        save_agents(data)
    provider_state.STORE.evict(agent_id)
    log.warning("agent disabled by %s: id=%s", flask_request.remote_addr, agent_id)
    return jsonify({"ok": True, "status": "disabled"})


def _agents_delete(agent_id: str):
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    with _agents_lock:
        data = load_agents()
        if agent_id in data.get("agents", {}):
            del data["agents"][agent_id]
            save_agents(data)
    provider_state.STORE.evict(agent_id)
    log.warning("agent deleted by %s: id=%s", flask_request.remote_addr, agent_id)
    return jsonify({"ok": True})


def _agents_set_primary(agent_id: str):
    """Mark an approved agent as primary for `llama` or `lms`.
    Body: {"kind": "llama"|"lms", "set": true|false}"""
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    body = flask_request.get_json(force=True) or {}
    kind = body.get("kind")
    if kind not in ("llama", "lms"):
        return jsonify({"ok": False, "error": "kind must be 'llama' or 'lms'"}), 400
    set_flag = bool(body.get("set", True))
    with _agents_lock:
        data = load_agents()
        agent = data["agents"].get(agent_id)
        if not agent:
            return jsonify({"ok": False, "error": "unknown agent"}), 404
        if set_flag:
            if agent.get("status") != "approved":
                return jsonify({"ok": False, "error": "agent not approved"}), 400
            caps = agent.get("capabilities", {}) or {}
            if not caps.get(kind):
                return jsonify({"ok": False,
                                "error": f"agent does not advertise {kind} capability"}), 400
            _set_provider_default(data.setdefault("global", {}), kind, agent_id)
        else:
            _set_provider_default(data.setdefault("global", {}), kind, "")
        save_agents(data)
    log.info("primary_%s_id %s by %s: agent=%s hostname=%s",
             kind, "set" if set_flag else "cleared",
             flask_request.remote_addr, agent_id, agent.get("hostname"))
    return jsonify({
        "ok": True,
        "primary": data.get("global", {}).get(f"primary_{kind}_id") or None,
    })


def _agents_push_llama_state(agent_id: str):
    tok = bearer_from_request()
    agent = agent_by_token(tok or "")
    if not agent or agent["agent_id"] != agent_id:
        return jsonify({"ok": False, "error": "invalid token"}), 401
    body = flask_request.get_json(force=True) or {}
    state = (body.get("state") or "").lower()
    if state not in ("awake", "sleeping"):
        return jsonify({"ok": False, "error": "state must be 'awake' or 'sleeping'"}), 400

    data = load_agents()
    if (data.get("global") or {}).get("primary_llama_id") != agent_id:
        # Not the primary — return 200 so the agent doesn't retry, but
        # don't touch the interval. Pool-driven multi-host support
        # could lift this restriction later.
        return jsonify({"ok": True, "applied": False, "reason": "not primary llama"}), 200

    _deps.set_llama_awake(state == "awake")
    return jsonify({"ok": True, "applied": True, "state": state, "interval": _deps.get_interval()})


def _agents_set_llama_pool(agent_id: str):
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    body = flask_request.get_json(force=True) or {}
    in_pool = bool(body.get("in_pool", True))
    position = body.get("position")

    with _agents_lock:
        data = load_agents()
        agent = data["agents"].get(agent_id)
        if not agent:
            return jsonify({"ok": False, "error": "unknown agent"}), 404
        if in_pool:
            caps = agent.get("capabilities", {}) or {}
            if not caps.get("llama"):
                return jsonify({"ok": False,
                                "error": "agent does not advertise llama capability"}), 400
            if agent.get("status") != "approved":
                return jsonify({"ok": False, "error": "agent not approved"}), 400

        glob = data.setdefault("global", {})
        pool = list(glob.get("llama_pool") or [])
        # Remove if present, then re-add at target index — handles both
        # add (was absent) and re-order (was present) in one path.
        if agent_id in pool:
            pool.remove(agent_id)
        if in_pool:
            if isinstance(position, int) and 0 <= position <= len(pool):
                pool.insert(position, agent_id)
            else:
                pool.append(agent_id)
        glob["llama_pool"] = pool
        save_agents(data)

    log.info("llama_pool %s by %s: agent=%s host=%s; pool=%s",
             "add/move" if in_pool else "remove",
             flask_request.remote_addr, agent_id, agent.get("hostname"), pool)
    return jsonify({"ok": True, "llama_pool": pool})


def _agents_issue_cert(agent_id: str):
    deny = _deps.require_admin()
    if deny is not None:
        return deny

    data = load_agents()
    agent = (data.get("agents") or {}).get(agent_id)
    if not agent:
        return jsonify({"ok": False, "error": "unknown agent"}), 404
    if agent.get("status") != "approved":
        return jsonify({"ok": False, "error": "agent not approved"}), 400

    try:
        bundle = _build_and_sign_agent_cert(agent)
    except Exception as e:
        log.exception("cert issuance failed for agent %s", agent_id)
        return jsonify({"ok": False, "error": f"signing failed: {e}"}), 500
    if bundle is None:
        return jsonify({"ok": False, "error": "pki module unavailable"}), 500

    # Lightweight audit trail in the registry — record when we last
    # issued a cert for this agent so the admin tab can show staleness.
    # Unlike the heartbeat-driven issuer, this path deliberately does NOT
    # clear force_cert_reissue — that flag is heartbeat-consumed.
    with _agents_lock:
        data = load_agents()
        a = (data.get("agents") or {}).get(agent_id)
        if a is not None:
            a["last_cert_issued_at"] = datetime.now(timezone.utc).isoformat()
            save_agents(data)

    log.info("issued cert for agent %s (host=%s, expires=%s)",
             agent_id, agent.get("hostname"), bundle["expires_at"])
    return jsonify({
        "ok": True,
        "agent_id": agent_id,
        "cert_pem": bundle["cert_pem"],
        "key_pem":  bundle["key_pem"],
        "ca_pem":   bundle["ca_pem"],
        "expires_at": bundle["expires_at"],
    })


def _admin_push_ca_to_agents():
    """Force every approved agent to pull a fresh cert+key+CA bundle on its
    next heartbeat ack. Use after rotating the manager's internal CA — the
    agent's stored data/tls-ca.pem otherwise updates only at first issue,
    near-expiry rotation, or format-upgrade, none of which fire when the
    CA changes but the leaf cert is still valid. The flag is consumed by
    _maybe_issue_tls_bundle and cleared after the bundle is built; agents
    pick up the new bundle within one heartbeat (≤60s).

    No payload — admin auth alone authorizes the rotation. Returns the
    list of agents that were marked plus the manager's CA fingerprint so
    the operator can spot-check that everything pivots correctly afterward.
    """
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    with _agents_lock:
        data = load_agents()
        agents = (data.get("agents") or {})
        marked: "list[dict]" = []
        for aid, a in agents.items():
            if a.get("status") != "approved":
                continue
            a["force_cert_reissue"] = True
            marked.append({"agent_id": aid,
                           "hostname": a.get("hostname") or "",
                           "bind_url": a.get("bind_url") or ""})
        if marked:
            save_agents(data)
    # CA fingerprint for the operator to verify against agent-side
    # tls-ca.pem after the heartbeat round-trip lands.
    ca_fp = ""
    with best_effort("push-ca: compute CA fingerprint", log=log):
        ca_path = (_deps.data_dir or Path(".")) / "internal-ca.crt"
        if ca_path.is_file():
            ca_fp = hashlib.sha256(ca_path.read_bytes()).hexdigest()
    log.warning("admin push-ca-to-agents by %s: marked %d approved agent(s) "
                "for cert-reissue on next heartbeat (CA fp=%s)",
                flask_request.remote_addr, len(marked), ca_fp[:16] or "?")
    return jsonify({
        "ok": True,
        "marked_count": len(marked),
        "marked": marked,
        "ca_fingerprint_sha256": ca_fp,
        "note": "Agents will receive the fresh bundle on their next "
                "heartbeat (≤60s). Watch /api/admin/system-health or the "
                "Admin → Agents page; the ↔ TLS badge confirms each agent "
                "has the new CA AND has re-probed the manager's HTTPS.",
    })


def _agents_stream_token(agent_id: str):
    """Issue a short-lived HMAC stream token for direct browser→agent SSE."""
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    body = flask_request.get_json(force=True) or {}
    path = body.get("path") or ""
    if not path.startswith("/"):
        return jsonify({"ok": False, "error": "path must start with /"}), 400
    ttl = int(body.get("ttl", 300))
    ttl = max(30, min(900, ttl))
    data = load_agents()
    agent = data["agents"].get(agent_id)
    if not agent or agent.get("status") != "approved":
        return jsonify({"ok": False, "error": "unknown or unapproved agent"}), 404
    token = issue_stream_token(agent_id, path, ttl)
    return jsonify({
        "ok": True,
        "url": f"{browser_reachable_bind_url(agent)}{path}?token={token}",
        "expires_in": ttl,
    })


def _agents_collection(agent_id: str):
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    body = flask_request.get_json(force=True) or {}
    enabled = bool(body.get("enabled", True))
    data = load_agents()
    agent = data["agents"].get(agent_id)
    if not agent or not agent.get("bind_url") or not agent.get("token"):
        return jsonify({"ok": False, "error": "unknown agent or missing token"}), 404
    r, tried, err = agent_request(
        "POST", agent, "/agent/collection",
        json={"enabled": enabled},
        headers={"Authorization": f"Bearer {agent['token']}"},
        timeout=5,
    )
    if r is None:
        return jsonify({"ok": False, "error": err, "tried": tried}), 502
    # Optimistically reflect the new state in the cached heartbeat data so
    # the admin UI flips the pause/resume button immediately. Without this
    # the button doesn't toggle until the next heartbeat (HEARTBEAT_INTERVAL_S,
    # currently 60s), leaving the user looking at a "Pause" button that
    # they just clicked.
    if r.ok:
        with _agents_lock:
            data2 = load_agents()
            agent2 = data2["agents"].get(agent_id)
            if agent2 is not None:
                hb = dict(agent2.get("last_heartbeat_data") or {})
                hb["collection_enabled"] = enabled
                agent2["last_heartbeat_data"] = hb
                save_agents(data2)
    return jsonify({"ok": r.ok, "status_code": r.status_code, "tried": tried,
                    "data": r.json() if r.ok else r.text[:500]})


def _agents_global():
    deny = _deps.require_admin()
    if deny is not None:
        return deny
    body = flask_request.get_json(force=True) or {}
    with _agents_lock:
        data = load_agents()
        data.setdefault("global", {})
        if "auth_disabled" in body:
            data["global"]["auth_disabled"] = bool(body["auth_disabled"])
        save_agents(data)
    log.info("global agent settings updated by %s: %s",
             flask_request.remote_addr, data.get("global"))
    return jsonify({"ok": True, "global": data.get("global", {})})


# ── Public: set_deps + register_routes ───────────────────────────────
def set_deps(ctx, *,
             request_host_no_port: Callable[[], str],
             rewrite_loopback_host: Callable[[str, str], str],
             set_llama_awake: Callable[[bool], None],
             get_interval: Callable[[], int],
             latest_agent_version: Callable[[], Any],
             refresh_infra_versions: Callable[[], None],
             infra_version_get: Callable[[str], Any],
             hostname: str,
             loopback_hosts: "frozenset[str] | set[str]",
             pki_ensure_ca: Callable[[], Any]) -> None:
    """Populate cross-module dependencies. Call before register_routes.

    Shared deps come from `ctx` (app_context.Context); the explicit kwargs
    are the ones only the registry reads — heartbeat-ack assembly helpers,
    LLaMA wake/idle controls, the infra-version cache, and the PKI loader.
    """
    global AGENTS_FILE
    AGENTS_FILE = Path(ctx.data_dir) / "agents.json"
    # Tighten file mode on startup. Previously _save_agents never chmod'd, so
    # historical installs likely landed at umask-default 0644 — the chmod here
    # one-shot upgrades them at the next manager restart without waiting for
    # the next write to fire (which only happens on heartbeat / admin actions).
    if AGENTS_FILE.is_file():
        try:
            import os as _os
            _os.chmod(AGENTS_FILE, 0o600)
        except OSError as e:
            log.warning("could not chmod %s to 0o600: %s", AGENTS_FILE, e)
    _deps.settings = ctx.settings
    _deps.data_dir = Path(ctx.data_dir)
    _deps.version = ctx.version
    _deps.require_admin = ctx.require_admin
    _deps.admin_ip_allowed = ctx.admin_ip_allowed
    _deps.agent_admin_allow = ctx.agent_admin_allow
    _deps.alarm_engine_url = ctx.alarm_engine_url
    _deps.manager_secret = ctx.manager_secret
    _deps.request_host_no_port = request_host_no_port
    _deps.rewrite_loopback_host = rewrite_loopback_host
    _deps.set_llama_awake = set_llama_awake
    _deps.get_interval = get_interval
    _deps.latest_agent_version = latest_agent_version
    _deps.refresh_infra_versions = refresh_infra_versions
    _deps.infra_version_get = infra_version_get
    _deps.hostname = hostname
    _deps.loopback_hosts = frozenset(loopback_hosts)
    _deps.pki_ensure_ca = pki_ensure_ca


def register_routes(app) -> None:
    """Wire all agent-registry routes into `app` and start the liveness watcher.

    Call once at startup, AFTER set_deps(). Routes registered:
      POST   /api/agents/register
      GET    /api/agents/<id>/status
      GET    /api/agents/whoami
      GET    /api/agent-tarball
      POST   /api/agents/heartbeat
      GET    /api/agents
      POST   /api/agents/<id>/approve
      POST   /api/agents/<id>/disable
      DELETE /api/agents/<id>
      POST   /api/agents/<id>/role-primary
      POST   /api/agents/<id>/llama-state
      POST   /api/agents/<id>/llama-pool
      POST   /api/agents/<id>/cert-bundle
      POST   /api/admin/push-ca-to-agents
      POST   /api/agents/<id>/stream-token
      POST   /api/agents/<id>/collection
      POST   /api/agents/global
    """
    app.add_url_rule("/api/agents/register", endpoint="agents_register",
                     view_func=_agents_register, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/status", endpoint="agents_get_status",
                     view_func=_agents_get_status, methods=["GET"])
    app.add_url_rule("/api/agents/whoami", endpoint="agents_whoami",
                     view_func=_agents_whoami, methods=["GET"])
    app.add_url_rule("/api/agent-tarball", endpoint="agent_tarball",
                     view_func=_agent_tarball, methods=["GET"])
    app.add_url_rule("/api/agents/heartbeat", endpoint="agents_heartbeat",
                     view_func=_agents_heartbeat, methods=["POST"])
    app.add_url_rule("/api/agents", endpoint="agents_list",
                     view_func=_agents_list, methods=["GET"])
    app.add_url_rule("/api/agents/<agent_id>/approve", endpoint="agents_approve",
                     view_func=_agents_approve, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/disable", endpoint="agents_disable",
                     view_func=_agents_disable, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>", endpoint="agents_delete",
                     view_func=_agents_delete, methods=["DELETE"])
    app.add_url_rule("/api/agents/<agent_id>/role-primary", endpoint="agents_set_primary",
                     view_func=_agents_set_primary, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/llama-state", endpoint="agents_push_llama_state",
                     view_func=_agents_push_llama_state, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/llama-pool", endpoint="agents_set_llama_pool",
                     view_func=_agents_set_llama_pool, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/cert-bundle", endpoint="agents_issue_cert",
                     view_func=_agents_issue_cert, methods=["POST"])
    app.add_url_rule("/api/admin/push-ca-to-agents", endpoint="admin_push_ca_to_agents",
                     view_func=_admin_push_ca_to_agents, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/stream-token", endpoint="agents_stream_token",
                     view_func=_agents_stream_token, methods=["POST"])
    app.add_url_rule("/api/agents/<agent_id>/collection", endpoint="agents_collection",
                     view_func=_agents_collection, methods=["POST"])
    app.add_url_rule("/api/agents/global", endpoint="agents_global",
                     view_func=_agents_global, methods=["POST"])

    threading.Thread(target=_agent_liveness_watcher, daemon=True,
                     name="agent-liveness-watcher").start()
