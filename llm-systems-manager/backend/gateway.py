"""OpenAI-compatible inference gateway (#214, vllm #125). Routes
chat/completion requests to a healthy provider agent — llama:
pin > ?agent= picker > pool RR > default; vllm: picker > default."""
from __future__ import annotations

import json
import logging

import requests
from flask import Response, jsonify, request as flask_request

import agent_registry
import proxies
import stream_pool
from config.unified_config import settings

log = logging.getLogger("llm-systems-manager.gateway")

# Per-provider gateway sub-path -> agent passthrough route (allowlist).
_AGENT_PATHS = {
    "llama": {
        "chat/completions": "/llama/openai/chat/completions",
        "completions": "/llama/openai/completions",
    },
    "vllm": {
        "chat/completions": "/vllm/openai/chat/completions",
        "completions": "/vllm/openai/completions",
    },
}

_MODELS_PATHS = {"llama": "/llama/models", "vllm": "/vllm/models"}


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
    """Ordered failover list: resolved primary first, then remaining live
    pool members (llama only) + default, deduped by agent_id."""
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
    if provider == "llama":
        try:
            data = agent_registry.load_agents()
            ids = list((data.get("global") or {}).get("llama_pool") or [])
        except Exception:
            ids = []
    did = agent_registry.default_agent_id_for(provider)
    if did:
        ids.append(did)
    for aid in ids:
        agent = agent_registry.resolve_agent_by_id(aid, capability=provider)
        _add(agent)
    # Order live backends first, non-live after as failover — so a stale/down
    # agent (incl. an explicit ?agent= pick) never jumps ahead but stays reachable.
    live, rest = [], []
    for a in ordered:
        (live if agent_registry.agent_liveness(a) == "live" else rest).append(a)
    return live + rest


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


def _handle_completion(sub: str, provider: str = "llama") -> Response:
    if not _gw_enabled():
        return _oai_error("gateway disabled", 503, "disabled")
    body = flask_request.get_json(silent=True)
    if not isinstance(body, dict):
        return _oai_error("invalid JSON body", 400, "invalid_request_error")
    model_id = body.get("model") or None
    agent_id = flask_request.args.get("agent") or None
    wants_stream = bool(body.get("stream"))
    path = _AGENT_PATHS[provider][sub]
    errors = []
    for agent in _candidates(model_id, agent_id, provider):
        if wants_stream:
            resp = _stream_from(agent, path, body, errors)
            if resp is not None:
                return resp
            continue
        r, err = _forward_json(agent, path, body)
        if r is None:
            errors.append(f"{_label(agent)}: {err}")
            continue
        if r.status_code in (502, 503):
            errors.append(f"{_label(agent)}: {r.status_code}")
            continue
        return Response(r.content, status=r.status_code,
                        mimetype=r.headers.get("content-type") or "application/json",
                        headers={"X-Proxied-To": _label(agent)})
    log.warning("gateway %s: no usable %s agent (%s)",
                sub, provider, "; ".join(errors) or "no candidates")
    return _oai_error(f"no {provider} backend available", 503)


def _stream_from(agent: dict, path: str, body: dict, errors: list):
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
        resp = Response(
            proxies.thread_pumped(upstream, path,
                                  max_lifetime_s=proxies._STREAM_OP_MAX_LIFETIME_S),
            status=upstream.status_code, mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Proxied-To": _label(agent)})
        resp.call_on_close(stream_pool.POOL.release)
        handed_off = True
        return resp
    finally:
        # Construction can raise after the slot is taken; release + close here.
        if not handed_off:
            upstream.close()
            stream_pool.POOL.release()


def _gateway_models(provider: str = "llama") -> Response:
    if not _gw_enabled():
        return _oai_error("gateway disabled", 503, "disabled")
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
            if mid and mid not in seen:
                seen.add(mid)
                merged.append(m)
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

    @app.route("/api/gateway/vllm/v1/chat/completions", methods=["POST"])
    def gateway_vllm_chat_completions():
        return _handle_completion("chat/completions", provider="vllm")

    @app.route("/api/gateway/vllm/v1/completions", methods=["POST"])
    def gateway_vllm_completions():
        return _handle_completion("completions", provider="vllm")

    @app.route("/api/gateway/vllm/v1/models", methods=["GET"])
    def gateway_vllm_models():
        return _gateway_models(provider="vllm")
