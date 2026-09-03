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
import auth
import energy
import gateway_usage
import provider_state
import providers
import proxies
import stream_pool
from config.unified_config import settings

log = logging.getLogger("llm-systems-manager.gateway")

# Providers whose tokens are counted from proxied response usage (#496).
_USAGE_COUNTED_PROVIDERS = ("lms",)

# Upstream statuses that advance failover to the next candidate; every other
# status (incl. 500/504) is relayed verbatim to the client (#630).
_FAILOVER_STATUSES = (502, 503)

# Client hint on stream-pool 503s; slots normally drain within seconds (#628).
_POOL_RETRY_AFTER_S = 5

_POOL_READ_WARN_INTERVAL_S = 60.0
_pool_read_warn = {"ts": 0.0}
_pool_read_warn_lock = threading.Lock()


def _warn_pool_read_failed(e: Exception) -> None:
    """Rate-limited warning for pool-id read failures in _candidates (#632)."""
    with _pool_read_warn_lock:
        now = time.monotonic()
        if now - _pool_read_warn["ts"] < _POOL_READ_WARN_INTERVAL_S:
            return
        _pool_read_warn["ts"] = now
    log.warning("gateway: pool id read failed (%s: %s); treating pool as empty",
                type(e).__name__, e)


# Per-provider gateway sub-path -> agent passthrough route (allowlist), for
# every gateway_enabled spec. The Flask routes stay per-provider (bottom).
# Built once at import; a gateway_enabled change takes effect on restart (#651).
# Label handed to dashboard-session callers (no bearer presented).
GATEWAY_SESSION_LABEL = "session"

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


def _client_identity() -> "tuple[str, str]":
    """(label, ip) for the caller: its api-key label, else the session label."""
    label = auth.gateway_key_label() or GATEWAY_SESSION_LABEL
    return label, (flask_request.remote_addr or "")


def _proxied_to_header(agent: dict) -> dict:
    """X-Proxied-To header dict, or {} when gateway.expose_proxied_to
    is off (#631)."""
    if bool(getattr(_gw_cfg(), "expose_proxied_to", True)):
        return {"X-Proxied-To": _label(agent)}
    return {}


def _candidates(model_id, agent_id, provider="llama", advance_rr=True) -> list:
    """Ordered failover list: pin/picker primary first, then live agents
    serving model_id per the index, then other live, then non-live.
    advance_rr=False resolves pin/picker but never advances pool RR (#625, #652)."""
    ordered, seen = [], set()

    def _add(agent):
        aid = (agent or {}).get("agent_id")
        if agent and aid and aid not in seen:
            seen.add(aid)
            ordered.append(agent)

    primary = None
    if advance_rr or model_id or agent_id:
        try:
            primary, override = proxies._resolve_target(provider, model_id,
                                                        agent_id,
                                                        allow_pool=advance_rr)
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
        except Exception as e:
            _warn_pool_read_failed(e)
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
            r = requests.post(
                url, json=body, stream=True,
                headers={"Authorization": f"Bearer {token}"},
                timeout=(5, _read_timeout_s()),
                **agent_registry.agent_tls_kwargs(url))
            agent_registry.note_dial_result(agent, base, True)
            return r
        except requests.exceptions.RequestException as e:
            agent_registry.note_dial_error(agent, base, e)
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
    if (wants_stream and provider in _USAGE_COUNTED_PROVIDERS
            and bool(getattr(_gw_cfg(), "usage_probe", True))):
        stream_body, injected = _with_usage_probe(body)
    client = gateway_usage.client_begin(*_client_identity())
    t0 = time.perf_counter()
    stream_owns_client = False
    try:
        for agent in _candidates(model_id, agent_id, provider):
            if wants_stream:
                resp = _stream_from(agent, path, stream_body, errors, provider,
                                    strip_usage=injected, client=client, t0=t0)
                if resp is not None:
                    stream_owns_client = getattr(resp, "gw_client_owned", False)
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
            if r.status_code in _FAILOVER_STATUSES:
                errors.append(f"{_label(agent)}: {r.status_code}")
                continue
            gateway_usage.record_latency((time.perf_counter() - t0) * 1000.0)
            if 200 <= r.status_code < 300:
                u = gateway_usage.completion_usage_from_json_bytes(r.content)
                if u:
                    gateway_usage.client_record(client, *u)
                    if provider in _USAGE_COUNTED_PROVIDERS:
                        gateway_usage.record(aid, *u)
            else:
                gateway_usage.record_error()
            return Response(r.content, status=r.status_code,
                            mimetype=r.headers.get("content-type") or "application/json",
                            headers=_proxied_to_header(agent))
        log.warning("gateway %s: no usable %s agent (%s)",
                    sub, provider, "; ".join(errors) or "no candidates")
        gateway_usage.record_error()
        if not errors:
            # Zero candidates = nothing registered/configured for the provider —
            # a config state, not transient: non-retryable status + distinct type.
            return _oai_error(
                f"no {provider} backend registered — register an agent or set a "
                f"default/pool", 404, "no_backend")
        return _oai_error(f"no {provider} backend available", 503)
    finally:
        if not stream_owns_client:
            gateway_usage.client_end(client)


def _stream_from(agent: dict, path: str, body: dict, errors: list,
                 provider: str = "llama", strip_usage: bool = False,
                 client=None, t0=None):
    """One streaming attempt; None means try the next candidate."""
    upstream = _dial_stream(agent, path, body)
    if upstream is None:
        errors.append(f"{_label(agent)}: unreachable")
        return None
    if upstream.status_code in _FAILOVER_STATUSES:
        upstream.close()
        errors.append(f"{_label(agent)}: {upstream.status_code}")
        return None
    # Headers are in hand: this is the client's first byte.
    if t0 is not None:
        gateway_usage.record_latency((time.perf_counter() - t0) * 1000.0)
    ctype = (upstream.headers.get("content-type") or "").lower()
    if "text/event-stream" not in ctype:
        # Upstream answered non-stream (e.g. 400 validation error): relay as-is.
        content, status = upstream.content, upstream.status_code
        upstream.close()
        if status == 400 and strip_usage:
            log.warning("gateway: %s answered 400 after stream_options."
                        "include_usage injection — backend may reject "
                        "stream_options (disable gateway.usage_probe)",
                        _label(agent))
        if status >= 400:
            gateway_usage.record_error()
        return Response(content, status=status,
                        mimetype=ctype or "application/json")
    if not stream_pool.POOL.try_acquire():
        upstream.close()
        log.warning("gateway: stream pool at capacity, rejecting %s: %s",
                    _label(agent), stream_pool.POOL.stats())
        gateway_usage.record_error()
        resp = _oai_error("manager at stream capacity; retry shortly", 503)
        resp.headers["Retry-After"] = str(_POOL_RETRY_AFTER_S)
        return resp
    handed_off = False
    try:
        pumped = proxies.thread_pumped(
            upstream, path, max_lifetime_s=proxies._STREAM_OP_MAX_LIFETIME_S)
        if provider in _USAGE_COUNTED_PROVIDERS:
            aid = agent.get("agent_id")

            def _on_usage(p, g, a=aid, k=client):
                gateway_usage.record(a, p, g)
                gateway_usage.client_record(k, p, g)

            pumped = gateway_usage.tap_sse(pumped, _on_usage,
                                           strip_usage=strip_usage)
        resp = Response(
            pumped,
            status=upstream.status_code, mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     **_proxied_to_header(agent)})
        resp.call_on_close(stream_pool.POOL.release)
        # Same lifecycle as the stream slot: a streamed request is in flight
        # until the response closes, not until this function returns.
        gw_aid = agent.get("agent_id")
        resp.call_on_close(lambda a=gw_aid: gateway_usage.end(a))
        # The client slot follows the same lifecycle; the caller stops
        # closing it once gw_client_owned is set.
        resp.call_on_close(lambda k=client: gateway_usage.client_end(k))
        resp.gw_client_owned = True
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
    for agent in _candidates(None, None, provider, advance_rr=False):
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
# Longest a completion waits for an in-flight index refresh before it
# serves the llama fallback (#751, #752).
_MODEL_INDEX_WAIT_S = 5.0
_model_index_lock = threading.Lock()
_model_index_cond = threading.Condition(_model_index_lock)
_model_index: dict = {"ts": 0.0, "map": {}, "serving": {}, "entries": None,
                      "refreshing": False}
_refresh_lock = threading.Lock()


def _store_model_index(mapping: dict, serving: "dict | None" = None,
                       entries: "list | None" = None) -> None:
    with _model_index_lock:
        _model_index["ts"] = time.time()
        _model_index["map"] = mapping
        if serving is not None:
            _model_index["serving"] = serving
        if entries is not None:
            _model_index["entries"] = entries


def _cached_model_entries() -> "list | None":
    """The merged entries list when the index is fresh, else None (#648)."""
    with _model_index_lock:
        fresh = (time.time() - _model_index["ts"]) < _MODEL_INDEX_TTL_S
        entries = _model_index.get("entries")
    return list(entries) if (fresh and entries is not None) else None


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
        entries: list = []
        for p in _GATEWAY_PROVIDERS:
            for m in _fetch_provider_models(p, serving=serving):
                owner = mapping.setdefault(m["id"], p)
                if owner != p:
                    log.debug("gateway: model id %s on %s shadowed by %s",
                              m["id"], p, owner)
                else:
                    entries.append(m)
        _store_model_index(mapping, serving, entries)
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
            log.warning("gateway: model index refresh failed: %s", e)
        finally:
            with _model_index_cond:
                _model_index["refreshing"] = False
                _model_index_cond.notify_all()

    threading.Thread(target=_run, name="gw-model-index", daemon=True).start()


def _await_model_index(timeout: float) -> "tuple[dict, bool]":
    """Wait up to timeout seconds for an in-flight refresh to finish; returns
    (model → provider map, refresh still running)."""
    deadline = time.monotonic() + timeout
    with _model_index_cond:
        while _model_index["refreshing"]:
            left = deadline - time.monotonic()
            if left <= 0 or not _model_index_cond.wait(left):
                break
        return dict(_model_index["map"]), bool(_model_index["refreshing"])


def _provider_for_model(model_id) -> str:
    """Pin > cached model index > llama fallback (#493). A stale hit is served
    as-is; a miss waits a bounded time for the refresh before falling back."""
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
        hit = _model_index["map"].get(model_id)
    if hit:
        if not fresh:
            _refresh_model_index_async()
        return hit
    if not fresh:
        # Miss on a never-built or stale index: kick the refresh and wait a
        # bounded time for it to land before serving the llama fallback.
        _refresh_model_index_async()
        mapping, pending = _await_model_index(_MODEL_INDEX_WAIT_S)
        hit = mapping.get(model_id)
        if hit:
            return hit
        if pending:
            log.warning("gateway: model index refresh still running after "
                        "%.0fs; routing %s to llama", _MODEL_INDEX_WAIT_S,
                        model_id)
    return "llama"


def prewarm_model_index() -> None:
    """Kick the async index build at service startup so the first completion
    doesn't pay the full fan-out (#627)."""
    _refresh_model_index_async()


def _gateway_models(provider=None) -> Response:
    """One provider's models, or (provider=None) all pools merged. The merged
    path serves the cached index, refreshing it single-flight when stale."""
    if not _gw_enabled():
        return _oai_error("gateway disabled", 503, "disabled")
    if provider is None:
        cached = _cached_model_entries()
        if cached is None:
            _refresh_model_index()
            cached = _cached_model_entries()
        return jsonify({"object": "list", "data": cached or []})
    return jsonify({"object": "list", "data": _fetch_provider_models(provider)})


# ── Admin flow snapshot (#797) ───────────────────────────────────────

GATEWAY_ENDPOINT = "/api/gateway/v1"
_FLOW_CLIENT_LIMIT = 3


def _pool_agent_ids(provider: str) -> list:
    """Pool member ids for a provider, falling back to its default agent."""
    ids: list = []
    try:
        data = agent_registry.load_agents()
        ids = [a for a in ((data.get("global") or {}).get(f"{provider}_pool")
                           or []) if a]
    except Exception as e:
        _warn_pool_read_failed(e)
    did = agent_registry.default_agent_id_for(provider)
    if did and did not in ids:
        ids.append(did)
    return ids


def _current_model(provider: str, sample: dict) -> "str | None":
    """First resident model id on a host, per the autopilot placement rules."""
    try:
        import autopilot
        fn = autopilot._LOADED_BY_PROVIDER.get(provider)
    except Exception:
        fn = None
    if fn is None:
        return None
    try:
        loaded = fn(sample or {})
    except Exception:
        return None
    return loaded[0] if loaded else None


def _host_rates(provider: str, agent_id: str, sample: dict) -> "tuple[float, float]":
    """(gen_tps, prompt_tps): provider telemetry, or the gateway's own rates
    for providers that publish none."""
    block = (sample or {}).get(provider)
    if isinstance(block, dict):
        gen = block.get("tokens_per_second")
        pro = block.get("prompt_tokens_per_second")
        if isinstance(gen, (int, float)) or isinstance(pro, (int, float)):
            return (float(gen or 0.0), float(pro or 0.0))
    r = gateway_usage.last_rates(agent_id) or {}
    return (float(r.get("gen_tps") or 0.0), float(r.get("prompt_tps") or 0.0))


def _host_busy(provider: str, sample: dict) -> int:
    """Requests the host itself reports in progress, per provider telemetry."""
    s = sample or {}
    if provider == "llama":
        b = s.get("llama") or {}
        for k in ("requests_processing", "slots_processing", "active_slots"):
            if isinstance(b.get(k), (int, float)):
                return max(0, int(b[k]))
        return 0
    if provider == "vllm":
        rr = (s.get("vllm") or {}).get("requests_running")
        return max(0, int(rr)) if isinstance(rr, (int, float)) else 0
    if provider == "lms":
        return sum(1 for r in (s.get("ps") or [])
                   if any(m in str(r.get("status") or "").upper() for m in energy.LMS_BUSY_MARKERS))
    return 0


def _flow_hosts() -> "tuple[list, float, float, list]":
    """(host rows, gen_tps total, prompt_tps total, samples for power)."""
    rows, gen_total, pro_total, power_samples = [], 0.0, 0.0, []
    seen_power: set = set()
    for provider in _GATEWAY_PROVIDERS:
        primary = agent_registry.default_agent_id_for(provider)
        for aid in _pool_agent_ids(provider):
            agent = agent_registry.resolve_agent_by_id(aid) or {}
            wrap = provider_state.STORE.get(provider, aid) or {}
            sample = wrap.get("sample") if isinstance(wrap.get("sample"), dict) else {}
            gen, pro = _host_rates(provider, aid, sample)
            gen_total += gen
            pro_total += pro
            n_inflight = max(gateway_usage.inflight(aid), _host_busy(provider, sample))
            model = _current_model(provider, sample)
            rows.append({
                "agent_id": aid,
                "hostname": agent.get("hostname") or aid[:8],
                "provider": provider,
                "model": model,
                "gen_tps": round(gen, 2),
                "inflight": n_inflight,
                "primary": aid == primary,
                "state": "ok" if (n_inflight > 0 or gen > 0 or model) else "idle",
            })
            if sample and aid not in seen_power:
                seen_power.add(aid)
                power_samples.append(sample)
    return rows, gen_total, pro_total, power_samples


def _today_bounds(now: float) -> "tuple[int, int]":
    """UTC day containing `now`, snapped to the energy table's hour grid."""
    start = int(now // 86400) * 86400
    return start, int(now // 3600 + 1) * 3600


class _EnergyCtx:
    """Minimal ctx shim so energy._cfg_energy reads the live settings."""
    settings = settings


def _flow_energy(power_samples: list, now: float) -> dict:
    """serving_w from the live samples; today's kWh/cost/$ per Mtok from the
    energy rollup table."""
    serving_w = 0.0
    for sample in power_samples:
        watts, _src = energy.extract_power(sample)
        if watts is not None:
            serving_w += watts
    cfg = energy._cfg_energy(_EnergyCtx())
    out = {"serving_w": round(serving_w, 1), "kwh_today": None,
           "cost_today": None, "usd_per_mtok": None,
           "cloud_usd_per_mtok": cfg["cloud_price_out_per_mtok"]}
    factory = energy._conn_factory
    if factory is None:
        return out
    try:
        start, end = _today_bounds(now)
        rows = energy.query_rows(factory(), start, end)
        summary = energy.summarize(rows, max(0.0, now - start),
                                   cfg["price_kwh"],
                                   cfg["cloud_price_in_per_mtok"],
                                   cfg["cloud_price_out_per_mtok"])
    except Exception as e:
        log.debug("gateway flow: energy summary unavailable: %s", e)
        return out
    totals = summary.get("totals") or {}
    out["kwh_today"] = totals.get("kwh")
    out["cost_today"] = totals.get("cost_usd")
    out["usd_per_mtok"] = totals.get("usd_per_mtok")
    return out


def flow_payload(now: "float | None" = None) -> dict:
    """Live picture behind the Routing tab's Inference Gateway card."""
    t = time.time() if now is None else float(now)
    clients = gateway_usage.clients_snapshot(t)
    seen = {c["label"] for c in clients}
    for label, _secret in auth.gateway_key_entries():
        if label not in seen and len(clients) < _FLOW_CLIENT_LIMIT:
            clients.append({"label": label, "ip": None, "req_per_min": 0, "inflight": 0,
                            "prompt_tokens": 0, "gen_tokens": 0, "last_seen_s": None,
                            "state": "idle"})
    clients = clients[:_FLOW_CLIENT_LIMIT]
    hosts, gen_tps, prompt_tps, power_samples = _flow_hosts()
    totals = gateway_usage.client_totals(t)
    totals["prompt_tps"] = round(prompt_tps, 2)
    totals["gen_tps"] = round(gen_tps, 2)
    totals["inflight"] = sum(h["inflight"] for h in hosts)
    return {
        "ok": True,
        "enabled": _gw_enabled(),
        "endpoint": GATEWAY_ENDPOINT,
        "keys": len(auth.gateway_key_entries()),
        "usage_probe": bool(getattr(_gw_cfg(), "usage_probe", True)),
        "clients": clients,
        "hosts": hosts,
        "totals": totals,
        "energy": _flow_energy(power_samples, t),
    }


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
