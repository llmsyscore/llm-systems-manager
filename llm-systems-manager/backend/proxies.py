"""Reverse-proxy infrastructure for the LLM Systems Manager.

Owns every Flask-routed proxy the manager exposes:

  Primary-agent dispatchers (used by ~40 routes in main):
    proxy_to_primary(kind, method, path, ...)        — sync RPC
    proxy_stream_to_primary(kind, path, ...)         — SSE pipe

  Catch-all reverse proxies (7 routes registered here):
    /proxy/llmchat/<path>        → llama.cpp Chat UI
    /proxy/openclaw/<path>       → OpenClaw service (injects WS shim)
    /proxy/imggen/<path>         → stable-diffusion.cpp UI
    /sdcpp/<path>                → SPA-relative sd.cpp backend calls
    /api/alarm/<path>            → alarm engine (admin/* gated)
    /alarm/<path>                → AE SPA static (split-install falls back to proxy)
    /ws/alarm                    → 426 Upgrade-Required stub

  Public helpers reached cross-module:
    resolve_proxy_target(name)   — also used by main's /api/config probe
    ae_ws_url_for_browser()      — used by main's index() for window.__AE_WS_URL__

Stays in main: `_ae_session` (process-singleton, consumed by many non-proxy
sites too — proxies.py reads it via ctx.ae_session), `_AE_CA_PATH`, and the
standalone `_maybe_start_alarm_ws_proxy` daemon (it's a `websockets` server,
not a Flask route — Cheroot WSGI can't do the upgrade). `install_topology`
also stays; serve_alarm_frontend reaches it via the kwarg below.

Wired into the Flask app by main via register_routes(app, ctx, ...).
"""

from __future__ import annotations

import logging
import mimetypes as _mimetypes
import os
import queue as _queue_lib
import re
import socket
import threading as _threading
import time as _time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import urlparse, urlsplit

import requests
from flask import (Response, current_app, jsonify, request as flask_request,
                   send_from_directory)

import agent_registry  # type: ignore[import-not-found]  # sibling
import stream_pool  # type: ignore[import-not-found]  # sibling
from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling

# Shared with main; proxies.py and main both reach the same Settings singleton.
from config.unified_config import settings  # type: ignore[import-not-found]

log = logging.getLogger("llm-systems-manager.proxies")

__all__ = [
    "register_routes",
    "proxy_to_primary",
    "proxy_stream_to_primary",
    "resolve_proxy_target",
    "ae_ws_url_for_browser",
]


# ── Constants ─────────────────────────────────────────────────────────

# Hop-by-hop response headers (RFC 7230 §6.1) — a reverse proxy MUST NOT
# forward these to its client. Plus content-framing (waitress sets these
# itself based on the body iterator) and frame-busting (CSP/X-Frame-Options)
# which we strip so embedded-iframe proxies render. Centralized so all four
# `/proxy/*` helpers agree.
_PROXY_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
    "content-security-policy", "x-frame-options",
}


def _csp_header_pairs(content_type: str) -> list:
    """Manager-origin CSP header for an HTML response, else []. Replaces the
    stripped upstream CSP with [manager.security].proxy_html_csp (empty = off)."""
    csp = (getattr(settings.manager.security, "proxy_html_csp", "") or "").strip()
    if csp and "text/html" in (content_type or "").lower():
        return [("Content-Security-Policy", csp)]
    return []

# Full HTTP verb list applied to every catch-all proxy decorator so proxied
# apps see GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS.
_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

# Bounded read timeout for proxied SSE streams. A None read timeout pins a
# Cheroot worker thread forever when an agent goes silent (no data + no
# keepalive), leaking threads until the pool is exhausted. Agent keepalives
# are ≤30s (log 15s, download/build/bench/autotune 30s, terminal 0.4s), so 60s
# never kills a healthy stream but reaps a stalled one; the browser reconnects.
_STREAM_READ_TIMEOUT_S = 120.0

# A proxied SSE generator that blocks in iter_content can't notice a departed
# browser (it only writes when the agent sends data, and a half-closed/ACKing
# peer never makes that write raise). thread_pumped() writes on the MANAGER's
# own clock: a keepalive every _STREAM_KEEPALIVE_S (so a full-close is detected
# fast) and a hard _STREAM_MAX_LIFETIME_S cap (the reliable reaper for a
# half-closed/ACKing peer whose writes never raise — verified). Both
# configurable via [manager].
_STREAM_KEEPALIVE_S = float(getattr(settings.manager, "stream_keepalive_s", 8.0) or 8.0)
_STREAM_MAX_LIFETIME_S = float(getattr(settings.manager, "stream_max_lifetime_s", 120.0) or 120.0)


def _proxy_error(message: str, status: int, detail: object = None) -> Response:
    # text/plain + generic message: never reflect request method / exception
    # into the body (logged server-side instead).
    if detail is not None:
        log.warning("%s: %s", message, detail)
    return Response(message, status=status,
                    content_type="text/plain; charset=utf-8")


def thread_pumped(upstream, path, *, keepalive_s: float = _STREAM_KEEPALIVE_S,
                  max_lifetime_s: float = _STREAM_MAX_LIFETIME_S):
    """Relay a requests stream as an SSE generator that writes on the manager's
    own clock so a departed browser is reaped. A daemon thread drains
    iter_content into a bounded queue; the main generator yields a keepalive
    comment on idle and returns at the lifetime cap. Closes upstream on exit;
    the slot is freed by the caller's response.call_on_close."""
    outq: "_queue_lib.Queue" = _queue_lib.Queue(maxsize=64)
    sentinel = object()
    stop = _threading.Event()

    def _drain():
        try:
            for chunk in upstream.iter_content(chunk_size=None):
                if stop.is_set():
                    break
                if not chunk:
                    continue
                while not stop.is_set():
                    try:
                        outq.put(chunk, timeout=1.0)
                        break
                    except _queue_lib.Full:
                        continue
        except requests.exceptions.RequestException as e:
            log.info("proxy SSE %s upstream idle/closed: %s", path, type(e).__name__)
        except Exception as e:
            # `stop` is set before the generator closes upstream; an exception
            # after that is the benign teardown race (urllib3 reads a nulled fp).
            if stop.is_set():
                log.debug("proxy SSE %s drain ended on teardown: %s", path, type(e).__name__)
            else:
                log.info("proxy SSE %s drain error: %s", path, type(e).__name__)
        finally:
            with best_effort("proxy drain: enqueue sentinel", log=log):
                outq.put_nowait(sentinel)

    _threading.Thread(target=_drain, name="sse-drain", daemon=True).start()
    deadline = _time.monotonic() + max_lifetime_s
    try:
        while _time.monotonic() < deadline:
            try:
                item = outq.get(timeout=keepalive_s)
            except _queue_lib.Empty:
                yield b": ka\n\n"   # manager keepalive -> Cheroot writes -> detects a full-close
                continue
            if item is sentinel:
                return
            yield item
    finally:
        stop.set()
        with best_effort("proxy: close upstream", log=log):
            upstream.close()


# ── Dep namespace ────────────────────────────────────────────────────

# Populated by register_routes(). Holds the Context plus the few module-
# specific callables proxies.py needs (install_topology for the AE frontend
# fallback, request_host_no_port + rewrite_loopback_host for the browser-
# dialable WS URL builder).
_deps = SimpleNamespace()

# Populated by register_routes(). Path to the AE SPA static bundle.
_ALARM_FRONTEND_DIR: "str | None" = None


# ── Helpers (private) ────────────────────────────────────────────────

def _host_from_agent(agent: "dict | None") -> "str | None":
    if not agent:
        return None
    ip = agent.get("registered_from")
    if ip:
        return ip
    bu = agent.get("bind_url") or ""
    try:
        return urlparse(bu).hostname
    except ValueError:
        return None


def resolve_proxy_target(name: str) -> "str | None":
    """Return the live upstream URL for proxy <name>, or None if disabled.
    `name` is one of: llm_chat, openclaw, image_gen.

    TOML semantics:
      false     → disabled
      true      → enabled, same as "auto" (resolve from approved agent)
      "auto"    → discover from approved agent that advertises the capability
      "<url>"   → explicit upstream URL
    """
    val = getattr(settings.manager.proxies, name, False)
    if val is False:
        return None
    if val is True:
        val = "auto"
    if not isinstance(val, str):
        return None
    s = val.strip()
    sl = s.lower()
    if sl in ("", "false"):
        return None
    if sl in ("auto", "true"):
        if name == "llm_chat":
            host = _host_from_agent(agent_registry.primary_agent("llama"))
            return f"http://{host}:8080" if host else None
        if name in ("openclaw", "image_gen"):
            cap_name = name  # capability flag key matches the proxy name
            default_port = 18789 if name == "openclaw" else 1234
            data = agent_registry.load_agents()
            for a in (data.get("agents") or {}).values():
                if a.get("status") != "approved":
                    continue
                if not (a.get("capabilities") or {}).get(cap_name):
                    continue
                host = _host_from_agent(a)
                if not host:
                    continue
                # Image-gen lets the agent advertise a non-default sd-server
                # port via agent_config.yaml's IMGGEN_PORT — fall back to
                # 1234 when the agent didn't report one.
                port = a.get("image_gen_port") if name == "image_gen" else None
                return f"http://{host}:{port or default_port}"
            return None
        return None
    # Treat any other string as an explicit URL (caller strips trailing /).
    return s.rstrip("/")


# ── Primary-agent dispatchers (used by ~40 routes in main) ───────────

def _picker_agent_id() -> "str | None":
    """The ?agent= query param, if any — the dashboard picker's selection.
    Read once in the dispatchers so no call site has to thread it through."""
    try:
        return flask_request.args.get("agent") or None
    except Exception:
        return None


def _resolve_target(pk: str, model_id: "str | None",
                    agent_id: "str | None",
                    allow_pool: bool = True) -> "tuple[dict | None, str | None]":
    """Resolve the target agent for a proxied call. Returns (agent, override).
    Precedence for llama: pin(model_id) > agent_id picker > pool RR > default.
    For everything else: agent_id picker > default. override == 'pin' when a
    model pin overrode an explicit picker selection (surfaced to the operator
    via log + X-Routing-Override header).

    allow_pool=False (streams) skips pool round-robin — a stream must follow
    a specific known host (picker, else default), never a rotating pick."""
    if pk == "llama":
        if model_id:
            pinned = agent_registry.pinned_llama_agent(model_id)
            if pinned:
                override = ("pin" if (agent_id and agent_id != pinned.get("agent_id"))
                           else None)
                return pinned, override
        if agent_id:
            a = agent_registry.resolve_agent_by_id(agent_id, capability="llama")
            if a:
                return a, None
        # Pin already resolved/excluded above — pass model_id=None so
        # pick_llama_agent doesn't re-run the pin lookup (which would log the
        # 'pinned but unavailable' warning a second time).
        if allow_pool:
            a = agent_registry.pick_llama_agent(None)
            if a:
                return a, None
        return _default_agent(pk), None
    if agent_id:
        a = agent_registry.resolve_agent_by_id(agent_id, capability=pk)
        if a:
            return a, None
    return _default_agent(pk), None


def _default_agent(pk: str) -> "dict | None":
    """The no-?agent= default — resolved through default_agent_id_for so it
    matches the picker's default chip + the agent-aware read endpoints
    (/api/metrics, /api/llama-state, …) exactly, regardless of whether the
    operator set default_<p>_id or only the legacy primary_<p>_id."""
    did = agent_registry.default_agent_id_for(pk)
    a = agent_registry.resolve_agent_by_id(did) if did else None
    return a or agent_registry.primary_agent(pk)


def proxy_to_primary(kind: str, method: str, path: str,
                     *, primary_kind: "str | None" = None,
                     timeout: float = 30,
                     model_id: "str | None" = None,
                     agent_id: "str | None" = None,
                     **kwargs):
    """General-purpose dispatcher: forwards a Flask request to the target
    agent for `kind`. agent_id defaults to the ?agent= picker selection;
    pin (model_id) still wins over it for load/unload."""
    pk = primary_kind or kind
    if agent_id is None:
        agent_id = _picker_agent_id()
    agent, override = _resolve_target(pk, model_id, agent_id)
    if not agent:
        return jsonify({
            "ok": False,
            "error": f"no primary {pk} agent set; pick one in the Admin tab → Agents",
        }), 503
    if not agent.get("token"):
        return jsonify({
            "ok": False,
            "error": f"primary {pk} agent has no token (not approved?)",
        }), 503

    if override == "pin":
        log.info("proxy %s %s → agent:%s (pin override; picker requested %s)",
                 method, path, agent["agent_id"][:8], (agent_id or "")[:8])

    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("Authorization", f"Bearer {agent['token']}")
    r, tried, err = agent_registry.agent_request(method, agent, path,
                                                 headers=headers, timeout=timeout, **kwargs)
    if r is None:
        log.warning("proxy %s %s → primary %s failed: %s (tried=%s)",
                    method, path, pk, err, tried)
        return jsonify({"ok": False, "error": "upstream agent request failed", "tried": tried}), 502

    log.info("proxy %s %s → agent:%s host=%s rc=%s (%.0fms)",
             method, path, agent["agent_id"][:8], agent.get("hostname"),
             r.status_code, r.elapsed.total_seconds() * 1000)
    ctype = r.headers.get("Content-Type", "application/json")
    resp = current_app.response_class(r.content, status=r.status_code, mimetype=ctype)
    resp.headers["X-Proxied-To"] = f"{agent['agent_id'][:8]}@{agent.get('hostname','?')}"
    if override == "pin":
        resp.headers["X-Routing-Override"] = "pin"
    return resp


def proxy_stream_to_primary(kind: str, path: str, *, primary_kind: "str | None" = None,
                            agent_id: "str | None" = None,
                            read_timeout: float = _STREAM_READ_TIMEOUT_S):
    """SSE/streaming variant of proxy_to_primary — opens a long-lived GET
    to the target agent's callback URL and pipes chunks back as a Flask
    Response. agent_id defaults to the ?agent= picker selection (no model
    pin on streams, so picker > default). read_timeout caps how long a
    silent upstream may pin the worker thread before the stream is reaped."""
    pk = primary_kind or kind
    if agent_id is None:
        agent_id = _picker_agent_id()
    agent, _override = _resolve_target(pk, None, agent_id, allow_pool=False)
    if not agent:
        return jsonify({
            "ok": False,
            "error": f"no primary {pk} agent set",
        }), 503

    urls = agent_registry.agent_callback_urls(agent)
    if not urls:
        return jsonify({"ok": False, "error": "no callback URL recorded"}), 502

    # A healthy stream's keepalives reset the read timeout, so it's never reaped
    # and pins its worker for the stream's whole life. Cap concurrent streams
    # below the pool size so they can't starve control requests; over the cap
    # the browser EventSource retries. Slot freed via call_on_close on success.
    if not stream_pool.POOL.try_acquire():
        return jsonify({"ok": False,
                        "error": "manager at stream capacity; retry shortly"}), 503
    slot_handed = False
    try:
        last_err = None
        for base in urls:
            full = f"{base}{path}"
            upstream = None
            handed_off = False
            try:
                upstream = requests.get(
                    full,
                    headers={"Authorization": f"Bearer {agent['token']}"},
                    stream=True, timeout=(5, read_timeout),
                    **agent_registry.agent_tls_kwargs(full),
                )

                log.info("proxy SSE %s → agent:%s host=%s rc=%s",
                         path, agent["agent_id"][:8], agent.get("hostname"),
                         upstream.status_code)
                response = current_app.response_class(
                    thread_pumped(upstream, path),
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
                # Close the upstream socket on any path that doesn't hand it
                # to the response generator — Flask never iterates _gen() when
                # response construction itself raises, so the finally in _gen
                # alone can't cover that window.
                if upstream is not None and not handed_off:
                    upstream.close()
        if last_err:
            log.warning("proxy stream %s → all callback URLs failed: %s", path, last_err)
        return jsonify({"ok": False, "error": "all callback URLs failed"}), 502
    finally:
        # Every non-handoff exit (all URLs failed, exception) frees the slot;
        # on a successful handoff call_on_close owns the release instead.
        if not slot_handed:
            stream_pool.POOL.release()


# ── Per-target reverse-proxy helpers ─────────────────────────────────

def _proxy_llmchat(path: str, base: str):
    url = base + "/" + path.lstrip("/")
    try:
        qs = flask_request.query_string.decode("utf-8")
        if qs:
            url += "?" + qs
        upstream = requests.request(
            method=flask_request.method,
            url=url,
            headers={k: v for k, v in flask_request.headers if k.lower() not in
                     ("host", "content-length", "transfer-encoding")},
            data=flask_request.get_data(),
            allow_redirects=False,
            timeout=15,
            stream=True,
        )
        headers = []
        for k, v in upstream.headers.items():
            kl = k.lower()
            if kl in _PROXY_HOP_BY_HOP:
                continue
            if kl == "set-cookie":
                v = re.sub(r';\s*Domain=[^;]+',   '', v, flags=re.IGNORECASE)
                v = re.sub(r';\s*SameSite=[^;]+', '', v, flags=re.IGNORECASE)
                v = re.sub(r';\s*Secure',         '', v, flags=re.IGNORECASE)
            if kl == "location" and v.startswith(base):
                v = v.replace(base, "/proxy/llmchat")
            headers.append((k, v))
        headers += _csp_header_pairs(upstream.headers.get("content-type", ""))
        return Response(upstream.iter_content(chunk_size=8192),
                        status=upstream.status_code,
                        headers=headers)
    except Exception as e:
        return _proxy_error("Proxy error", 502, e)


def _build_openclaw_ws_patch(netloc: str, port: str) -> str:
    return (
        "<script>"
        "(function(){"
        "var _WS=window.WebSocket;"
        "window.WebSocket=function(url,p){"
        f"if(/^wss?:\\/\\//.test(url)&&!/^wss?:\\/\\/[^/]+:{port}/.test(url)){{"
        f"url=url.replace(/^(wss?:\\/\\/)[^/]+(\\/.*)$/,'$1{netloc}$2');"
        "}"
        "return p?new _WS(url,p):new _WS(url);"
        "};"
        "Object.assign(window.WebSocket,{CONNECTING:0,OPEN:1,CLOSING:2,CLOSED:3});"
        "})();"
        "</script>"
    )


def _proxy_openclaw(path: str, base: str):
    netloc = urlparse(base).netloc or ""
    port = (netloc.split(":") + ["80"])[1]
    ws_patch = _build_openclaw_ws_patch(netloc, port)
    url = base + "/" + path.lstrip("/")
    try:
        qs = flask_request.query_string.decode("utf-8")
        if qs:
            url += "?" + qs
        is_html_req = "text/html" in flask_request.headers.get("Accept", "")
        upstream = requests.request(
            method=flask_request.method,
            url=url,
            headers={k: v for k, v in flask_request.headers if k.lower() not in
                     ("host", "content-length", "transfer-encoding")},
            data=flask_request.get_data(),
            allow_redirects=False,
            timeout=15,
            stream=not is_html_req,  # don't stream HTML so we can inject the WS patch
        )
        headers = []
        for k, v in upstream.headers.items():
            kl = k.lower()
            if kl in _PROXY_HOP_BY_HOP:
                continue
            if kl == "set-cookie":
                v = re.sub(r';\s*Domain=[^;]+',   '', v, flags=re.IGNORECASE)
                v = re.sub(r';\s*SameSite=[^;]+', '', v, flags=re.IGNORECASE)
                v = re.sub(r';\s*Secure',         '', v, flags=re.IGNORECASE)
            if kl == "location" and v.startswith(base):
                v = v.replace(base, "/proxy/openclaw")
            headers.append((k, v))

        # For HTML responses, inject the WS-redirect shim before </head>.
        ct = upstream.headers.get("content-type", "")
        if "text/html" in ct:
            body = upstream.content.decode("utf-8", errors="replace")
            if "</head>" in body:
                body = body.replace("</head>", ws_patch + "</head>", 1)
            else:
                body = ws_patch + body
            headers += _csp_header_pairs(ct)
            return Response(body, status=upstream.status_code, headers=headers,
                            content_type=ct)

        return Response(upstream.iter_content(chunk_size=8192),
                        status=upstream.status_code,
                        headers=headers)
    except Exception as e:
        return _proxy_error("Proxy error", 502, e)


def _proxy_imggen(path: str, base: str):
    url = base + "/" + path.lstrip("/")
    try:
        qs = flask_request.query_string.decode("utf-8")
        if qs:
            url += "?" + qs
        is_html_req = "text/html" in flask_request.headers.get("Accept", "")
        upstream = requests.request(
            method=flask_request.method,
            url=url,
            headers={k: v for k, v in flask_request.headers if k.lower() not in
                     ("host", "content-length", "transfer-encoding")},
            data=flask_request.get_data(),
            allow_redirects=False,
            timeout=settings.manager.timeouts.generic_http,
            stream=not is_html_req,  # don't stream HTML so we can inject the WS patch
        )
        headers = []
        for k, v in upstream.headers.items():
            kl = k.lower()
            if kl in _PROXY_HOP_BY_HOP:
                continue
            if kl == "set-cookie":
                v = re.sub(r';\s*Domain=[^;]+',   '', v, flags=re.IGNORECASE)
                v = re.sub(r';\s*SameSite=[^;]+', '', v, flags=re.IGNORECASE)
                v = re.sub(r';\s*Secure',         '', v, flags=re.IGNORECASE)
            if kl == "location" and v.startswith(base):
                v = v.replace(base, "/proxy/imggen")
            headers.append((k, v))
        headers += _csp_header_pairs(upstream.headers.get("content-type", ""))
        return Response(upstream.iter_content(chunk_size=8192),
                        status=upstream.status_code, headers=headers)
    except Exception as e:
        return _proxy_error("Image Generation proxy error", 502, e)


def _proxy_sdcpp(path: str, base: str):
    """Proxy a /sdcpp/... request to the image generation backend (relative
    SPA calls from the image-gen UI)."""
    url = base + "/sdcpp/" + path.lstrip("/")
    headers = {k: v for k, v in flask_request.headers if k.lower() != "host"}
    headers["X-Forwarded-For"] = flask_request.remote_addr or ""
    headers["X-Forwarded-Proto"] = flask_request.scheme
    headers["X-Forwarded-Host"] = flask_request.host

    method = flask_request.method
    if method == "GET":
        upstream = requests.get(url, headers=headers, params=flask_request.args,
                                timeout=120, stream=True)
    elif method == "POST":
        upstream = requests.post(url, headers=headers,
                                 data=flask_request.get_data(),
                                 timeout=120, stream=True)
    elif method == "PUT":
        upstream = requests.put(url, headers=headers,
                                data=flask_request.get_data(),
                                timeout=120, stream=True)
    elif method == "DELETE":
        upstream = requests.delete(url, headers=headers,
                                   timeout=120, stream=True)
    elif method == "PATCH":
        upstream = requests.patch(url, headers=headers,
                                  data=flask_request.get_data(),
                                  timeout=120, stream=True)
    else:
        return _proxy_error("Method not supported for /sdcpp proxy", 405, method)

    resp_headers = {}
    for k, v in upstream.headers.items():
        if k.lower() not in _PROXY_HOP_BY_HOP:
            resp_headers[k] = v
    for name, val in _csp_header_pairs(upstream.headers.get("content-type", "")):
        resp_headers[name] = val

    return Response(upstream.iter_content(chunk_size=8192),
                    status=upstream.status_code, headers=resp_headers)


def _resolve_alarm_agent_param(path: str, args) -> list:
    """Rewrite a browser-supplied ?agent=<id> on metric reads into the
    ?hostname= the alarm engine filters by, so the frontend never keys
    catalog/history reads by hostname. Unresolvable agent → drop the param
    (no host filter) rather than forcing empty results."""
    params = list(args.items(multi=True))
    if not path.lstrip("/").startswith("metrics"):
        return params
    agent_id = args.get("agent")
    if not agent_id:
        return params
    params = [(k, v) for (k, v) in params if k != "agent"]
    agent = agent_registry.resolve_agent_by_id(agent_id)
    hostname = (agent or {}).get("hostname")
    if hostname and not any(k == "hostname" for k, _ in params):
        params.append(("hostname", hostname))
    return params


def _proxy_alarm_engine(path: str):
    """Proxy /api/alarm/* requests to the alarm engine via the shared
    ctx.ae_session (which verifies against the internal CA when AE TLS
    is on)."""
    ae_url = _deps.ctx.alarm_engine_url()
    if not ae_url:
        return Response("Alarm engine URL not configured", status=502)
    url = f"{ae_url.rstrip('/')}/api/alarm/" + path.lstrip("/")
    params = _resolve_alarm_agent_param(path, flask_request.args)
    try:
        upstream = _deps.ctx.ae_session.request(
            method=flask_request.method,
            url=url,
            headers={k: v for k, v in flask_request.headers if k.lower() not in
                     ("host", "content-length", "transfer-encoding")},
            data=flask_request.get_data(),
            params=params,
            allow_redirects=True,
            timeout=settings.manager.timeouts.generic_http,
            stream=(flask_request.method == "GET" and
                    "text/event-stream" not in flask_request.headers.get("Accept", "")),
        )
        excluded = {"content-security-policy", "x-frame-options",
                    "transfer-encoding", "content-encoding", "content-length",
                    "connection"}
        headers = []
        for k, v in upstream.headers.items():
            kl = k.lower()
            if kl in excluded:
                continue
            headers.append((k, v))
        headers += _csp_header_pairs(upstream.headers.get("content-type", ""))
        if (flask_request.method == "GET" and
                "text/event-stream" in upstream.headers.get("content-type", "")):
            return Response(upstream.content, status=upstream.status_code, headers=headers,
                            mimetype="text/event-stream")
        return Response(upstream.iter_content(chunk_size=8192),
                        status=upstream.status_code, headers=headers)
    except Exception as e:
        return _proxy_error("Alarm engine proxy error", 502, e)


# ── AE WebSocket URL builder + AE-frontend injection ─────────────────

def ae_ws_url_for_browser() -> str:
    """Translate the alarm-engine URL into a WebSocket URL the BROWSER
    can dial directly (Flask/Werkzeug can't proxy WS frames). Loopback
    hosts get rewritten via the same helper the heartbeat ack uses, so
    colocated installs work without forcing the operator to advertise
    a LAN IP.

    Hostnames are resolved to IPs server-side. When the operator sets
    [manager].alarm_engine_url to something like http://ae-host:8081,
    the manager can resolve "ae-host" against its own DNS / /etc/hosts
    but the user's browser usually can't — and the WebSocket open
    silently fails, which is what surfaces as the pill stuck on
    "Disconnected". Substituting the IP keeps the URL reachable from
    any client on the same network. Resolution failures fall through
    to the original hostname so this can't make the situation worse.
    """
    ae_url = _deps.ctx.alarm_engine_url()
    if not ae_url:
        return ""
    # When the standalone WS proxy is enabled, send browsers there instead of
    # straight to the AE — the proxy verifies the AE's internal-CA cert so the
    # browser doesn't have to trust it. getattr() guards an upgraded deploy
    # whose local config/unified_config.py predates these fields (the file is
    # gitignored and update.sh doesn't re-render it).
    ws_proxy_port = int(getattr(settings.manager, "ws_proxy_port", 0) or 0)
    if ws_proxy_port > 0:
        # The proxy always serves plain ws — manager-tls.{crt,key} is signed
        # by the internal CA, which the browser doesn't trust by default, so
        # serving wss here defeats the proxy's whole point (which is to hide
        # the internal CA from the browser). For https-dashboard users who
        # want wss end-to-end, the operator must front the proxy with a
        # real-CA cert (nginx/Caddy) — out of scope here.
        return f"ws://{_deps.request_host_no_port()}:{ws_proxy_port}/ws/alarm"
    rewritten = _deps.rewrite_loopback_host(ae_url.rstrip("/"),
                                            _deps.request_host_no_port())
    parts = urlsplit(rewritten)
    ae_host = parts.hostname or ""
    ae_port = parts.port or (443 if parts.scheme == "https" else 80)
    ws_scheme = "wss" if parts.scheme == "https" else "ws"
    # Only resolve when the host *looks* like a name (contains a letter).
    # Skips pure IPv4 / bracketed IPv6 literals which are already routable.
    if ae_host and any(c.isalpha() for c in ae_host) and ae_host not in ("localhost",):
        try:
            ae_host = socket.gethostbyname(ae_host)
        except (socket.gaierror, OSError) as e:
            log.debug("AE-URL hostname resolution failed for %s: %s — "
                      "passing through as-is", ae_host, e)
    return f"{ws_scheme}://{ae_host}:{ae_port}/ws"


def _inject_alarm_ws_url(html_bytes: bytes) -> bytes:
    """Insert `<script>window.ALARM_WS_URL=...</script>` just before </head>
    so the AE frontend's websocket.js opens its WebSocket against the
    real AE host instead of the manager (which can't proxy WS frames)."""
    ws = ae_ws_url_for_browser()
    if not ws:
        return html_bytes
    snippet = (b'<script>window.ALARM_WS_URL='
               + repr(ws).encode("utf-8")
               + b';</script></head>')
    if b"</head>" not in html_bytes:
        return html_bytes
    return html_bytes.replace(b"</head>", snippet, 1)


# ── Route registration ───────────────────────────────────────────────

def register_routes(app, ctx, *,
                    repo_root: Path,
                    install_topology: Callable[[], dict],
                    request_host_no_port: Callable[[], str],
                    rewrite_loopback_host: Callable[[str, str], str]) -> None:
    """Wire the 7 catch-all proxy routes into `app`. Shared deps come
    from ctx (ae_session, alarm_engine_url, require_admin); the module-
    specific kwargs are repo_root (for the AE SPA dir), install_topology
    (split-install detection), and the two URL helpers used by
    ae_ws_url_for_browser.

    `current_app` is used inside the route bodies so we don't need to
    stash `app` on _deps.
    """
    global _ALARM_FRONTEND_DIR
    _deps.ctx = ctx
    _deps.install_topology = install_topology
    _deps.request_host_no_port = request_host_no_port
    _deps.rewrite_loopback_host = rewrite_loopback_host

    _ALARM_FRONTEND_DIR = str(Path(repo_root) / "llm-systems-alarm-engine" / "frontend")
    # Make sure send_from_directory hands modules/CSS back with the right type.
    _mimetypes.init()
    _mimetypes.add_type("application/javascript", ".mjs")
    _mimetypes.add_type("text/css", ".css")

    @app.route("/proxy/llmchat/", defaults={"path": ""}, methods=_PROXY_METHODS)
    @app.route("/proxy/llmchat/<path:path>", methods=_PROXY_METHODS)
    def proxy_llmchat(path):
        target = resolve_proxy_target("llm_chat")
        if not target:
            return jsonify({"error": "llm_chat proxy disabled in config"}), 404
        return _proxy_llmchat(path, target)

    @app.route("/proxy/openclaw/", defaults={"path": ""}, methods=_PROXY_METHODS)
    @app.route("/proxy/openclaw/<path:path>", methods=_PROXY_METHODS)
    def proxy_openclaw(path):
        target = resolve_proxy_target("openclaw")
        if not target:
            return jsonify({"error": "openclaw proxy disabled in config"}), 404
        return _proxy_openclaw(path, target)

    @app.route("/proxy/imggen/", defaults={"path": ""}, methods=_PROXY_METHODS)
    @app.route("/proxy/imggen/<path:path>", methods=_PROXY_METHODS)
    def proxy_imggen(path):
        target = resolve_proxy_target("image_gen")
        if not target:
            return jsonify({"error": "image_gen proxy disabled in config"}), 404
        return _proxy_imggen(path, target)

    @app.route("/sdcpp/", defaults={"path": ""}, methods=_PROXY_METHODS)
    @app.route("/sdcpp/<path:path>", methods=_PROXY_METHODS)
    def proxy_sdcpp(path):
        """Catch-all proxy for /sdcpp/... routes → image generation backend."""
        target = resolve_proxy_target("image_gen")
        if not target:
            return jsonify({"error": "image_gen proxy disabled in config"}), 404
        return _proxy_sdcpp(path, target)

    @app.route("/api/alarm/", defaults={"path": ""}, methods=_PROXY_METHODS)
    @app.route("/api/alarm/<path:path>", methods=_PROXY_METHODS)
    def proxy_alarm_engine(path):
        """Catch-all proxy for /api/alarm/... → alarm engine. admin/* and
        dbstats (DB internals) get the same IP gate as native admin routes
        before forwarding (security #124)."""
        if path.startswith("admin/") or path.startswith("dbstats"):
            deny = _deps.ctx.require_admin()
            if deny is not None:
                return deny
        return _proxy_alarm_engine(path)

    # ── Alarm Engine WebSocket — see _maybe_start_alarm_ws_proxy in main.
    # The Flask /ws/alarm route can't actually upgrade (Cheroot WSGI doesn't
    # speak WS); the real proxy is the websockets server started in a daemon
    # thread, listening on [manager].ws_proxy_port. Browsers reach it directly.
    # This Flask route exists only so a stray HTTP GET to /ws/alarm gets a
    # helpful 426 instead of a confusing 404.
    @app.route("/ws/alarm", methods=["GET"])
    def proxy_alarm_websocket():
        return Response(
            "WebSocket endpoint — connect with a WS client, not HTTP. See "
            "[manager].ws_proxy_port for the proxy port (default off).",
            status=426,  # Upgrade Required
        )

    @app.route("/alarm/")
    @app.route("/alarm/<path:filename>")
    def serve_alarm_frontend(filename="index.html"):
        """Serve the alarm engine frontend. On a colocated install we read
        the static files from disk. On a split install (Mode 3 — manager
        only) the `llm-systems-alarm-engine/` tree was excluded from the
        deploy, so we proxy the request to the remote alarm engine instead.
        Either way the Events tab iframe and its asset requests resolve.
        For index.html we also inject a <script>window.ALARM_WS_URL=...
        </script> so the live pill's WebSocket connects directly to the AE
        (manager can't proxy WS)."""
        is_index = filename in ("", "index.html")
        topo = _deps.install_topology()
        # Prefer the local on-disk copy when colocated; on a split install
        # the local tree may be missing entirely so we proxy instead.
        if not topo["split"] and os.path.isdir(_ALARM_FRONTEND_DIR):
            if not is_index:
                from werkzeug.exceptions import NotFound
                try:
                    return send_from_directory(_ALARM_FRONTEND_DIR, filename)
                except NotFound:
                    pass
            idx = os.path.join(_ALARM_FRONTEND_DIR, "index.html")
            with open(idx, "rb") as f:
                body = _inject_alarm_ws_url(f.read())
            return Response(body, mimetype="text/html",
                            headers=_csp_header_pairs("text/html"))
        ae_url = _deps.ctx.alarm_engine_url()
        if not ae_url:
            return Response("alarm engine frontend not deployed locally and "
                            "[manager].alarm_engine_url is not set", status=502)
        upstream_path = "" if is_index else filename.lstrip("/")
        try:
            upstream = _deps.ctx.ae_session.get(
                f"{ae_url.rstrip('/')}/{upstream_path}",
                timeout=settings.manager.timeouts.generic_http,
                stream=not is_index,   # buffer index so we can inject the WS URL
            )
        except Exception as e:
            return _proxy_error("alarm engine frontend proxy error", 502, e)
        excluded = {"content-security-policy", "x-frame-options",
                    "transfer-encoding", "content-encoding", "content-length",
                    "connection"}
        headers = [(k, v) for k, v in upstream.headers.items()
                   if k.lower() not in excluded]
        if is_index:
            body = _inject_alarm_ws_url(upstream.content)
            headers += _csp_header_pairs("text/html")
            return Response(body, status=upstream.status_code, headers=headers,
                            mimetype="text/html")
        return Response(upstream.iter_content(chunk_size=8192),
                        status=upstream.status_code, headers=headers)
