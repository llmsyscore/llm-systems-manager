"""Interactive Discord bot (#471): slash commands over the raw gateway v10.
Opt-in via [manager.discord]; read commands + gated model control."""
from __future__ import annotations

import asyncio
import logging
import random
import sys
import threading as _threading
import time as _time
import uuid as _uuid

log = logging.getLogger("llm-systems-manager.discord_bot")

API_BASE = "https://discord.com/api/v10"
GATEWAY_QS = "?v=10&encoding=json"

EPHEMERAL = 64
CONFIRM_TTL_S = 60.0
LIST_CAP = 10
CONTROL_PROVIDERS = ("llama", "lms")

# Gateway close codes that no amount of retrying will fix.
FATAL_CLOSE_CODES = {4004, 4013, 4014}

SEVERITY_COLOR = {"critical": 0xFF0000, "warning": 0xFFF200, "info": 0x007400}
EMBED_COLOR = 0x5E6AD2


# ── Command schemas ──────────────────────────────────────────────────
# Option types: 3=string, 4=integer, 5=boolean.


def command_schemas() -> "list[dict]":
    host_opt = {"type": 3, "name": "host", "required": False,
                "description": "Hostname (defaults to each provider's default agent)"}
    public_opt = {"type": 5, "name": "public", "required": False,
                  "description": "Post visibly in the channel instead of ephemerally"}
    provider_opt = {"type": 3, "name": "provider", "required": False,
                    "description": "Provider (default llama)",
                    "choices": [{"name": "llama.cpp", "value": "llama"},
                                {"name": "LM Studio", "value": "lms"}]}
    return [
        {"name": "fleet", "type": 1,
         "description": "Fleet overview: hosts, providers, models, power",
         "options": [public_opt]},
        {"name": "host", "type": 1,
         "description": "One host's live telemetry",
         "options": [{"type": 3, "name": "name", "required": True,
                      "description": "Hostname as shown in /fleet"}]},
        {"name": "models", "type": 1,
         "description": "List models per provider",
         "options": [host_opt]},
        {"name": "load", "type": 1,
         "description": "Load a model (asks for confirmation)",
         "options": [{"type": 3, "name": "model", "required": True,
                      "description": "Model id as the provider reports it"},
                     provider_opt, host_opt]},
        {"name": "unload", "type": 1,
         "description": "Unload a model (asks for confirmation)",
         "options": [{"type": 3, "name": "model", "required": True,
                      "description": "Model id as the provider reports it"},
                     provider_opt, host_opt]},
        {"name": "alarms", "type": 1,
         "description": "Recent alarms from the alarm engine",
         "options": [{"type": 4, "name": "count", "required": False,
                      "description": "How many (default 5, max 20)"},
                     public_opt]},
        {"name": "ack", "type": 1,
         "description": "Acknowledge an alert (stops repeat notifications)",
         "options": [{"type": 3, "name": "alert_id", "required": True,
                      "description": "Alert id from /alarms"}]},
        {"name": "silence", "type": 1,
         "description": "Close an alert; the rule re-fires on a new breach",
         "options": [{"type": 3, "name": "alert_id", "required": True,
                      "description": "Alert id from /alarms"}]},
    ]


# ── Pending confirmations ────────────────────────────────────────────


class PendingActions:
    """TTL store for /load-/unload confirmations, keyed by button nonce."""

    def __init__(self, ttl_s: float = CONFIRM_TTL_S):
        self._ttl = ttl_s
        self._lock = _threading.Lock()
        self._items: dict = {}

    def put(self, nonce: str, job: dict, user_id: str, now: float) -> None:
        with self._lock:
            self._items[nonce] = {"job": job, "user_id": user_id,
                                  "expires": now + self._ttl}
            for k in [k for k, v in self._items.items()
                      if v["expires"] <= now]:
                self._items.pop(k, None)

    def peek(self, nonce: str, now: float) -> "dict | None":
        with self._lock:
            item = self._items.get(nonce)
            if not item or item["expires"] <= now:
                self._items.pop(nonce, None)
                return None
            return dict(item)

    def pop(self, nonce: str, now: float) -> "dict | None":
        with self._lock:
            item = self._items.pop(nonce, None)
            if not item or item["expires"] <= now:
                return None
            return dict(item)


# ── Routing (pure) ───────────────────────────────────────────────────
# Decisions: respond (immediate payload) or defer (ack, then run the job).


def _msg(content: str, flags: int = EPHEMERAL, components=None) -> dict:
    data: dict = {"content": content, "flags": flags}
    if components is not None:
        data["components"] = components
    return {"type": 4, "data": data}


def _refuse(content: str) -> dict:
    return {"kind": "respond", "payload": _msg(content)}


def _user_id(ix: dict) -> str:
    member = ix.get("member") or {}
    user = member.get("user") or ix.get("user") or {}
    return str(user.get("id") or "")


def _options(ix: dict) -> dict:
    return {o.get("name"): o.get("value")
            for o in (ix.get("data") or {}).get("options") or []}


def _confirm_components(nonce: str) -> list:
    return [{"type": 1, "components": [
        {"type": 2, "style": 4, "label": "Confirm",
         "custom_id": f"confirm:{nonce}"},
        {"type": 2, "style": 2, "label": "Cancel",
         "custom_id": f"cancel:{nonce}"},
    ]}]


def route(ix: dict, cfg: dict, pending: PendingActions,
          now: "float | None" = None, nonce_fn=None) -> dict:
    """One interaction → a routing decision. Pure given injected time/nonce."""
    now = _time.time() if now is None else now
    if ix.get("type") == 1:
        return {"kind": "respond", "payload": {"type": 1}}

    allow = [str(u) for u in cfg.get("allowed_user_ids") or []]
    uid = _user_id(ix)
    if not allow:
        return _refuse("No Discord users are allowlisted — set "
                       "[manager.discord] allowed_user_ids on the manager.")
    if uid not in allow:
        return _refuse("You are not on this bot's allowlist.")

    if ix.get("type") == 3:
        return _route_component(ix, uid, pending, now)
    if ix.get("type") != 2:
        return _refuse("Unsupported interaction.")

    name = (ix.get("data") or {}).get("name") or ""
    opts = _options(ix)
    public_flags = 0 if opts.get("public") else EPHEMERAL

    if name == "fleet":
        return {"kind": "defer", "flags": public_flags, "update": False,
                "job": {"kind": "fleet"}}
    if name == "host":
        return {"kind": "defer", "flags": EPHEMERAL, "update": False,
                "job": {"kind": "host", "name": str(opts.get("name") or "")}}
    if name == "models":
        return {"kind": "defer", "flags": EPHEMERAL, "update": False,
                "job": {"kind": "models",
                        "host": str(opts.get("host") or "") or None}}
    if name == "alarms":
        try:
            count = max(1, min(int(opts.get("count") or 5), 20))
        except (TypeError, ValueError):
            count = 5
        return {"kind": "defer", "flags": public_flags, "update": False,
                "job": {"kind": "alarms", "count": count}}
    if name in ("ack", "silence"):
        return {"kind": "defer", "flags": EPHEMERAL, "update": False,
                "job": {"kind": name,
                        "alert_id": str(opts.get("alert_id") or "")}}
    if name in ("load", "unload"):
        if not cfg.get("allow_model_control"):
            return _refuse("Model control is disabled — set "
                           "[manager.discord] allow_model_control = true.")
        provider = str(opts.get("provider") or "llama")
        if provider not in CONTROL_PROVIDERS:
            return _refuse(f"{provider} is read-only from the bot.")
        job = {"kind": name, "provider": provider,
               "model": str(opts.get("model") or ""),
               "host": str(opts.get("host") or "") or None}
        nonce = (nonce_fn or (lambda: _uuid.uuid4().hex))()
        pending.put(nonce, job, uid, now)
        where = job["host"] or f"the default {provider} host"
        return {"kind": "respond", "payload": _msg(
            f"{name.capitalize()} `{job['model']}` ({provider}) on "
            f"**{where}**?", components=_confirm_components(nonce))}
    return _refuse(f"Unknown command: {name}")


def _route_component(ix: dict, uid: str, pending: PendingActions,
                     now: float) -> dict:
    cid = str((ix.get("data") or {}).get("custom_id") or "")
    action, _, nonce = cid.partition(":")
    item = pending.peek(nonce, now)
    if item is None:
        return {"kind": "respond", "payload": {
            "type": 7, "data": {"content": "Expired — run the command again.",
                                "components": []}}}
    if item["user_id"] != uid:
        return _refuse("Only the requesting user can confirm this action.")
    if action == "cancel":
        pending.pop(nonce, now)
        return {"kind": "respond", "payload": {
            "type": 7, "data": {"content": "Cancelled.", "components": []}}}
    if action == "confirm":
        pending.pop(nonce, now)
        return {"kind": "defer", "flags": EPHEMERAL, "update": True,
                "job": {"kind": "execute", "job": item["job"]}}
    return _refuse("Unknown action.")


# ── Job execution + formatting (pure given deps) ─────────────────────


def _cap(lines: "list[str]") -> "list[str]":
    if len(lines) > LIST_CAP:
        return lines[:LIST_CAP] + [f"… +{len(lines) - LIST_CAP} more"]
    return lines


def _fmt_age(age_s) -> str:
    if age_s is None:
        return "never"
    age_s = float(age_s)
    if age_s < 90:
        return f"{age_s:.0f}s"
    if age_s < 5400:
        return f"{age_s / 60:.0f}m"
    return f"{age_s / 3600:.1f}h"


def _fleet_embed(hosts: "list[dict]") -> dict:
    if not hosts:
        return {"title": "Fleet", "color": EMBED_COLOR,
                "description": "No approved agents yet."}
    lines = []
    for h in sorted(hosts, key=lambda h: str(h.get("hostname") or "")):
        dot = "🟢" if h.get("online") else "🔴"
        bits = [f"{dot} **{h.get('hostname') or '?'}**",
                "/".join(h.get("providers") or []) or "no providers"]
        if h.get("model"):
            bits.append(f"`{h['model']}`" + (" ⚡" if h.get("busy") else ""))
        if h.get("watts") is not None:
            bits.append(f"{h['watts']:.0f} W")
        if not h.get("online"):
            bits.append(f"last seen {_fmt_age(h.get('age_s'))} ago")
        lines.append(" · ".join(bits))
    return {"title": "Fleet", "color": EMBED_COLOR,
            "description": "\n".join(_cap(lines))}


def _host_embed(detail: "dict | None", name: str) -> dict:
    if not detail:
        return {"title": name, "color": EMBED_COLOR,
                "description": "Unknown host — check /fleet for names."}
    fields = []
    for label, key, unit in (("CPU", "cpu_pct", "%"), ("RAM", "ram_pct", "%"),
                             ("GPU", "gpu_pct", "%"),
                             ("GPU temp", "gpu_temp_c", "°C"),
                             ("Power", "watts", " W")):
        v = detail.get(key)
        if v is not None:
            fields.append({"name": label, "value": f"{float(v):.0f}{unit}",
                           "inline": True})
    for prov, state in (detail.get("provider_states") or {}).items():
        fields.append({"name": prov, "value": state or "—", "inline": True})
    return {"title": detail.get("hostname") or name, "color": EMBED_COLOR,
            "description": "online" if detail.get("online")
                           else f"offline · last seen {_fmt_age(detail.get('age_s'))} ago",
            "fields": fields[:25]}


def _alarms_embed(alerts: "list[dict]", count: int) -> dict:
    if not alerts:
        return {"title": "Alarms", "color": EMBED_COLOR,
                "description": "No recent alarms."}
    lines = []
    for a in alerts[:count]:
        sev = str(a.get("severity") or "info")
        icon = {"critical": "🟥", "warning": "🟨"}.get(sev, "🟦")
        status = str(a.get("status") or "?")
        host = a.get("hostname") or a.get("metric_source") or "?"
        lines.append(f"{icon} [{status}] **{a.get('rule_name') or 'alert'}** — "
                     f"{host} · `{a.get('alert_id') or '?'}`")
    color = SEVERITY_COLOR.get(str(alerts[0].get("severity") or ""),
                               EMBED_COLOR)
    return {"title": f"Last {min(count, len(alerts))} alarms", "color": color,
            "description": "\n".join(_cap(lines)),
            "footer": {"text": "/ack <alert_id> · /silence <alert_id>"}}


def run_job(job: dict, deps: dict) -> dict:
    """Deferred work → follow-up message data. Details stay in the log."""
    try:
        return _run_job(job, deps)
    except Exception as e:
        log.warning("discord job %s failed: %s", job.get("kind"), e)
        return {"content": "Failed — check the manager log for details."}


def _run_job(job: dict, deps: dict) -> dict:
    kind = job.get("kind")
    if kind == "execute":
        return _run_job(job.get("job") or {}, deps)
    if kind == "fleet":
        return {"embeds": [_fleet_embed(deps["fleet"]())]}
    if kind == "host":
        return {"embeds": [_host_embed(deps["host"](job["name"]),
                                       job["name"])]}
    if kind == "models":
        rows = deps["models"](job.get("host"))
        if not rows:
            return {"content": "No models reported — is the host online?"}
        lines = []
        for r in rows:
            mark = "▶ " if r.get("loaded") else ""
            lines.append(f"{mark}`{r.get('model')}` — {r.get('provider')}"
                         f" @ {r.get('hostname') or 'default'}")
        return {"content": "\n".join(_cap(lines))}
    if kind == "alarms":
        alerts = deps["alarms"](job.get("count") or 5)
        return {"embeds": [_alarms_embed(alerts, job.get("count") or 5)]}
    if kind == "ack":
        ok, err = deps["ack"](job.get("alert_id") or "")
        return {"content": f"Acknowledged `{job.get('alert_id')}`." if ok
                else f"Ack failed: {err}"}
    if kind == "silence":
        ok, err = deps["close"](job.get("alert_id") or "")
        return {"content": (f"Closed `{job.get('alert_id')}` — the rule "
                            "re-fires on a new breach.") if ok
                else f"Close failed: {err}"}
    if kind in ("load", "unload"):
        ok, err = deps[kind](job.get("provider"), job.get("host"),
                             job.get("model"))
        verb = "Loaded" if kind == "load" else "Unloaded"
        return {"content": f"{verb} `{job.get('model')}`." if ok
                else f"{kind} failed: {err}"}
    return {"content": f"Unknown job: {kind}"}


# ── Production deps ──────────────────────────────────────────────────


def _freshest(buckets: dict) -> "tuple[dict, float]":
    """(sample, last_seen) of the newest bucket; ({}, 0) when empty."""
    best, best_ls = {}, 0.0
    for sample, ls in buckets.values():
        if ls >= best_ls:
            best, best_ls = sample, ls
    return best, best_ls


def _agent_call(agent: dict, method: str, path: str, timeout=180,
                **kw) -> "tuple[bool, str | None]":
    """Agent call as (ok, error); expected failures arrive as HTTP 200
    {"ok": false} (same convention report_card documents)."""
    import agent_registry
    r, _tried, err = agent_registry.agent_request(
        method, agent, path,
        headers={"Authorization": f"Bearer {agent.get('token') or ''}"},
        timeout=timeout, **kw)
    if r is None or not r.ok:
        return False, str(err or getattr(r, "status_code", "no response"))
    try:
        body = r.json()
    except ValueError:
        return True, None
    if isinstance(body, dict) and body.get("ok") is False:
        return False, str(body.get("error") or "provider reported failure")
    return True, None


def _agent_json(agent: dict, path: str, timeout=15):
    import agent_registry
    r, _tried, _err = agent_registry.agent_request(
        "GET", agent, path,
        headers={"Authorization": f"Bearer {agent.get('token') or ''}"},
        timeout=timeout)
    if r is None or not r.ok:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def prod_deps(ctx) -> dict:
    """Live data callables for run_job, bound to STORE/registry/AE."""
    import agent_registry
    import energy
    import provider_state
    import providers as providers_mod

    def _agents() -> dict:
        return (agent_registry.load_agents().get("agents") or {})

    def _store_view() -> dict:
        return energy.store_view_from_provider_state()

    def _cap_key(provider: "str | None") -> "str | None":
        if not provider:
            return None
        spec = providers_mod.get(provider)
        return spec.capability_key if spec else provider

    def _agent_by_host(name: "str | None", provider: "str | None" = None):
        agents = _agents()
        cap = _cap_key(provider)
        if name:
            low = name.strip().lower()
            for aid, a in agents.items():
                if a.get("status") != "approved":
                    continue
                if str(a.get("hostname") or "").lower() != low:
                    continue
                # A named host must still advertise the provider capability.
                if cap and not (a.get("capabilities") or {}).get(cap):
                    return None
                return dict(a, agent_id=aid)
            return None
        if provider:
            aid = agent_registry.default_agent_id_for(provider)
            if aid and aid in agents:
                return dict(agents[aid], agent_id=aid)
        return None

    def _bucket_sample(buckets: dict, provider: str) -> dict:
        return (buckets.get(provider) or ({}, 0.0))[0]

    def fleet() -> "list[dict]":
        agents = _agents()
        view = _store_view()
        now = _time.time()
        out = []
        for aid, a in agents.items():
            if a.get("status") != "approved":
                continue
            buckets = view.get(aid) or {}
            # Power comes from the freshest bucket that reports it; the
            # provider-specific blocks are read from their own buckets.
            ordered = sorted(buckets.values(), key=lambda t: t[1],
                             reverse=True)
            watts = None
            for s, _ls in ordered:
                watts, _src = energy.extract_power(s)
                if watts is not None:
                    break
            _sample, last_seen = _freshest(buckets)
            model = None
            ll = _bucket_sample(buckets, "llama").get("llama") or {}
            if ll.get("model"):
                model = ll["model"]
            for row in _bucket_sample(buckets, "lms").get("ps") or []:
                if isinstance(row, dict) and row.get("identifier"):
                    model = model or row.get("identifier")
            caps = a.get("capabilities") or {}
            provs = [p for p in providers_mod.names()
                     if caps.get(_cap_key(p))]
            out.append({
                "hostname": a.get("hostname"),
                "providers": provs,
                "online": bool(last_seen) and (now - last_seen) < 90,
                "age_s": (now - last_seen) if last_seen else None,
                "model": model,
                "busy": any(energy.extract_busy(s)
                            for s, _ls in buckets.values()),
                "watts": watts,
            })
        return out

    def host(name: str) -> "dict | None":
        agent = _agent_by_host(name)
        if not agent:
            return None
        buckets = (_store_view().get(agent["agent_id"]) or {})
        sample, last_seen = _freshest(buckets)
        now = _time.time()
        sysb = energy._sys_block(sample)
        gpu = sysb.get("gpu") or {}
        watts = None
        for s, _ls in sorted(buckets.values(), key=lambda t: t[1],
                             reverse=True):
            watts, _src = energy.extract_power(s)
            if watts is not None:
                break
        states = {}
        for prov, (s, _ls) in buckets.items():
            if prov == "llama":
                states["llama.cpp"] = str(((s.get("llama") or {}).get("state"))
                                          or "?")
            if prov == "lms":
                on = ((s.get("server") or {}).get("on"))
                states["LM Studio"] = "running" if on else "stopped"
            if prov == "vllm":
                states["vLLM"] = str((s.get("vllm") or {}).get("state")
                                     or "?")
        return {
            "hostname": agent.get("hostname"),
            "online": bool(last_seen) and (now - last_seen) < 90,
            "age_s": (now - last_seen) if last_seen else None,
            "cpu_pct": sysb.get("cpu_total"),
            "ram_pct": (sysb.get("ram") or {}).get("percent"),
            "gpu_pct": gpu.get("gpu_util_percent"),
            "gpu_temp_c": gpu.get("temperature_c"),
            "watts": watts,
            "provider_states": states,
        }

    def models(hostname: "str | None") -> "list[dict]":
        out = []
        for prov in CONTROL_PROVIDERS + ("vllm",):
            agent = _agent_by_host(hostname, provider=prov)
            if not agent or not (agent.get("capabilities") or {}).get(
                    _cap_key(prov)):
                continue
            data = (_agent_json(agent, f"/{prov}/models") or {}).get("data")
            for m in data or []:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                st = m.get("status")
                loaded = (st.get("value") in ("loaded", "sleeping")
                          if isinstance(st, dict) else False)
                out.append({"model": str(m["id"]), "provider": prov,
                            "hostname": agent.get("hostname"),
                            "loaded": loaded})
        return out

    def _control(kind: str):
        def call(provider, hostname, model) -> "tuple[bool, str | None]":
            agent = _agent_by_host(hostname, provider=provider)
            if not agent:
                return False, (f"no approved {provider} agent"
                               + (f" named {hostname}" if hostname else ""))
            return _agent_call(agent, "POST", f"/{provider}/{kind}",
                               json={"model": model})
        return call

    def _ae(method: str, path: str, timeout=10):
        # ctx.alarm_engine_url is a Callable[[], str] getter (app_context).
        getter = getattr(ctx, "alarm_engine_url", None)
        base = str((getter() if callable(getter) else getter) or "").rstrip("/")
        if not base:
            raise RuntimeError("alarm engine URL not configured")
        session = getattr(ctx, "ae_session", None)
        fn = getattr(session, method.lower())
        return fn(f"{base}{path}", timeout=timeout)

    def alarms(count: int) -> "list[dict]":
        r = _ae("GET", f"/api/alarm/alerts/?limit={int(count)}"
                       "&include_closed=true")
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, list) else []

    def _alert_action(action: str):
        def call(alert_id: str) -> "tuple[bool, str | None]":
            if not alert_id:
                return False, "alert_id required"
            try:
                import urllib.parse
                r = _ae("POST", "/api/alarm/alerts/"
                        f"{urllib.parse.quote(alert_id, safe='')}/{action}")
            except Exception as e:
                log.warning("discord: alert %s failed: %s", action, e)
                return False, "alarm engine unreachable"
            if r.ok:
                return True, None
            if r.status_code == 404:
                return False, "alert not found"
            return False, f"HTTP {r.status_code}"
        return call

    return {"fleet": fleet, "host": host, "models": models,
            "load": _control("load"), "unload": _control("unload"),
            "alarms": alarms, "ack": _alert_action("acknowledge"),
            "close": _alert_action("close")}


# ── Gateway client ───────────────────────────────────────────────────


class GatewayBot:
    """Owns the ws connection, heartbeats, and interaction dispatch."""

    def __init__(self, cfg: dict, deps: dict):
        self.cfg = cfg
        self.deps = deps
        self.pending = PendingActions()
        self.app_id = None
        self._session = None
        self._seq = None
        self._session_id = None
        self._resume_url = None
        self._ack = True
        # Strong refs so fire-and-forget dispatch tasks aren't GC'd mid-run.
        self._tasks: set = set()

    def _headers(self) -> dict:
        return {"Authorization": f"Bot {self.cfg['bot_token']}"}

    async def _rest(self, method: str, path: str, json_body=None):
        async with self._session.request(
                method, f"{API_BASE}{path}", json=json_body,
                headers=self._headers()) as r:
            body = None
            try:
                body = await r.json()
            except Exception:
                pass
            return r.status, body

    async def _register(self) -> bool:
        status, app = await self._rest("GET", "/applications/@me")
        if status != 200 or not isinstance(app, dict) or not app.get("id"):
            log.error("discord: token rejected fetching application (%s)",
                      status)
            return False
        self.app_id = str(app["id"])
        gid = str(self.cfg.get("guild_id") or "").strip()
        path = (f"/applications/{self.app_id}/guilds/{gid}/commands" if gid
                else f"/applications/{self.app_id}/commands")
        status, body = await self._rest("PUT", path, command_schemas())
        if status != 200:
            log.error("discord: command registration failed (%s): %s",
                      status, str(body)[:200])
            return False
        scope = f"guild {gid}" if gid else "global (may take up to 1h)"
        log.info("discord: registered %d slash commands for %s",
                 len(command_schemas()), scope)
        return True

    async def _respond(self, ix: dict, payload: dict) -> None:
        status, body = await self._rest(
            "POST", f"/interactions/{ix['id']}/{ix['token']}/callback",
            payload)
        if status not in (200, 204):
            log.warning("discord: interaction callback %s: %s", status,
                        str(body)[:150])

    async def _followup_edit(self, ix: dict, data: dict) -> None:
        status, body = await self._rest(
            "PATCH",
            f"/webhooks/{self.app_id}/{ix['token']}/messages/@original",
            data)
        if status != 200:
            log.warning("discord: followup edit %s: %s", status,
                        str(body)[:150])

    async def _handle_interaction(self, ix: dict) -> None:
        try:
            decision = route(ix, self.cfg, self.pending)
            if decision["kind"] == "respond":
                await self._respond(ix, decision["payload"])
                return
            defer_type = 6 if decision.get("update") else 5
            await self._respond(ix, {"type": defer_type,
                                     "data": {"flags": decision["flags"]}})
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None, run_job, decision["job"], self.deps)
            if decision.get("update"):
                data = dict(data, components=[])
            await self._followup_edit(ix, data)
        except Exception as e:
            log.warning("discord: interaction handling failed: %s",
                        str(e)[:150])

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _heartbeat_loop(self, ws, interval_s: float) -> None:
        await asyncio.sleep(interval_s * random.random())
        while True:
            if not self._ack:
                await ws.close(code=4000)
                return
            self._ack = False
            await ws.send_json({"op": 1, "d": self._seq})
            await asyncio.sleep(interval_s)

    async def _run_once(self) -> "bool":
        """One gateway session; returns False on a fatal (no-retry) error."""
        import aiohttp
        url = self._resume_url
        if not url:
            status, body = await self._rest("GET", "/gateway/bot")
            if status == 401:
                log.error("discord: bot token rejected — check "
                          "[manager.discord] bot_token")
                return False
            url = (body or {}).get("url") or "wss://gateway.discord.gg"
        hb_task = None
        try:
            async with self._session.ws_connect(
                    url + GATEWAY_QS, max_msg_size=8 * 1024 * 1024,
                    heartbeat=None) as ws:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    ev = msg.json()
                    op = ev.get("op")
                    if op == 10:
                        interval = float(ev["d"]["heartbeat_interval"]) / 1000
                        self._ack = True
                        hb_task = asyncio.create_task(
                            self._heartbeat_loop(ws, interval))
                        if self._session_id:
                            await ws.send_json({"op": 6, "d": {
                                "token": self.cfg["bot_token"],
                                "session_id": self._session_id,
                                "seq": self._seq}})
                        else:
                            await ws.send_json({"op": 2, "d": {
                                "token": self.cfg["bot_token"],
                                "intents": 0,
                                "properties": {"os": "linux",
                                               "browser": "llm-systems-manager",
                                               "device": "llm-systems-manager"}}})
                    elif op == 0:
                        self._seq = ev.get("s") or self._seq
                        t = ev.get("t")
                        if t == "READY":
                            self._session_id = ev["d"].get("session_id")
                            self._resume_url = ev["d"].get(
                                "resume_gateway_url")
                            log.info("discord: gateway ready as %s",
                                     ((ev["d"].get("user") or {})
                                      .get("username") or "?"))
                        elif t == "RESUMED":
                            log.info("discord: gateway session resumed")
                        elif t == "INTERACTION_CREATE":
                            self._spawn(self._handle_interaction(ev["d"]))
                    elif op == 1:
                        await ws.send_json({"op": 1, "d": self._seq})
                    elif op == 7:
                        break
                    elif op == 9:
                        if not ev.get("d"):
                            self._session_id = None
                            self._resume_url = None
                        await asyncio.sleep(1 + random.random() * 4)
                        break
                    elif op == 11:
                        self._ack = True
                code = ws.close_code
                if code in FATAL_CLOSE_CODES:
                    log.error("discord: gateway refused connection "
                              "(close %s) — not retrying", code)
                    return False
        finally:
            if hb_task:
                hb_task.cancel()
        return True

    async def run(self) -> None:
        import aiohttp
        self._session = aiohttp.ClientSession()
        try:
            if not await self._register():
                return
            backoff = 1.0
            while True:
                started = _time.monotonic()
                try:
                    if not await self._run_once():
                        return
                except Exception as e:
                    log.warning("discord: gateway error: %s", str(e)[:150])
                    self._resume_url = None
                if _time.monotonic() - started > 120:
                    backoff = 1.0
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        finally:
            await self._session.close()


# ── Config + thread ──────────────────────────────────────────────────


def bot_config(ctx) -> dict:
    """[manager.discord] with getattr guards for older unified_config."""
    manager = getattr(getattr(ctx, "settings", None), "manager", None)
    d = getattr(manager, "discord", None)
    ids = getattr(d, "allowed_user_ids", None) or []
    return {
        "enabled": bool(getattr(d, "enabled", False)),
        "bot_token": str(getattr(d, "bot_token", "") or ""),
        "guild_id": str(getattr(d, "guild_id", "") or ""),
        "allowed_user_ids": [str(u) for u in ids],
        "allow_model_control": bool(getattr(d, "allow_model_control", False)),
    }


def start_thread(ctx=None) -> None:
    """Daemon gateway thread when [manager.discord] is enabled; pytest no-op."""
    if "pytest" in sys.modules:
        return
    cfg = bot_config(ctx)
    if not cfg["enabled"]:
        return
    if not cfg["bot_token"]:
        log.warning("discord: enabled but bot_token is empty — not starting")
        return

    def _main():
        try:
            asyncio.run(GatewayBot(cfg, prod_deps(ctx)).run())
        except Exception as e:
            log.error("discord: bot thread exited: %s", e)

    _threading.Thread(target=_main, name="discord-bot", daemon=True).start()
    log.info("discord: bot thread started (control=%s, allowlist=%d users)",
             cfg["allow_model_control"], len(cfg["allowed_user_ids"]))
