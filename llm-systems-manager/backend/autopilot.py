"""Fleet autopilot (#472): state, observer, executors, reconciler, routes."""
from __future__ import annotations

import logging
import shlex
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import agent_registry  # type: ignore[import-not-found]  # sibling
import providers        # type: ignore[import-not-found]  # sibling

log = logging.getLogger("llm-systems-manager.autopilot")

_PROVIDERS = ("llama", "vllm", "lms")
_AUTOSCALE_DEFAULTS = {"target_saturation": 0.75, "up_window_s": 120,
                       "down_window_s": 900}


def _default_state() -> dict:
    return {"enabled": False, "entries": [], "hosts": {}}


def _coerce_int(val, field: str) -> int:
    """int(val) but non-numeric input (list/dict/str-garbage) raises
    ValueError naming the field, not a TypeError the PUT route can't catch."""
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ValueError(f"bad {field} {val!r}")


def _validate_placement(val) -> str:
    """"auto" or a non-empty, non-structured-data string (agent id)."""
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"bad placement {val!r}")
    return val.strip()


def validate_state(raw: dict) -> dict:
    out = {"enabled": bool(raw.get("enabled")), "entries": [], "hosts": {}}
    seen = set()
    for e in raw.get("entries") or []:
        model = (e.get("model") or "").strip()
        prov = e.get("provider")
        if not model:
            raise ValueError("entry missing model")
        if prov not in _PROVIDERS:
            raise ValueError(f"unknown provider {prov!r}")
        if (model, prov) in seen:
            raise ValueError(f"duplicate entry {model}/{prov}")
        seen.add((model, prov))
        fo = e.get("failover", "semi")
        if fo not in ("semi", "auto"):
            raise ValueError(f"bad failover {fo!r}")
        mn = _coerce_int(e.get("min_replicas", 1), "min_replicas")
        mx = _coerce_int(e.get("max_replicas", mn), "max_replicas")
        if mn < 1 or mx < mn:
            raise ValueError(f"bad replica range {mn}..{mx}")
        ne = {"model": model, "provider": prov,
              "placement": _validate_placement(e.get("placement", "auto")),
              "failover": fo,
              "priority": _coerce_int(e.get("priority", 100), "priority"),
              "min_replicas": mn, "max_replicas": mx}
        if mx > mn:
            ne["autoscale"] = {**_AUTOSCALE_DEFAULTS, **(e.get("autoscale") or {})}
        out["entries"].append(ne)
    for aid, pol in (raw.get("hosts") or {}).items():
        mins = _coerce_int(pol.get("sleep_after_idle_min", 0), "sleep_after_idle_min")
        if mins < 0:
            raise ValueError("sleep_after_idle_min must be >= 0")
        out["hosts"][aid] = {"sleep_after_idle_min": mins}
    return out


def get_state() -> dict:
    data = agent_registry.load_agents()
    return (data.get("global") or {}).get("autopilot") or _default_state()


def set_state(state: dict) -> None:
    with agent_registry.agents_lock:
        data = agent_registry.load_agents()
        data.setdefault("global", {})["autopilot"] = state
        agent_registry.save_agents(data)


# ── Observer: agents.json + provider samples -> plan() contract ──────

def _clean_llama_model(raw) -> "str | None":
    """Strip llama's ' (sleeping)' display suffix; keep the bare model id."""
    if not isinstance(raw, str):
        return None
    return raw.replace(" (sleeping)", "").strip() or None


def _llama_loaded(sample: dict) -> "list[str]":
    """A sleeping model is still resident (counts as placed); '(unloaded)'
    or unknown state means nothing is really loaded."""
    llama = sample.get("llama") or {}
    if llama.get("state") not in ("awake", "sleeping"):
        return []
    raw = llama.get("model")
    if isinstance(raw, str) and raw.endswith(" (unloaded)"):
        return []
    m = _clean_llama_model(raw)
    return [m] if m else []


def _vllm_loaded(sample: dict) -> "list[str]":
    v = sample.get("vllm") or {}
    m = v.get("model")
    return [m] if v.get("state") == "running" and m else []


def _lms_loaded(sample: dict) -> "list[str]":
    # ps = loaded instances (per lms_get_ps); STOPPED rows are unloaded.
    ps = sample.get("ps") or []
    return [p.get("model") for p in ps
            if p.get("model") and str(p.get("status") or "").upper() != "STOPPED"]


_LOADED_BY_PROVIDER = {"llama": _llama_loaded, "vllm": _vllm_loaded, "lms": _lms_loaded}


def _sample_gpu(sample: dict) -> dict:
    # llama pushes gpu flat at top level; vllm/lms nest it under "system".
    return sample.get("gpu") or (sample.get("system") or {}).get("gpu") or {}


def build_observed(deps: dict) -> dict:
    """Snapshot agents.json + per-provider STORE samples into the agent
    dict shape autopilot_planner.plan() consumes."""
    data = deps["agents"]() or {}
    agents_map = data.get("agents") or {}
    out: "dict[str, dict]" = {}
    for aid, agent in agents_map.items():
        caps = agent.get("capabilities") or {}
        provider_caps = [p for p in _PROVIDERS if caps.get(p)]
        loaded: "dict[str, list]" = {}
        saturation: "dict[str, float | None]" = {}
        server_state = None
        gpu: dict = {}
        for prov in provider_caps:
            snap = deps["provider_snapshot"](prov, aid) or {}
            sample = snap.get("sample") or {}
            loaded[prov] = _LOADED_BY_PROVIDER[prov](sample)
            if prov == "llama":
                st = (sample.get("llama") or {}).get("state")
                if st in ("awake", "sleeping"):
                    server_state = st
            if not gpu:
                gpu = _sample_gpu(sample)
            sat = deps["saturation"](prov, aid) or {}
            saturation[prov] = sat.get("value")
        total_mb = round((gpu.get("vram_total_bytes") or 0) / 1_048_576)
        used_mb = gpu.get("vram_used_mb") or 0
        out[aid] = {
            "provider_caps": provider_caps,
            "live": deps["liveness"](agent) == "live",
            "vram_total_mb": total_mb,
            "vram_free_mb": max(total_mb - used_mb, 0),
            "loaded": loaded,
            "server_state": server_state,
            # No per-model idle timestamp is pushed by any provider today.
            "idle_since": None,
            "saturation": saturation,
        }
    return {"agents": out, "model_sizes_mb": deps["model_sizes"]() or {}}


def _prod_model_sizes() -> dict:
    """No fleet-wide model-size catalog exists yet; an empty map makes
    every size-gated fit check conservatively fail."""
    return {}


def _prod_saturation(provider: str, agent_id: str) -> dict:
    """Best-effort 0..1 saturation from whatever the provider already
    reports; {"value": None} where no comparable signal exists."""
    import provider_state  # type: ignore[import-not-found]  # sibling
    snap = provider_state.STORE.get(provider, agent_id) or {}
    sample = snap.get("sample") or {}
    if provider == "llama":
        ratio = (sample.get("llama") or {}).get("kv_cache_usage_ratio")
        return {"value": float(ratio) if isinstance(ratio, (int, float)) else None}
    if provider == "vllm":
        pct = (sample.get("vllm") or {}).get("kv_cache_usage_pct")
        return {"value": float(pct) / 100.0 if isinstance(pct, (int, float)) else None}
    return {"value": None}  # lms has no comparable saturation signal today


def _prod_deps() -> dict:
    import provider_state  # type: ignore[import-not-found]  # sibling
    return {
        "agents": agent_registry.load_agents,
        "liveness": agent_registry.agent_liveness,
        "provider_snapshot": provider_state.STORE.get,
        "model_sizes": _prod_model_sizes,
        "saturation": _prod_saturation,
    }


# ── Reconciler: observe -> plan -> execute/propose, with a ledger ────

_SAT_HISTORY_WINDOW_S = 1200.0  # 20 minutes


class Reconciler:
    def __init__(self, get_state, build_observed, executor):
        self._get_state = get_state
        self._observe = build_observed
        self._exec = executor
        self._proposals: "dict[str, dict]" = {}
        self._sat_history: "dict[str, list]" = {}
        self.ledger = {"last_action_ts": {}, "placed_at": {},
                       "in_flight_migrations": 0, "backoff_until": {}}
        # Coarse reentrant lock: serializes the background tick against
        # route-triggered apply/dismiss/tick calls (#472 Task 7).
        self._lock = threading.RLock()
        self.last_plan_ts: "float | None" = None
        # Lock-free read snapshot, reassigned whole after each mutation;
        # proposals() reads this instead of taking self._lock.
        self._snapshot: "list[dict]" = []

    def tick(self, now: float) -> dict:
        with self._lock:
            import autopilot_planner as pl
            desired = self._get_state()
            observed = self._observe()
            self._prune_placed_at(observed, now)
            self._refresh_sat_history(desired, observed, now)
            actions = pl.plan(desired, observed, self.ledger, now)
            sig = lambda a: (a.kind, a.provider, a.model, a.agent_id)
            current = {sig(a) for a in actions}
            self._proposals = {pid: p for pid, p in self._proposals.items()
                               if tuple(p["sig"]) in current}
            for a in actions:
                if a.auto:
                    self._run(a, now)
                elif not any(tuple(p["sig"]) == sig(a)
                             for p in self._proposals.values()):
                    pid = uuid.uuid4().hex[:12]
                    self._proposals[pid] = {"id": pid, "sig": list(sig(a)),
                                            "action": asdict(a), "created": now,
                                            "reason": a.reason}
            self.last_plan_ts = now
            self._refresh_snapshot()
            return {"actions": [asdict(a) for a in actions],
                    "proposals": self._snapshot}

    def _prune_placed_at(self, observed: dict, now: float) -> None:
        """Drop placed_at[k][aid] once a live agent stops reporting the
        model loaded, past a COOLDOWN_S grace window; dead agents stay."""
        import autopilot_planner as pl
        for k, amap in list(self.ledger["placed_at"].items()):
            model, _, provider = k.rpartition("/")
            for aid, ts in list(amap.items()):
                if now - ts < pl.COOLDOWN_S:
                    continue
                agent = observed["agents"].get(aid)
                if agent is None or not agent["live"]:
                    continue
                if model not in (agent["loaded"].get(provider) or []):
                    del amap[aid]

    def _refresh_sat_history(self, desired: dict, observed: dict, now: float) -> None:
        """Append (now, max saturation across placed replicas) per entry,
        trimmed to the last 20 minutes, feeding evaluate_autoscale."""
        cutoff = now - _SAT_HISTORY_WINDOW_S
        for e in desired.get("entries") or []:
            k = f"{e['model']}/{e['provider']}"
            placed = [aid for aid, a in observed["agents"].items()
                      if e["model"] in (a["loaded"].get(e["provider"]) or [])]
            vals = [s for aid in placed
                    if (s := (observed["agents"][aid].get("saturation") or {})
                        .get(e["provider"])) is not None]
            hist = [pt for pt in self._sat_history.get(k, []) if pt[0] >= cutoff]
            if vals:
                hist.append((now, max(vals)))
            self._sat_history[k] = hist
        observed["sat_history"] = self._sat_history

    def _run(self, action, now: float):
        # Brackets only failover-class (reason startswith "failover:") actions.
        is_migration = action.reason.startswith("failover:")
        if is_migration:
            self.ledger["in_flight_migrations"] += 1
        try:
            ok = self._exec(action)
        finally:
            if is_migration:
                self.ledger["in_flight_migrations"] = max(
                    0, self.ledger["in_flight_migrations"] - 1)
        k = action.entry_key
        if ok:
            self.ledger["last_action_ts"][action.agent_id] = now
            if action.kind in ("load", "migrate", "scale_up"):
                self.ledger["placed_at"].setdefault(k, {})[action.agent_id] = now
            if action.kind == "scale_down":
                self.ledger["placed_at"].get(k, {}).pop(action.agent_id, None)
        else:
            self.ledger["backoff_until"][k] = now + 300.0
        return ok

    def _refresh_snapshot(self) -> None:
        """Publish a fresh read snapshot; caller must already hold self._lock."""
        self._snapshot = sorted(self._proposals.values(), key=lambda p: p["created"])

    def proposals(self) -> "list[dict]":
        return self._snapshot

    def apply(self, pid: str, now: float = None) -> dict:
        import time as _t
        with self._lock:
            p = self._proposals.pop(pid, None)
            if not p:
                raise KeyError(pid)
            from autopilot_planner import Action
            ok = self._run(Action(**p["action"]), now if now is not None
                           else _t.time())
            self._refresh_snapshot()
            return {"ok": ok, "action": p["action"]}

    def dismiss(self, pid: str) -> None:
        with self._lock:
            self._proposals.pop(pid, None)
            self._refresh_snapshot()


# ── Executors: dispatch a planned Action to provider-specific side effects ──

def make_executor(deps: dict, entries_by_key):
    """Build an executor(Action) -> bool per the #472 behavior matrix.
    entries_by_key: dict[entry_key, entry] snapshot, or a zero-arg callable."""

    def _entries() -> dict:
        eb = entries_by_key() if callable(entries_by_key) else entries_by_key
        return eb or {}

    def _audit(action, outcome: str) -> None:
        deps["audit"](f"autopilot:{action.kind}",
                      f"{action.model}@{action.agent_id[:8]}", outcome)

    def _route_replica(action, in_pool: bool) -> None:
        entry = _entries().get(action.entry_key) or {}
        if int(entry.get("max_replicas", 1)) > 1:
            deps["pool_update"](action.provider, action.agent_id, in_pool)
        elif in_pool:
            deps["set_pin"](action.provider, action.model, action.agent_id)
        else:
            deps["set_pin"](action.provider, action.model, None)

    def _load(action) -> bool:
        provider, model, agent_id = action.provider, action.model, action.agent_id
        if provider in ("llama", "lms"):
            ok, _body = deps["proxy"](provider, "POST", f"/{provider}/load", {"model": model})
        elif provider == "vllm":
            ok = deps["vllm_svc"](agent_id, model)
        else:
            ok = False
        if ok:
            _route_replica(action, True)
        return ok

    def _unload(action) -> bool:
        ok, _body = deps["proxy"](action.provider, "POST",
                                  f"/{action.provider}/unload", {"model": action.model})
        if ok:
            _route_replica(action, False)
        return ok

    def execute(action) -> bool:
        if action.kind == "download":
            _audit(action, "refused")
            return False
        if action.provider == "vllm" and action.auto:
            _audit(action, "refused")
            return False
        if action.kind == "sleep":
            # No /llama/server/sleep agent route exists (only .../wake).
            _audit(action, "unsupported")
            return False

        try:
            if action.kind in ("load", "scale_up"):
                ok = _load(action)
            elif action.kind in ("scale_down", "unload"):
                ok = _unload(action)
            elif action.kind == "wake":
                ok, _body = deps["proxy"](action.provider, "POST", "/llama/server/wake", {})
            else:
                ok = False
        except Exception as e:
            log.debug("autopilot executor raised on %s: %s", action.kind, e)
            _audit(action, "error")
            return False

        _audit(action, "ok" if ok else "fail")
        return ok

    return execute


# ── Production deps: wire make_executor's callables to real agent I/O ──────

_METRICS_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "metrics.db"


def _prod_agent_call(agent_id: str, method: str, path: str,
                     json_body: "dict | None" = None, timeout: float = 30):
    """One HTTP hop to a specific agent id; headers match proxy_to_primary.
    Returns (Response|None, body dict)."""
    agent = agent_registry.resolve_agent_by_id(agent_id)
    if not agent or not agent.get("token"):
        return None, {}
    headers = {"Authorization": f"Bearer {agent['token']}"}
    r, _tried, _err = agent_registry.agent_request(
        method, agent, path, headers=headers, timeout=timeout, json=json_body)
    if r is None:
        return None, {}
    try:
        body = r.json()
    except Exception:
        body = {}
    return r, body


def _make_prod_proxy(agent_id: str):
    """proxy dep bound to one action's target agent_id."""
    def _proxy(provider, method, path, json=None):
        r, body = _prod_agent_call(agent_id, method, path, json)
        return (r is not None and r.ok), body
    return _proxy


def _prod_set_pin(provider: str, model: str, agent_id: "str | None") -> None:
    """Mutate glob[<provider>_model_pins] via the same lock+save helper
    set_state() uses. Providers without a pin_dict_key (lms) are a no-op."""
    spec = providers.get(provider)
    pin_key = getattr(spec, "pin_dict_key", None)
    if not pin_key:
        return
    with agent_registry.agents_lock:
        data = agent_registry.load_agents()
        glob = data.setdefault("global", {})
        pins = dict(glob.get(pin_key) or {})
        if agent_id:
            pins[model] = agent_id
        else:
            pins.pop(model, None)
        glob[pin_key] = pins
        agent_registry.save_agents(data)


def _prod_pool_update(provider: str, agent_id: str, in_pool: bool) -> bool:
    ok, _err, _pool, _hostname = agent_registry.set_pool_membership(agent_id, provider, in_pool)
    return ok


def _vllm_rewrite_model(cur: dict, model: str) -> "dict | None":
    """Replace the ExecStart positional model (head_tokens[2], vllm.py's
    autotune convention); args pass through. None if the head is too short."""
    head_tokens = shlex.split(cur.get("binary", "") or "")
    if len(head_tokens) <= 2:
        return None
    head_tokens[2] = model
    return {"binary": " ".join(head_tokens), "args": cur.get("args") or []}


def _prod_vllm_svc(agent_id: str, model: str) -> bool:
    """Rewrite the ExecStart positional model + restart in the same POST —
    vLLM has no --model flag override; the positional is authoritative."""
    r, cur = _prod_agent_call(agent_id, "GET", "/vllm/server/svcconfig")
    if r is None or not r.ok or not cur.get("ok"):
        return False
    rewritten = _vllm_rewrite_model(cur, model)
    if rewritten is None:
        return False
    body = {**rewritten, "restart": True}
    r2, _body2 = _prod_agent_call(agent_id, "POST", "/vllm/server/svcconfig", body, timeout=60)
    return r2 is not None and r2.ok


# Bounds audit_log growth from this module's own inserts (separate connection).
_AUDIT_MAX_ROWS = 10000
_AUDIT_PRUNE_EVERY = 200


def _prod_audit(action_str: str, target: str, outcome: str) -> None:
    """INSERT into the #217 audit_log table; write failures are logged,
    never raised."""
    try:
        conn = sqlite3.connect(str(_METRICS_DB_PATH), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            cur = conn.execute(
                "INSERT INTO audit_log (ts, actor, role, ip, method, path, action, target, status, outcome)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "autopilot", "system", "-", "", "", action_str, target, None, outcome))
            if cur.lastrowid and cur.lastrowid % _AUDIT_PRUNE_EVERY == 0:
                conn.execute(
                    "DELETE FROM audit_log WHERE id <= (SELECT MAX(id) FROM audit_log) - ?",
                    (_AUDIT_MAX_ROWS,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.debug("autopilot audit write failed: %s", e)


def _prod_executor_deps(agent_id: str) -> dict:
    return {
        "proxy": _make_prod_proxy(agent_id),
        "set_pin": _prod_set_pin,
        "pool_update": _prod_pool_update,
        "audit": _prod_audit,
        "vllm_svc": _prod_vllm_svc,
    }


def _prod_entries_by_key() -> dict:
    return {f"{e['model']}/{e['provider']}": e for e in (get_state().get("entries") or [])}


def _prod_executor(action) -> bool:
    """Builds deps fresh per action (proxy bound to action.agent_id) and
    reads entries_by_key live from get_state(), then dispatches."""
    return make_executor(_prod_executor_deps(action.agent_id), _prod_entries_by_key)(action)


RECONCILER = Reconciler(get_state=get_state,
                        build_observed=lambda: build_observed(_prod_deps()),
                        executor=_prod_executor)


# ── Routes: mount /api/autopilot* on the manager app ──────────────────

def register_routes(app, ctx, auth) -> None:
    """Mount /api/autopilot* on `app`, each route wrapped with `auth`."""
    from flask import jsonify, request as flask_request

    @app.route("/api/autopilot")
    @auth
    def autopilot_get():
        return jsonify({"state": get_state(), "proposals": RECONCILER.proposals(),
                        "last_plan_ts": RECONCILER.last_plan_ts})

    @app.route("/api/autopilot", methods=["PUT"])
    @auth
    def autopilot_put():
        body = flask_request.get_json(silent=True) or {}
        try:
            state = validate_state(body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        set_state(state)
        return jsonify({"ok": True, "state": state})

    @app.route("/api/autopilot/proposals/<pid>/apply", methods=["POST"])
    @auth
    def autopilot_apply(pid):
        try:
            result = RECONCILER.apply(pid)
        except KeyError:
            return jsonify({"error": "unknown proposal"}), 404
        return jsonify(result)

    @app.route("/api/autopilot/proposals/<pid>/dismiss", methods=["POST"])
    @auth
    def autopilot_dismiss(pid):
        RECONCILER.dismiss(pid)
        return jsonify({"ok": True})

    @app.route("/api/autopilot/tick", methods=["POST"])
    @auth
    def autopilot_tick():
        return jsonify(RECONCILER.tick(time.time()))


_TICK_PERIOD_S = 30


def start_thread(ctx=None) -> None:
    """Daemon thread ticking RECONCILER every 30s; exceptions are logged,
    never raised. ctx is reserved for future wiring; unused for now."""
    if "pytest" in sys.modules:
        return

    def _loop():
        while True:
            try:
                RECONCILER.tick(time.time())
            except Exception as e:
                log.warning("reconciler tick failed: %s", e)
            time.sleep(_TICK_PERIOD_S)

    threading.Thread(target=_loop, name="autopilot-reconciler", daemon=True).start()
