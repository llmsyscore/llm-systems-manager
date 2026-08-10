"""OpenAI-compatible inference gateway (#214, vllm #125, multi-pool #493).
/v1/models merges every gateway provider's pool; /v1/chat|completions resolve
the owning provider per model (pin > model index > llama), then route within
it: pin > ?agent= picker > pool RR > default. Completion responses for
_USAGE_COUNTED_PROVIDERS also feed gateway_usage token counters (#496)."""
from __future__ import annotations

import json
import logging
import threading
import time

import requests
from flask import Response, jsonify, request as flask_request

import agent_registry
import gateway_usage
import providers
import proxies
import stream_pool
from config.unified_config import settings

log = logging.getLogger("llm-systems-manager.gateway")

# Providers whose tokens are counted from proxied response usage (#496).
_USAGE_COUNTED_PROVIDERS = ("lms",)

# Per-provider gateway sub-path -> agent passthrough route (allowlist), for
# every gateway_enabled spec. The Flask routes stay per-provider (bottom).
_GATEWAY_SUBS = ("chat/completions", "completions")
_GATEWAY_PROVIDERS = tuple(
    p for p in providers.names()
    if getattr(providers.get(p), "gateway_enabled", False))
_AGENT_PATHS = {p: {s: f"/{p}/openai/{s}" for s in _GATEWAY_SUBS}
                for p in _GATEWAY_PROVIDERS}
_MODELS_PATHS = {p: f"/{p}/models" for p in _GATEWAY_PROVIDERS}


def _gw_cfg():
    return getattr(settings.manager, "gateway", None)


def _gw_enabled() -> bool:
    return bool(getattr(_gw_cfg(), "enabled", True))


def _read_timeout_s() -> float:
    return float(getattr(_gw_cfg(), "read_timeout_s", 600.0) or 600.0)


def _oai_error(message: str, status: int, err_type: str = "unavailable") -> Response:
    body = {"error": {"message": message, "type": err_type, "code": status}}
    return Response(json.dumps(body), status=status, mimetype="application/json")


def _label(agent: dict) -> str:
    return f"{(agent.get('agent_id') or '')[:8]}@{agent.get('hostname') or '?'}"


def _candidates(model_id, agent_id, provider="llama") -> list:
    """Ordered failover list: pin/picker primary first, then live agents
    serving model_id per the index, then other live, then non-live."""
    ordered, seen = [], set()

    def _add(agent):
        aid = (agent or {}).get("agent_id")
        if agent and aid and aid not in seen:
            seen.add(aid)
            ordered.append(agent)

    try:
        primary, override = proxies._resolve_target(provider, model_id, agent_id,
                                                    allow_pool=True)
        if override == "pin":
            log.info("gateway: model pin overrode ?agent=%s for model %s",
                     (agent_id or "")[:8], model_id)
    except Exception as e:
        log.warning("gateway: resolve failed: %s", e)
        primary = None
    _add(primary)
    ids: list = []
    spec = providers.get(provider)
    if spec and spec.default_picker == "pool":
        try:
            data = agent_registry.load_agents()
            ids = list((data.get("global") or {}).get(f"{provider}_pool") or [])
        except Exception:
            ids = []
    did = agent_registry.default_agent_id_for(provider)
    if did:
        ids.append(did)
    cap = spec.capability_key if spec else provider
    for aid in ids:
        agent = agent_registry.resolve_agent_by_id(aid, capability=cap)
        _add(agent)
    # Order live backends first, non-live after as failover — so a stale/down
    # agent (incl. an explicit ?agent= pick) never jumps ahead but stays reachable.
    live, rest = [], []
    for a in ordered:
        (live if agent_registry.agent_liveness(a) == "live" else rest).append(a)
    if model_id:
        srv = _serving_agent_ids(provider, model_id)
        if srv:
            # A pin or explicit ?agent= primary keeps first place; only the
            # RR/default remainder is reordered by the serving index.
            explicit = _explicit_primary_id(primary, agent_id, provider, model_id)
            live.sort(key=lambda a: (a.get("agent_id") != explicit,
                                     a.get("agent_id") not in srv))
    return live + rest


def _explicit_primary_id(primary, agent_id, provider, model_id) -> "str | None":
    """agent_id of the resolved primary when it came from a model pin or an
    explicit ?agent= pick; None for RR/default picks."""
    pid = (primary or {}).get("agent_id")
    if not pid:
        return None
    if agent_id and pid == agent_id:
        return pid
    try:
        pinned = agent_registry.pinned_agent(provider, model_id)
    except Exception:
        return None
    return pid if (pinned and pinned.get("agent_id") == pid) else None


def _forward_json(agent: dict, path: str, body: dict):
    r, _tried, err = agent_registry.agent_request(
        "POST", agent, path, json=body,
        headers={"Authorization": f"Bearer {agent.get('token') or ''}"},
        timeout=(4, _read_timeout_s()))
    return (r, None) if r is not None else (None, err)


def _dial_stream(agent: dict, path: str, body: dict):
    token = agent.get("token") or ""
    for base in agent_registry.agent_callback_urls(agent):
        url = f"{base}{path}"
        try:
            return requests.post(
                url, json=body, stream=True,
                headers={"Authorization": f"Bearer {token}"},
                timeout=(5, _read_timeout_s()),
                **agent_registry.agent_tls_kwargs(url))
        except requests.exceptions.RequestException as e:
            log.debug("gateway dial %s failed: %s", url, type(e).__name__)
    return None


def _with_usage_probe(body: dict) -> "tuple[dict, bool]":
    """Copy with stream_options.include_usage when the client sent none."""
    if "stream_options" in body:
        return body, False
    return {**body, "stream_options": {"include_usage": True}}, True


def _handle_completion(sub: str, provider=None) -> Response:
    if not _gw_enabled():
        return _oai_error("gateway disabled", 503, "disabled")
    body = flask_request.get_json(silent=True)
    if not isinstance(body, dict):
        return _oai_error("invalid JSON body", 400, "invalid_request_error")
    model_id = body.get("model") or None
    if provider is None:
        provider = _provider_for_model(model_id)
    agent_id = flask_request.args.get("agent") or None
    wants_stream = bool(body.get("stream"))
    path = _AGENT_PATHS[provider][sub]
    errors = []
    stream_body, injected = body, False
    if wants_stream and provider in _USAGE_COUNTED_PROVIDERS:
        stream_body, injected = _with_usage_probe(body)
    for agent in _candidates(model_id, agent_id, provider):
        if wants_stream:
            resp = _stream_from(agent, path, stream_body, errors, provider,
                                strip_usage=injected)
            if resp is not None:
                return resp
            continue
        aid = agent.get("agent_id")
        gateway_usage.begin(aid)
        try:
            r, err = _forward_json(agent, path, body)
        finally:
            gateway_usage.end(aid)
        if r is None:
            errors.append(f"{_label(agent)}: {err}")
            continue
        if r.status_code in (502, 503):
            errors.append(f"{_label(agent)}: {r.status_code}")
            continue
        if (provider in _USAGE_COUNTED_PROVIDERS
                and 200 <= r.status_code < 300):
            u = gateway_usage.usage_from_json_bytes(r.content)
            if u:
                gateway_usage.record(aid, *u)
        return Response(r.content, status=r.status_code,
                        mimetype=r.headers.get("content-type") or "application/json",
                        headers={"X-Proxied-To": _label(agent)})
    log.warning("gateway %s: no usable %s agent (%s)",
                sub, provider, "; ".join(errors) or "no candidates")
    return _oai_error(f"no {provider} backend available", 503)


def _stream_from(agent: dict, path: str, body: dict, errors: list,
                 provider: str = "llama", strip_usage: bool = False):
    """One streaming attempt; None means try the next candidate."""
    upstream = _dial_stream(agent, path, body)
    if upstream is None:
        errors.append(f"{_label(agent)}: unreachable")
        return None
    if upstream.status_code in (502, 503):
        upstream.close()
        errors.append(f"{_label(agent)}: {upstream.status_code}")
        return None
    ctype = (upstream.headers.get("content-type") or "").lower()
    if "text/event-stream" not in ctype:
        # Upstream answered non-stream (e.g. 400 validation error): relay as-is.
        content, status = upstream.content, upstream.status_code
        upstream.close()
        return Response(content, status=status,
                        mimetype=ctype or "application/json")
    if not stream_pool.POOL.try_acquire():
        upstream.close()
        return _oai_error("manager at stream capacity; retry shortly", 503)
    handed_off = False
    try:
        pumped = proxies.thread_pumped(
            upstream, path, max_lifetime_s=proxies._STREAM_OP_MAX_LIFETIME_S)
        if provider in _USAGE_COUNTED_PROVIDERS:
            aid = agent.get("agent_id")
            pumped = gateway_usage.tap_sse(
                pumped, lambda p, g, a=aid: gateway_usage.record(a, p, g),
                strip_usage=strip_usage)
        resp = Response(
            pumped,
            status=upstream.status_code, mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Proxied-To": _label(agent)})
        resp.call_on_close(stream_pool.POOL.release)
        # Same lifecycle as the stream slot: a streamed request is in flight
        # until the response closes, not until this function returns.
        gw_aid = agent.get("agent_id")
        resp.call_on_close(lambda a=gw_aid: gateway_usage.end(a))
        # Increment last: nothing below can raise, so the pair can't leak if
        # the response ends up discarded instead of returned.
        gateway_usage.begin(gw_aid)
        handed_off = True
        return resp
    finally:
        # Construction can raise after the slot is taken; release + close here.
        if not handed_off:
            upstream.close()
            stream_pool.POOL.release()


def _entry_resident(m) -> bool:
    """False for llama-router catalog entries whose status marks the model
    not resident on that host; entries without a status dict count."""
    st = (m or {}).get("status")
    if isinstance(st, dict):
        return st.get("value") in ("loaded", "loading", "sleeping")
    return True


def _fetch_provider_models(provider: str, serving: "dict | None" = None) -> list:
    """Provider-tagged model entries merged from every candidate agent.
    When given, serving accumulates model_id -> [agent_ids] as it goes."""
    merged, seen = [], set()
    for agent in _candidates(None, None, provider):
        r, _tried, _err = agent_registry.agent_request(
            "GET", agent, _MODELS_PATHS[provider],
            headers={"Authorization": f"Bearer {agent.get('token') or ''}"},
            timeout=(4, 15))
        if r is None or r.status_code != 200:
            continue
        try:
            data = (r.json() or {}).get("data") or []
        except ValueError:
            continue
        for m in data:
            mid = (m or {}).get("id")
            if not mid:
                continue
            if serving is not None and _entry_resident(m):
                serving.setdefault(f"{provider}:{mid}", []).append(
                    agent.get("agent_id"))
            if mid not in seen:
                seen.add(mid)
                merged.append({**m, "provider": provider})
    return merged


# model id -> owning provider (+ serving agent ids), rebuilt from a
# full-pool fan-out (TTL below).
_MODEL_INDEX_TTL_S = 30.0
_model_index_lock = threading.Lock()
_model_index: dict = {"ts": 0.0, "map": {}, "serving": {}, "refreshing": False}
_refresh_lock = threading.Lock()


def _store_model_index(mapping: dict, serving: "dict | None" = None) -> None:
    with _model_index_lock:
        _model_index["ts"] = time.time()
        _model_index["map"] = mapping
        if serving is not None:
            _model_index["serving"] = serving


def _serving_agent_ids(provider, model_id) -> set:
    with _model_index_lock:
        return set((_model_index["serving"] or {})
                   .get(f"{provider}:{model_id}") or ())


def _refresh_model_index() -> dict:
    """Single-flight full fan-out; concurrent callers reuse the winner's map."""
    with _refresh_lock:
        with _model_index_lock:
            if (time.time() - _model_index["ts"]) < _MODEL_INDEX_TTL_S:
                return dict(_model_index["map"])
        mapping: dict = {}
        serving: dict = {}
        for p in _GATEWAY_PROVIDERS:
            for m in _fetch_provider_models(p, serving=serving):
                owner = mapping.setdefault(m["id"], p)
                if owner != p:
                    log.debug("gateway: model id %s on %s shadowed by %s",
                              m["id"], p, owner)
        _store_model_index(mapping, serving)
        return mapping


def _refresh_model_index_async() -> None:
    """Kick a background index refresh; no-op when one is already running."""
    with _model_index_lock:
        if _model_index["refreshing"]:
            return
        _model_index["refreshing"] = True

    def _run():
        try:
            _refresh_model_index()
        except Exception as e:
            log.debug("gateway: async model index refresh failed: %s", e)
        finally:
            with _model_index_lock:
                _model_index["refreshing"] = False

    threading.Thread(target=_run, name="gw-model-index", daemon=True).start()


def _provider_for_model(model_id) -> str:
    """Pin > cached/fresh model index > llama fallback (#493). A stale cache
    hit is served as-is with a background refresh kicked off."""
    if not model_id:
        return "llama"
    for p in _GATEWAY_PROVIDERS:
        spec = providers.get(p)
        try:
            if spec and spec.pin_dict_key and agent_registry.pinned_agent(p, model_id):
                return p
        except Exception:
            log.debug("gateway: pin lookup failed for %s/%s", p, model_id)
    with _model_index_lock:
        fresh = (time.time() - _model_index["ts"]) < _MODEL_INDEX_TTL_S
        mapping = _model_index["map"]
        hit = mapping.get(model_id)
    if hit:
        if not fresh:
            _refresh_model_index_async()
        return hit
    if not fresh:
        try:
            mapping = _refresh_model_index()
        except Exception as e:
            log.warning("gateway: model index refresh failed: %s", e)
            mapping = {}
        hit = mapping.get(model_id)
        if hit:
            return hit
    return "llama"


def _gateway_models(provider=None) -> Response:
    """One provider's models, or (provider=None) all pools merged."""
    if not _gw_enabled():
        return _oai_error("gateway disabled", 503, "disabled")
    provs = (provider,) if provider else _GATEWAY_PROVIDERS
    merged, seen = [], set()
    serving: dict = {}
    for p in provs:
        for m in _fetch_provider_models(p, serving=serving):
            if m["id"] not in seen:
                seen.add(m["id"])
                merged.append(m)
    if provider is None:
        _store_model_index({m["id"]: m["provider"] for m in merged}, serving)
    return jsonify({"object": "list", "data": merged})


def register_routes(app, ctx) -> None:
    _ = ctx  # signature parity with the sibling Tier-3 modules

    @app.route("/api/gateway/v1/chat/completions", methods=["POST"])
    def gateway_chat_completions():
        return _handle_completion("chat/completions")

    @app.route("/api/gateway/v1/completions", methods=["POST"])
    def gateway_completions():
        return _handle_completion("completions")

    @app.route("/api/gateway/v1/models", methods=["GET"])
    def gateway_models():
        return _gateway_models()

    def _completion_handler(sub, p):
        def handler():
            return _handle_completion(sub, provider=p)
        return handler

    for p in _GATEWAY_PROVIDERS:
        for sub in _GATEWAY_SUBS:
            app.add_url_rule(f"/api/gateway/{p}/v1/{sub}",
                             f"gateway_{p}_{sub.replace('/', '_')}",
                             _completion_handler(sub, p), methods=["POST"])
        app.add_url_rule(f"/api/gateway/{p}/v1/models", f"gateway_{p}_models",
                         lambda p=p: _gateway_models(provider=p), methods=["GET"])
