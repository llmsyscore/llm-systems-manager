"""Model autopilot (#472): state, observer, executors, reconciler, routes."""
from __future__ import annotations

import logging
import re
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
import autopilot_planner as pl  # type: ignore[import-not-found]  # sibling
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
    entries = raw.get("entries") or []
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("each entry must be an object")
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
        sv = e.get("size_mb")
        if sv not in (None, ""):
            mb = _coerce_int(sv, "size_mb")
            if mb < 1:
                raise ValueError(f"size_mb must be >= 1, got {mb}")
            ne["size_mb"] = mb
        if mx > mn:
            asc = e.get("autoscale")
            if asc is not None and not isinstance(asc, dict):
                raise ValueError(f"bad autoscale {asc!r}")
            ne["autoscale"] = {**_AUTOSCALE_DEFAULTS, **(asc or {})}
        out["entries"].append(ne)
    # Any submitted hosts config is ignored (idle sleep is llama-server's
    # own --sleep-idle-seconds).
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


def _sample_ram(sample: dict) -> dict:
    # llama pushes ram flat at top level; vllm/lms nest it under "system".
    return sample.get("ram") or (sample.get("system") or {}).get("ram") or {}


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
        ram: dict = {}
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
            if not ram:
                ram = _sample_ram(sample)
            sat = deps["saturation"](prov, aid) or {}
            saturation[prov] = sat.get("value")
        total_mb = round((gpu.get("vram_total_bytes") or 0) / 1_048_576)
        used_mb = gpu.get("vram_used_mb") or 0
        # available_bytes (not total-used) already excludes reclaimable
        # cache/buffers — psutil's own definition of "free for new work".
        ram_free_mb = round((ram.get("available_bytes") or 0) / 1_048_576)
        liveness = deps["liveness"](agent)
        out[aid] = {
            "provider_caps": provider_caps,
            "live": liveness == "live",
            "liveness": liveness,
            "vram_total_mb": total_mb,
            "vram_free_mb": max(total_mb - used_mb, 0),
            "ram_free_mb": ram_free_mb,
            "loaded": loaded,
            "server_state": server_state,
            "saturation": saturation,
        }
    # Entry-declared size_mb overrides win over discovered sizes (#474).
    sizes = dict(deps["model_sizes"]() or {})
    ov = deps.get("size_overrides")
    sizes.update((ov() if ov else None) or {})
    glob = data.get("global") or {}
    route_pins = {p: dict(glob.get(sp.pin_dict_key) or {})
                  for p, sp in providers.PROVIDERS.items() if sp.pin_dict_key}
    return {"agents": out, "model_sizes_mb": sizes,
            "model_gpu_layers": deps["model_gpu_layers"]() or {},
            "route_pins": route_pins}


_SIZES_CACHE_TTL_S = 600.0
_sizes_cache: "dict[str, float | dict | bool]" = {"ts": 0.0, "sizes": {},
                                                  "layers": {}, "refreshing": False}
_sizes_lock = threading.Lock()


def _llama_agent_sizes_and_layers(agent: dict) -> "tuple[dict, dict]":
    """(size_mb, gpu_layers) per model_id from one agent's /llama/models/sizes;
    ({}, {}) on any failure (unreachable, old agent, bad JSON) — isolated."""
    r, _tried, err = agent_registry.agent_request(
        "GET", agent, "/llama/models/sizes",
        headers={"Authorization": f"Bearer {agent.get('token') or ''}"},
        timeout=5)
    if r is None or not r.ok:
        if err:
            log.debug("model-size fan-out: llama agent %s: %s", agent.get("hostname"), err)
        return {}, {}
    try:
        body = r.json() or {}
    except Exception:
        return {}, {}
    sizes: "dict[str, int]" = {}
    for mid, size_bytes in (body.get("sizes") or {}).items():
        try:
            mb = round(int(size_bytes) / 1_048_576)
        except (TypeError, ValueError):
            continue
        if mb:
            sizes[mid] = mb
    # meta is absent from old agents — a missing/malformed entry just means
    # gpu_layers unknown for that model, never a hard failure.
    layers: "dict[str, int | None]" = {}
    for mid, meta in (body.get("meta") or {}).items():
        if not isinstance(meta, dict):
            continue
        gl = meta.get("gpu_layers")
        layers[mid] = int(gl) if isinstance(gl, (int, float)) else None
    return sizes, layers


_LMS_SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGT]?B)$", re.IGNORECASE)
_LMS_SIZE_MULT = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def _parse_lms_size_mb(val) -> "int | None":
    """`lms ps --json` 'size' is raw bytes or a formatted string ('8.03 GB');
    best-effort either way, None (not a hard fail) when it's neither."""
    if isinstance(val, (int, float)) and val > 0:
        return round(val / 1_048_576) or None
    if not isinstance(val, str):
        return None
    m = _LMS_SIZE_RE.match(val.strip())
    if not m:
        return None
    try:
        b = float(m.group(1)) * _LMS_SIZE_MULT[m.group(2).upper()]
    except (ValueError, KeyError):
        return None
    return round(b / 1_048_576) or None


def _lms_agent_sizes(agents_map: dict) -> dict:
    """model_id -> size_mb from each lms agent's already-pushed 'ps' sample
    (provider_state.STORE) — no network I/O, so not part of the TTL cache."""
    import provider_state  # type: ignore[import-not-found]  # sibling
    out: "dict[str, int]" = {}
    for aid, agent in agents_map.items():
        if agent.get("status") != "approved":
            continue
        if not (agent.get("capabilities") or {}).get("lms"):
            continue
        snap = provider_state.STORE.get("lms", aid) or {}
        for row in (snap.get("sample") or {}).get("ps") or []:
            if not isinstance(row, dict):
                continue
            try:
                model = row.get("model")
                mb = _parse_lms_size_mb(row.get("size"))
            except Exception:
                continue
            if model and mb:
                out[model] = max(mb, out.get(model, 0))
    return out


def _refresh_model_sizes() -> "tuple[dict, dict]":
    """Fan out to every approved llama agent + merge pushed lms sizes: sizes
    take the agent-max per model, gpu_layers keeps the first value seen."""
    data = agent_registry.load_agents()
    agents_map = data.get("agents") or {}
    sizes_merged: "dict[str, int]" = {}
    layers_merged: "dict[str, int | None]" = {}
    for agent in agents_map.values():
        if agent.get("status") != "approved":
            continue
        if not (agent.get("capabilities") or {}).get("llama"):
            continue
        a_sizes, a_layers = _llama_agent_sizes_and_layers(agent)
        for mid, mb in a_sizes.items():
            key = f"llama:{mid}"
            sizes_merged[key] = max(mb, sizes_merged.get(key, 0))
        for mid, gl in a_layers.items():
            key = f"llama:{mid}"
            layers_merged.setdefault(key, gl)
    for model, mb in _lms_agent_sizes(agents_map).items():
        key = f"lms:{model}"
        sizes_merged[key] = max(mb, sizes_merged.get(key, 0))
    return sizes_merged, layers_merged


def _prod_model_sizes_and_layers() -> "tuple[dict, dict]":
    """Cache-refresh core shared by _prod_model_sizes/_prod_model_gpu_layers:
    one fan-out, TTL >=600s, in-flight guard, stale-on-failure serving."""
    now = time.time()
    with _sizes_lock:
        if now - _sizes_cache["ts"] < _SIZES_CACHE_TTL_S or _sizes_cache["refreshing"]:
            return dict(_sizes_cache["sizes"]), dict(_sizes_cache["layers"])
        _sizes_cache["refreshing"] = True
    try:
        sizes, layers = _refresh_model_sizes()
    except Exception as e:
        log.warning("model-size refresh failed, serving stale cache: %s", e)
        with _sizes_lock:
            # Advance ts even on failure — a bad refresh backs off for a full
            # TTL window instead of re-fanning-out on every subsequent call.
            _sizes_cache["ts"] = time.time()
            _sizes_cache["refreshing"] = False
        return dict(_sizes_cache["sizes"]), dict(_sizes_cache["layers"])
    with _sizes_lock:
        _sizes_cache["ts"] = time.time()
        _sizes_cache["sizes"] = sizes
        _sizes_cache["layers"] = layers
        _sizes_cache["refreshing"] = False
    return dict(sizes), dict(layers)


def _prod_model_sizes() -> dict:
    """Fleet-wide {provider}:{model} -> size_mb map; see
    _prod_model_sizes_and_layers for the TTL/in-flight/stale-cache design."""
    sizes, _layers = _prod_model_sizes_and_layers()
    return sizes


def _prod_model_gpu_layers() -> dict:
    """Fleet-wide {provider}:{model} -> gpu_layers (int|None); shares
    _prod_model_sizes()'s cache, so it never triggers a second fan-out."""
    _sizes, layers = _prod_model_sizes_and_layers()
    return layers


def _state_size_overrides() -> dict:
    """Entry-declared size_mb values from the autopilot state, keyed
    {provider}:{model} (#474); non-int/absent entries are skipped."""
    out: "dict[str, int]" = {}
    for e in get_state().get("entries") or []:
        mb = e.get("size_mb")
        if isinstance(mb, int) and mb > 0:
            out[f"{e['provider']}:{e['model']}"] = mb
    return out


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
        "model_gpu_layers": _prod_model_gpu_layers,
        "size_overrides": _state_size_overrides,
        "saturation": _prod_saturation,
    }


# ── Reconciler: observe -> plan -> execute/propose, with a ledger ────

_SAT_HISTORY_WINDOW_S = 1200.0  # 20 minutes


def route_sync_writes(desired: dict, observed: dict, glob: dict,
                      ledger: "dict | None" = None,
                      now: "float | None" = None) -> "list[tuple]":
    """Routing writes converging pins/pools to current placements
    (observed samples + fresh in-flight ledger placements):
    ("pin", provider, model, agent_id) and ("pool_add", provider, agent_id)."""
    writes: "list[tuple]" = []
    if not desired.get("enabled"):
        return writes
    for e in desired.get("entries") or []:
        prov, model = e["provider"], e["model"]
        multi = int(e.get("max_replicas", 1)) > 1
        placed = pl._effective_placements(e, pl._key(e), observed, ledger, now)
        if not placed:
            continue
        if multi:
            pool = glob.get(f"{prov}_pool") or []
            writes.extend(("pool_add", prov, aid) for aid in placed
                          if aid not in pool)
        spec = providers.get(prov)
        pin_key = getattr(spec, "pin_dict_key", None)
        if not pin_key:
            continue
        cur = (glob.get(pin_key) or {}).get(model)
        if multi and len(placed) > 1:
            # 2+ replicas serving: pool RR governs; a pin would defeat it.
            if cur is not None:
                writes.append(("pin", prov, model, None))
            continue
        # placed already contains live agents only.
        target = cur if cur in placed else placed[0]
        if cur != target:
            writes.append(("pin", prov, model, target))
    return writes


class Reconciler:
    def __init__(self, get_state, build_observed, executor, route_sync=None):
        self._get_state = get_state
        self._observe = build_observed
        self._exec = executor
        self._route_sync = route_sync
        self._proposals: "dict[str, dict]" = {}
        self._sat_history: "dict[str, list]" = {}
        self.ledger = {"last_action_ts": {}, "placed_at": {},
                       "in_flight_migrations": 0, "backoff_until": {}}
        # Coarse reentrant lock: serializes the background tick against
        # route-triggered apply/dismiss/tick calls (#472 Task 7).
        self._lock = threading.RLock()
        self.last_plan_ts: "float | None" = None
        # Lock-free read snapshots, reassigned whole after each mutation;
        # proposals()/ledger_view read these instead of taking self._lock.
        self._snapshot: "list[dict]" = []
        self.ledger_view: dict = {"last_action_ts": {}, "placed_at": {},
                                  "in_flight_migrations": 0, "backoff_until": {}}

    def tick(self, now: float) -> dict:
        with self._lock:
            desired = self._get_state()
            observed = self._observe()
            self._prune_placed_at(observed, now)
            self._prune_stale_keys(desired, observed)
            self._refresh_sat_history(desired, observed, now)
            actions = pl.plan(desired, observed, self.ledger, now)
            sig = lambda a: (a.kind, a.provider, a.model, a.agent_id)
            current = {sig(a) for a in actions}
            self._proposals = {pid: p for pid, p in self._proposals.items()
                               if tuple(p["sig"]) in current}
            for a in actions:
                if a.auto:
                    if self._run(a, now) and a.kind == "scale_down":
                        # Drop the unloaded copy from this tick's snapshot for route_sync.
                        loaded = observed["agents"].get(a.agent_id, {}).get("loaded") or {}
                        loaded[a.provider] = [m for m in (loaded.get(a.provider) or [])
                                              if m != a.model]
                elif not any(tuple(p["sig"]) == sig(a)
                             for p in self._proposals.values()):
                    pid = uuid.uuid4().hex[:12]
                    self._proposals[pid] = {"id": pid, "sig": list(sig(a)),
                                            "action": asdict(a), "created": now,
                                            "reason": a.reason}
            if self._route_sync:
                try:
                    self._route_sync(desired, observed, self.ledger, now)
                except Exception as e:
                    log.warning("autopilot route sync failed: %s", e)
            self.last_plan_ts = now
            self._refresh_snapshot()
            return {"actions": [asdict(a) for a in actions],
                    "proposals": self._snapshot,
                    "entry_status": pl.entry_status(desired, observed,
                                                    self.ledger, now)}

    def _prune_stale_keys(self, desired: dict, observed: dict) -> None:
        """Drop sat-history/ledger keys for entries and agents that no
        longer exist."""
        keys = {f"{e['model']}/{e['provider']}"
                for e in desired.get("entries") or []}
        for d in (self._sat_history, self.ledger["placed_at"],
                  self.ledger["backoff_until"]):
            for k in list(d):
                if k not in keys:
                    del d[k]
        for aid in list(self.ledger["last_action_ts"]):
            if aid not in observed["agents"]:
                del self.ledger["last_action_ts"][aid]

    def _prune_placed_at(self, observed: dict, now: float) -> None:
        """Drop placed_at[k][aid] past the PLACEMENT_FRESH_S grace window when
        a live agent no longer reports the model or the agent is unregistered;
        dead (registered, non-live) agents stay."""
        for k, amap in list(self.ledger["placed_at"].items()):
            model, _, provider = k.rpartition("/")
            for aid, ts in list(amap.items()):
                if now - ts < pl.PLACEMENT_FRESH_S:
                    continue
                agent = observed["agents"].get(aid)
                if agent is None:
                    del amap[aid]
                    continue
                if not agent["live"]:
                    continue
                if model not in (agent["loaded"].get(provider) or []):
                    del amap[aid]

    def _refresh_sat_history(self, desired: dict, observed: dict, now: float) -> None:
        """Append (now, max saturation across placed replicas) per entry,
        trimmed to the last 20 minutes, feeding evaluate_autoscale."""
        cutoff = now - _SAT_HISTORY_WINDOW_S
        for e in desired.get("entries") or []:
            k = f"{e['model']}/{e['provider']}"
            placed = pl._placements(e, observed)
            vals = [s for aid in placed
                    if (s := (observed["agents"][aid].get("saturation") or {})
                        .get(e["provider"])) is not None]
            # No live placement: the ring resets so stale points can't drive autoscale.
            hist = ([pt for pt in self._sat_history.get(k, []) if pt[0] >= cutoff]
                    if placed else [])
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
        elif action.kind == "scale_down":
            self.ledger["last_action_ts"][action.agent_id] = now
        else:
            self.ledger["backoff_until"][k] = now + 300.0
        return ok

    def _refresh_snapshot(self) -> None:
        """Publish fresh read snapshots; caller must already hold self._lock."""
        self._snapshot = sorted(self._proposals.values(), key=lambda p: p["created"])
        self.ledger_view = {
            "last_action_ts": dict(self.ledger["last_action_ts"]),
            "placed_at": {k: dict(v)
                          for k, v in self.ledger["placed_at"].items()},
            "in_flight_migrations": self.ledger["in_flight_migrations"],
            "backoff_until": dict(self.ledger["backoff_until"]),
        }

    def proposals(self) -> "list[dict]":
        return self._snapshot

    def observe(self) -> dict:
        """A fresh, read-only observation — used by GET to compute entry_status."""
        return self._observe()

    def apply(self, pid: str, now: float = None) -> dict:
        import time as _t
        with self._lock:
            p = self._proposals.pop(pid, None)
            if not p:
                raise KeyError(pid)
            ok = self._run(pl.Action(**p["action"]), now if now is not None
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
            get_pin = deps.get("get_pin")
            cur = get_pin(action.provider, action.model) if get_pin else action.agent_id
            if cur == action.agent_id:
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
            # Clear other models' pins pointing at this now-displaced host.
            spec = providers.get(provider)
            clear = deps.get("clear_host_pins")
            if getattr(spec, "single_resident", False) and clear:
                clear(provider, agent_id, model)
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
        if (action.kind in ("scale_down", "unload")
                and not getattr(providers.get(action.provider), "unloadable", True)):
            _audit(action, "unsupported")
            return False
        if action.kind == "sleep":
            # Idle sleep is llama-server's own --sleep-idle-seconds.
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


# Covers the agent's unload(30s) + settle(2s) + load(120s) worst case.
_LOAD_TIMEOUT_S = 180.0


def _make_prod_proxy(agent_id: str):
    """proxy dep bound to one action's target agent_id; load calls get the
    longer model-load timeout."""
    def _proxy(provider, method, path, json=None):
        to = _LOAD_TIMEOUT_S if path.endswith("/load") else 30
        r, body = _prod_agent_call(agent_id, method, path, json, timeout=to)
        return (r is not None and r.ok), body
    return _proxy


def _prod_set_pin(provider: str, model: str, agent_id: "str | None") -> None:
    """Mutate glob[<provider>_model_pins] via the same lock+save helper
    set_state() uses. Providers without a pin_dict_key are a no-op."""
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


def _prod_get_pin(provider: str, model: str) -> "str | None":
    """Current glob[<provider>_model_pins][model], None when unpinned."""
    spec = providers.get(provider)
    pin_key = getattr(spec, "pin_dict_key", None)
    if not pin_key:
        return None
    glob = agent_registry.load_agents().get("global") or {}
    return (glob.get(pin_key) or {}).get(model)


def _prod_pool_update(provider: str, agent_id: str, in_pool: bool) -> bool:
    ok, _err, _pool, _hostname = agent_registry.set_pool_membership(agent_id, provider, in_pool)
    return ok


def _prod_clear_host_pins(provider: str, agent_id: str, keep_model: str) -> None:
    """Remove <provider>_model_pins entries pointing at agent_id for any
    model other than keep_model."""
    spec = providers.get(provider)
    pin_key = getattr(spec, "pin_dict_key", None)
    if not pin_key:
        return
    with agent_registry.agents_lock:
        data = agent_registry.load_agents()
        glob = data.setdefault("global", {})
        pins = dict(glob.get(pin_key) or {})
        stale = [m for m, aid in pins.items()
                 if aid == agent_id and m != keep_model]
        if not stale:
            return
        for m in stale:
            del pins[m]
        glob[pin_key] = pins
        agent_registry.save_agents(data)
    log.info("autopilot: cleared %d stale %s pin(s) on %s after loading %s",
             len(stale), provider, agent_id[:8], keep_model)


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
        "get_pin": _prod_get_pin,
        "pool_update": _prod_pool_update,
        "clear_host_pins": _prod_clear_host_pins,
        "audit": _prod_audit,
        "vllm_svc": _prod_vllm_svc,
    }


def _prod_entries_by_key() -> dict:
    return {f"{e['model']}/{e['provider']}": e for e in (get_state().get("entries") or [])}


def _prod_executor(action) -> bool:
    """Builds deps fresh per action (proxy bound to action.agent_id) and
    reads entries_by_key live from get_state(), then dispatches."""
    return make_executor(_prod_executor_deps(action.agent_id), _prod_entries_by_key)(action)


def _prod_route_sync(desired: dict, observed: dict,
                     ledger: "dict | None" = None,
                     now: "float | None" = None) -> None:
    """Apply route_sync_writes via the same pin/pool helpers the executor
    uses; every write is logged."""
    glob = (agent_registry.load_agents().get("global") or {})
    for w in route_sync_writes(desired, observed, glob, ledger, now):
        if w[0] == "pin":
            _, prov, model, aid = w
            _prod_set_pin(prov, model, aid)
            log.info("autopilot route-sync: %s %s/%s%s",
                     "pinned" if aid else "unpinned", prov, model,
                     f" -> {aid[:8]}" if aid else " (2+ replicas, pool RR)")
        else:
            _, prov, aid = w
            _prod_pool_update(prov, aid, True)
            log.info("autopilot route-sync: added %s to %s pool", aid[:8], prov)


RECONCILER = Reconciler(get_state=get_state,
                        build_observed=lambda: build_observed(_prod_deps()),
                        executor=_prod_executor,
                        route_sync=_prod_route_sync)


# ── Routes: mount /api/autopilot* on the manager app ──────────────────

def register_routes(app, ctx, auth) -> None:
    """Mount /api/autopilot* on `app`, each route wrapped with `auth`."""
    from flask import jsonify, request as flask_request

    @app.route("/api/autopilot")
    @auth
    def autopilot_get():
        state = get_state()
        return jsonify({"state": state, "proposals": RECONCILER.proposals(),
                        "last_plan_ts": RECONCILER.last_plan_ts,
                        "entry_status": pl.entry_status(
                            state, RECONCILER.observe(),
                            RECONCILER.ledger_view, time.time())})

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
