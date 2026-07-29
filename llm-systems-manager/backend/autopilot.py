"""Fleet autopilot (#472): state, observer, executors, reconciler, routes."""
from __future__ import annotations

import logging
import sys
import threading
import time
import uuid
from dataclasses import asdict

import agent_registry  # type: ignore[import-not-found]  # sibling

log = logging.getLogger("llm-systems-manager.autopilot")

_PROVIDERS = ("llama", "vllm", "lms")
_AUTOSCALE_DEFAULTS = {"target_saturation": 0.75, "up_window_s": 120,
                       "down_window_s": 900}


def _default_state() -> dict:
    return {"enabled": False, "entries": [], "hosts": {}}


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
        mn = int(e.get("min_replicas", 1)); mx = int(e.get("max_replicas", mn))
        if mn < 1 or mx < mn:
            raise ValueError(f"bad replica range {mn}..{mx}")
        ne = {"model": model, "provider": prov,
              "placement": e.get("placement", "auto"), "failover": fo,
              "priority": int(e.get("priority", 100)),
              "min_replicas": mn, "max_replicas": mx}
        if mx > mn:
            ne["autoscale"] = {**_AUTOSCALE_DEFAULTS, **(e.get("autoscale") or {})}
        out["entries"].append(ne)
    for aid, pol in (raw.get("hosts") or {}).items():
        mins = int(pol.get("sleep_after_idle_min", 0))
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

    def tick(self, now: float) -> dict:
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
        return {"actions": [asdict(a) for a in actions],
                "proposals": self.proposals()}

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

    def proposals(self) -> "list[dict]":
        return sorted(self._proposals.values(), key=lambda p: p["created"])

    def apply(self, pid: str, now: float = None) -> dict:
        import time as _t
        p = self._proposals.pop(pid, None)
        if not p:
            raise KeyError(pid)
        from autopilot_planner import Action
        ok = self._run(Action(**p["action"]), now if now is not None
                       else _t.time())
        return {"ok": ok, "action": p["action"]}

    def dismiss(self, pid: str) -> None:
        self._proposals.pop(pid, None)


def _no_op_executor(action) -> bool:
    """Placeholder until Task 6 wires the real action executor; audits nothing."""
    return False


RECONCILER = Reconciler(get_state=get_state,
                        build_observed=lambda: build_observed(_prod_deps()),
                        executor=_no_op_executor)

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
                log.debug("reconciler tick failed: %s", e)
            time.sleep(_TICK_PERIOD_S)

    threading.Thread(target=_loop, name="autopilot-reconciler", daemon=True).start()
