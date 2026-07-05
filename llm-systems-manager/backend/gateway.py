"""OpenAI-compatible inference gateway (#214). Routes chat/completion
requests to a healthy llama agent: pin > ?agent= picker > pool RR > default."""
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

# Gateway sub-path -> agent passthrough route (allowlist, nothing else proxied).
_AGENT_PATHS = {
    "chat/completions": "/llama/openai/chat/completions",
    "completions": "/llama/openai/completions",
}


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


def _candidates(model_id, agent_id) -> list:
    """Ordered failover list: resolved primary first, then remaining live
    llama-pool members + default, deduped by agent_id."""
    ordered, seen = [], set()

    def _add(agent):
        aid = (agent or {}).get("agent_id")
        if agent and aid and aid not in seen:
            seen.add(aid)
            ordered.append(agent)

    try:
        primary, _ = proxies._resolve_target("llama", model_id, agent_id,
                                             allow_pool=True)
    except Exception as e:
        log.warning("gateway: resolve failed: %s", e)
        primary = None
    _add(primary)
    ids: list = []
    try:
        data = agent_registry.load_agents()
        ids = list((data.get("global") or {}).get("llama_pool") or [])
    except Exception:
        ids = []
    did = agent_registry.default_agent_id_for("llama")
    if did:
        ids.append(did)
    for aid in ids:
        agent = agent_registry.resolve_agent_by_id(aid, capability="llama")
        if agent and agent_registry.agent_liveness(agent) == "live":
            _add(agent)
    return ordered


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


def _handle_completion(sub: str) -> Response:
    if not _gw_enabled():
        return _oai_error("gateway disabled", 503, "disabled")
    body = flask_request.get_json(silent=True)
    if not isinstance(body, dict):
        return _oai_error("invalid JSON body", 400, "invalid_request_error")
    model_id = body.get("model") or None
    agent_id = flask_request.args.get("agent") or None
    wants_stream = bool(body.get("stream"))
    path = _AGENT_PATHS[sub]
    errors = []
    for agent in _candidates(model_id, agent_id):
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
    log.warning("gateway %s: no usable llama agent (%s)",
                sub, "; ".join(errors) or "no candidates")
    return _oai_error("no llama backend available", 503)


def _stream_from(agent: dict, path: str, body: dict, errors: list):
    """One streaming attempt; None means try the next candidate."""
    return None  # implemented in Task 4


def _gateway_models() -> Response:
    if not _gw_enabled():
        return _oai_error("gateway disabled", 503, "disabled")
    merged, seen = [], set()
    for agent in _candidates(None, None):
        r, _tried, _err = agent_registry.agent_request(
            "GET", agent, "/llama/models",
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
