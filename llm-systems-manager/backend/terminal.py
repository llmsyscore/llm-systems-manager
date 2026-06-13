"""Manager-side terminal proxy for the LLM Systems Manager.

The manager no longer hosts local PTY sessions itself — every terminal is
opened on an approved agent and this module simply relays the create + IO
+ control HTTP calls. The sid -> agent_id mapping is held in-process so
follow-up calls (output stream, input bytes, resize, close) route to the
same agent that minted the sid. Every routed response carries an
``X-Proxied-To: <agent_id_8>@<hostname>`` header for debuggability.

Routes registered (all under app, see register_routes):
  POST /api/terminal/create           — primary llama-agent PTY
  POST /api/lms/terminal/create       — primary lms-agent PTY (SSH on macOS)
  GET  /api/terminal/output/<sid>     — SSE stream of PTY bytes
  POST /api/terminal/input/<sid>      — raw bytes into the PTY
  POST /api/terminal/resize/<sid>     — winsize JSON
  POST /api/terminal/close/<sid>      — kill + drop owner mapping

Wired by main via ``terminal.register_routes(app, ctx)``; only ctx + app
are needed because every other dep is reached via the sibling
``agent_registry`` module (primary_agent / agent_request / etc.).
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from types import SimpleNamespace

import requests
from flask import current_app, jsonify, request as flask_request

import agent_registry  # type: ignore[import-not-found]  # sibling
import proxies  # type: ignore[import-not-found]  # sibling (thread_pumped helper)
import stream_pool  # type: ignore[import-not-found]  # sibling

log = logging.getLogger("llm-systems-manager.terminal")

# Note: this logger emits as "llm-systems-manager.terminal:<funcName>" rather
# than the pre-M3 "llm-systems-manager:<funcName>". Propagates to the parent
# handlers so nothing is dropped, but operator greps / journald filters keyed
# on the literal `llm-systems-manager:` prefix won't match these 6 routes.
# Same convention auth.py and agent_registry.py established in M1 / M2.

__all__ = ["register_routes"]


# sid -> agent_id, populated by _proxy_create on a successful upstream
# response and consulted by every sid-keyed route. Manager-local only —
# agents own the PTY lifecycle; we own the owner map.
#
# Bounded LRU: every create inserts, hard-crash / forced-tab-kill never
# closes so the entry would otherwise leak (~40 bytes per session). At
# the cap the oldest entry evicts — a subsequent sid-keyed call falls
# through to the 503 "terminals are proxy-only" stub, identical to the
# behaviour for any sid the manager doesn't recognise. Reads bump the
# entry to most-recently-used so an active terminal isn't evicted by a
# burst of new creates.
_TERM_OWNER_MAX = 256
_term_owner: "OrderedDict[str, str]" = OrderedDict()
_term_owner_lock = threading.Lock()

# Bounded read timeout for the proxied PTY output stream. A None read timeout
# pins a Cheroot worker thread forever if the agent goes silent; the agent
# pings every ~0.4s, so 60s never kills a live terminal but reaps a dead one.
_STREAM_READ_TIMEOUT_S = 60.0

_deps = SimpleNamespace()


def _pick_agent(default_kind: str) -> "tuple[dict | None, str | None]":
    """(agent, None) on success, (None, error_message) on failure. Honors the
    picker selection ``?agent=<agent_id>`` (``?host=`` kept as a back-compat
    alias); otherwise resolves the provider default for ``default_kind``
    ('llama' | 'lms') — the same default the picker's default chip uses."""
    sel = (flask_request.args.get("agent")
           or flask_request.args.get("host") or "").strip()
    if sel:
        data = agent_registry.load_agents()
        agent = (data.get("agents") or {}).get(sel)
        if not agent:
            return None, f"unknown agent <{sel}>"
        if agent.get("status") != "approved":
            return None, f"agent {sel} not approved"
        return agent, None
    did = agent_registry.default_agent_id_for(default_kind)
    agent = (agent_registry.resolve_agent_by_id(did) if did else None) \
        or agent_registry.primary_agent(default_kind)
    if not agent:
        return None, f"no {default_kind} agent available"
    return agent, None


def _proxy_create(default_kind: str, agent_path: str = "/terminal/create"):
    """Pick the target agent, POST a create, stash sid -> agent_id on
    success."""
    agent, err = _pick_agent(default_kind)
    if err:
        return jsonify({"ok": False, "error": err}), 503
    if not agent.get("token"):
        return jsonify({"ok": False, "error": "agent has no token"}), 503

    r, tried, last_err = agent_registry.agent_request(
        "POST", agent, agent_path,
        headers={"Authorization": f"Bearer {agent['token']}"},
        timeout=15,
    )
    if r is None:
        log.warning("terminal-create proxy → %s failed: %s tried=%s",
                    agent.get("hostname"), last_err, tried)
        return jsonify({"ok": False, "error": last_err, "tried": tried}), 502
    try:
        body = r.json()
    except Exception:
        body = {"ok": False, "error": "agent did not return JSON",
                "raw": r.text[:200]}
    if body.get("ok") and body.get("sid"):
        with _term_owner_lock:
            _term_owner[body["sid"]] = agent["agent_id"]
            _term_owner.move_to_end(body["sid"])
            while len(_term_owner) > _TERM_OWNER_MAX:
                _term_owner.popitem(last=False)
        log.info("terminal-create proxy → agent:%s host=%s sid=%s",
                 agent["agent_id"][:8], agent.get("hostname"), body["sid"])
    resp = current_app.response_class(json.dumps(body),
                                      status=r.status_code,
                                      mimetype="application/json")
    resp.headers["X-Proxied-To"] = f"{agent['agent_id'][:8]}@{agent.get('hostname','?')}"
    return resp


def _proxy_sid(method: str, sid: str, agent_path: str,
               *, stream: bool = False, **kwargs):
    """Proxy a sid-keyed call to whichever agent minted the sid. Returns
    None when we have no owner record for sid (stale / never created
    through us — caller falls through to a 404)."""
    with _term_owner_lock:
        agent_id = _term_owner.get(sid)
        if agent_id is not None:
            _term_owner.move_to_end(sid)
    if not agent_id:
        return None
    data = agent_registry.load_agents()
    agent = (data.get("agents") or {}).get(agent_id)
    if not agent or not agent.get("token"):
        return jsonify({"ok": False, "error": "owning agent gone or unapproved"}), 502

    if stream:
        # SSE — open upstream, pipe bytes through. Same pattern as
        # _proxy_stream_to_primary but agent is picked by sid lookup.
        urls = agent_registry.agent_callback_urls(agent)
        # Cap concurrent streams so a held terminal SSE can't starve the pool;
        # slot freed via call_on_close on success, else the outer finally.
        if not stream_pool.POOL.try_acquire():
            return jsonify({"ok": False,
                            "error": "manager at stream capacity; retry shortly"}), 503
        slot_handed = False
        try:
            last_err = None
            for base in urls:
                full = f"{base}{agent_path}"
                upstream = None
                handed_off = False
                try:
                    upstream = requests.get(
                        full,
                        headers={"Authorization": f"Bearer {agent['token']}"},
                        stream=True, timeout=(5, _STREAM_READ_TIMEOUT_S),
                        **agent_registry.agent_tls_kwargs(full),
                    )

                    log.info("proxy SSE %s → agent:%s host=%s",
                             agent_path, agent["agent_id"][:8], agent.get("hostname"))
                    # Longer lifetime cap than log streams: terminal sends data
                    # every ~0.4s so a full-close is detected in <1s; the cap only
                    # backstops a half-closed/ACKing peer, and sessions are few.
                    response = current_app.response_class(
                        proxies.thread_pumped(upstream, agent_path, max_lifetime_s=600.0),
                        mimetype=upstream.headers.get("Content-Type", "text/event-stream"),
                        headers={
                            "Cache-Control": "no-cache",
                            "X-Accel-Buffering": "no",
                            "X-Proxied-To": f"{agent['agent_id'][:8]}@{agent.get('hostname','?')}",
                        },
                        status=upstream.status_code,
                    )
                    response.call_on_close(stream_pool.POOL.release)
                    handed_off = True
                    slot_handed = True
                    return response
                except Exception as e:
                    last_err = f"{full}: {type(e).__name__}: {e}"
                    continue
                finally:
                    # Close the upstream socket if we never handed it to the
                    # response generator (e.g. response_class construction
                    # raised) — _gen's own finally only fires once iteration
                    # starts.
                    if upstream is not None and not handed_off:
                        upstream.close()
            return jsonify({"ok": False, "error": last_err or "no callback URL"}), 502
        finally:
            if not slot_handed:
                stream_pool.POOL.release()

    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("Authorization", f"Bearer {agent['token']}")
    r, tried, err = agent_registry.agent_request(method, agent, agent_path,
                                                 headers=headers, timeout=15, **kwargs)
    if r is None:
        return jsonify({"ok": False, "error": err, "tried": tried}), 502
    log.info("proxy %s %s → agent:%s host=%s rc=%s sid=%s",
             method, agent_path, agent["agent_id"][:8],
             agent.get("hostname"), r.status_code, sid)
    ctype = r.headers.get("Content-Type", "application/json")
    resp = current_app.response_class(r.content, status=r.status_code, mimetype=ctype)
    resp.headers["X-Proxied-To"] = f"{agent['agent_id'][:8]}@{agent.get('hostname','?')}"
    return resp


def register_routes(app, ctx) -> None:
    """Wire the 6 terminal routes into ``app``. Reads only ``ctx``; every
    per-request dep is reached via ``agent_registry`` directly.
    ``current_app`` is used inside handlers so we don't need to stash
    ``app`` on _deps."""
    _deps.ctx = ctx

    @app.route("/api/terminal/create", methods=["POST"])
    def terminal_create():
        return _proxy_create("llama", "/terminal/create")

    @app.route("/api/lms/terminal/create", methods=["POST"])
    def lms_terminal_create():
        """Open a PTY on the primary lms agent (SSH to llm-systems-lmstudio)."""
        return _proxy_create("lms", "/terminal/create")

    @app.route("/api/terminal/output/<sid>")
    def terminal_output_sse(sid):
        proxied = _proxy_sid("GET", sid, f"/terminal/output/{sid}", stream=True)
        if proxied is not None:
            return proxied
        return jsonify({"ok": False, "error": "unknown sid"}), 404

    @app.route("/api/terminal/input/<sid>", methods=["POST"])
    def terminal_input(sid):
        # Raw body bytes — input may not be JSON (keystrokes, escape seqs).
        proxied = _proxy_sid(
            "POST", sid, f"/terminal/input/{sid}",
            data=flask_request.get_data(),
            headers={"Content-Type": "application/octet-stream"},
        )
        if proxied is not None:
            return proxied
        return jsonify({"ok": False, "error": "unknown sid"}), 404

    @app.route("/api/terminal/resize/<sid>", methods=["POST"])
    def terminal_resize(sid):
        proxied = _proxy_sid(
            "POST", sid, f"/terminal/resize/{sid}",
            json=flask_request.get_json(force=True) or {},
        )
        if proxied is not None:
            return proxied
        return jsonify({"ok": False, "error": "unknown sid"}), 404

    @app.route("/api/terminal/close/<sid>", methods=["POST"])
    def terminal_close(sid):
        proxied = _proxy_sid("POST", sid, f"/terminal/close/{sid}")
        if proxied is not None:
            # On a transient 502 the PTY is still alive on the agent;
            # keep the mapping so a retry lands at the right host.
            if 200 <= proxied.status_code < 300:
                with _term_owner_lock:
                    _term_owner.pop(sid, None)
            return proxied
        return jsonify({"ok": False, "error": "unknown sid"}), 404
