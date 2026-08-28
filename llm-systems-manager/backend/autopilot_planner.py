"""#472 pure planner: state in -> actions out. No I/O, no clocks."""
from __future__ import annotations
from dataclasses import dataclass

import providers  # type: ignore[import-not-found]  # sibling

COOLDOWN_S = 120
DWELL_S = 600
# In-flight window: ledger placements this recent count as placed. Must
# exceed the executor's load timeout (autopilot._LOAD_TIMEOUT_S = 180).
PLACEMENT_FRESH_S = 240
VRAM_HEADROOM_MB = 1024
# Providers whose load displaces the resident model (ProviderSpec.single_resident).
SINGLE_RESIDENT_PROVIDERS = providers.single_resident_names()

@dataclass(frozen=True)
class Action:
    kind: str
    provider: str
    model: str
    agent_id: str
    reason: str
    auto: bool
    entry_key: str

def _key(e) -> str:
    return f"{e['model']}/{e['provider']}"

def _reporting(a) -> bool:
    """Live or merely stale (heartbeat lag) agents; down/pending/disabled are not."""
    return bool(a["live"]) or a.get("liveness") == "stale"

def _placements(entry, observed) -> "list[str]":
    """Reporting agents whose last sample shows the model loaded; a down
    agent's frozen sample never counts as a placement."""
    return [aid for aid, a in observed["agents"].items()
            if _reporting(a) and entry["model"] in (a["loaded"].get(entry["provider"]) or [])]

def _fresh_placed(k, ledger, now) -> "list[str]":
    # Ledger placements younger than PLACEMENT_FRESH_S: loads issued but
    # not yet visible in observed samples (model still loading).
    if not ledger or now is None:
        return []
    return [aid for aid, ts in ((ledger.get("placed_at") or {}).get(k) or {}).items()
            if now - ts < PLACEMENT_FRESH_S]

def _effective_placements(entry, k, observed, ledger, now) -> "list[str]":
    placed = _placements(entry, observed)
    for aid in _fresh_placed(k, ledger, now):
        if aid not in placed and aid in observed["agents"] and _reporting(observed["agents"][aid]):
            placed.append(aid)
    return placed

def _residents(desired, observed, ledger, now) -> dict:
    """(provider, aid) -> resident model on single-resident hosts, seeded
    from observed loads plus fresh in-flight ledger placements."""
    res: dict = {}
    for aid, a in observed["agents"].items():
        if not _reporting(a):
            continue
        for prov in SINGLE_RESIDENT_PROVIDERS:
            models = a["loaded"].get(prov) or []
            if models:
                res[(prov, aid)] = models[0]
    for e in desired.get("entries") or []:
        if e["provider"] not in SINGLE_RESIDENT_PROVIDERS:
            continue
        for aid in _fresh_placed(_key(e), ledger, now):
            a = observed["agents"].get(aid)
            if a is not None and _reporting(a):
                res.setdefault((e["provider"], aid), e["model"])
    return res

def _resident_conflict(e, aid, residents, managed) -> bool:
    """True when placing e on aid would displace another managed model
    on a single-resident host."""
    if e["provider"] not in SINGLE_RESIDENT_PROVIDERS:
        return False
    cur = residents.get((e["provider"], aid))
    return (cur is not None and cur != e["model"]
            and (e["provider"], cur) in managed)

def _uses_ram_budget(entry, observed, aid=None) -> bool:
    # RAM budget for llama's CPU-served models (gpu_layers==0) and for any
    # candidate reporting no GPU at all (vram_total_mb==0 — unified memory).
    if aid is not None:
        a = observed["agents"].get(aid)
        if a and not a.get("vram_total_mb"):
            return True
    if entry["provider"] != "llama":
        return False
    gl = observed.get("model_gpu_layers", {}).get(
        f"{entry['provider']}:{entry['model']}")
    return gl == 0

def _fits(entry, aid, observed) -> bool:
    a = observed["agents"][aid]
    # live is the agent heartbeat, not the llama server's cached sleep state.
    if entry["provider"] not in a["provider_caps"] or not a["live"]:
        return False
    size = observed.get("model_sizes_mb", {}).get(
        f"{entry['provider']}:{entry['model']}")
    if size is None:
        return False
    avail = (a.get("ram_free_mb", 0) if _uses_ram_budget(entry, observed, aid)
             else a["vram_free_mb"])
    return avail >= size + VRAM_HEADROOM_MB

def _dwell_blocks(k, placed_at, observed, now) -> bool:
    # Recent placement on a now-dead agent is a liveness blip, not a failure.
    for aid, ts in (placed_at.get(k) or {}).items():
        a = observed["agents"].get(aid)
        if (a is None or not a["live"]) and now - ts < DWELL_S:
            return True
    return False

def _has_dead_candidate(entry, candidates, observed) -> bool:
    # A dead, capable candidate means any placement here is a failover.
    for aid in candidates:
        a = observed["agents"].get(aid)
        if a and entry["provider"] in a["provider_caps"] and not a["live"]:
            return True
    return False

def _may_auto(entry, desired, provider) -> bool:
    if not desired.get("enabled"):
        return False
    if provider == "vllm":
        return False            # vLLM never auto (spec)
    return entry["failover"] == "auto"

def evaluate_autoscale(entry, placements, sat_history, now: float) -> str:
    cfg = entry.get("autoscale")
    if not cfg or not sat_history:
        return "hold"
    target = cfg["target_saturation"]
    up_w, down_w = cfg["up_window_s"], cfg["down_window_s"]
    def window(w):
        pts = [s for t, s in sat_history if t >= now - w]
        return pts
    up_pts = window(up_w)
    if (up_pts and all(s > target for s in up_pts)
            and sat_history[0][0] <= now - up_w
            and len(placements) < entry["max_replicas"]):
        return "up"
    down_pts = window(down_w)
    if (down_pts and all(s < target * 0.5 for s in down_pts)
            and sat_history[0][0] <= now - down_w
            and len(placements) > entry["min_replicas"]):
        return "down"
    return "hold"

def _fit_and_size(e, aid, a, free, free_ram, observed):
    # Same capability/budget fit check used by both the min-replica and
    # autoscale passes; budget picked per (entry, candidate) — see _uses_ram_budget.
    size = observed.get("model_sizes_mb", {}).get(
        f"{e['provider']}:{e['model']}")
    budget = free_ram if _uses_ram_budget(e, observed, aid) else free
    if e["placement"] != "auto" and aid == e["placement"]:
        fit = e["provider"] in a["provider_caps"] and \
              size is not None and budget.get(aid, 0) >= size + VRAM_HEADROOM_MB \
              and a["live"]
    else:
        fit = _fits(e, aid, observed) and \
              budget.get(aid, 0) >= (size or 0) + VRAM_HEADROOM_MB
    return fit, size

def _live_capable(entry, candidates, observed) -> "list[str]":
    out = []
    for aid in candidates:
        a = observed["agents"].get(aid)
        if a and entry["provider"] in a["provider_caps"] and a["live"]:
            out.append(aid)
    return out

def entry_status(desired: dict, observed: dict,
                 ledger: "dict | None" = None,
                 now: "float | None" = None) -> dict:
    """Per-entry {placed, want, blocked}; shares plan()'s priority order +
    intra-pass RAM/VRAM budgets (via _fit_and_size) so the two can't disagree."""
    out: "dict[str, dict]" = {}
    free = {aid: a["vram_free_mb"] for aid, a in observed["agents"].items()}
    free_ram = {aid: a.get("ram_free_mb", 0) for aid, a in observed["agents"].items()}
    entries = sorted(desired.get("entries") or [], key=lambda e: e["priority"])
    residents = _residents(desired, observed, ledger, now)
    managed = {(e["provider"], e["model"]) for e in entries}
    for e in entries:
        k = _key(e)
        placed = _effective_placements(e, k, observed, ledger, now)
        want = e["min_replicas"]
        need = want - len(placed)
        blocked = None
        if need > 0:
            candidates = ([e["placement"]] if e["placement"] != "auto"
                          else list(observed["agents"].keys()))
            candidates = [aid for aid in candidates if aid not in placed]
            live_capable = _live_capable(e, candidates, observed)
            if not live_capable:
                blocked = "no live agent supports this provider"
            else:
                size = observed.get("model_sizes_mb", {}).get(
                    f"{e['provider']}:{e['model']}")
                if size is None and not placed:
                    blocked = "model size unknown (set entry size MB)"
                else:
                    placeable = 0
                    resident_blocked = 0
                    for aid in live_capable:
                        if placeable >= need:
                            break
                        if _resident_conflict(e, aid, residents, managed):
                            resident_blocked += 1
                            continue
                        fit, sz = _fit_and_size(e, aid, observed["agents"][aid],
                                                free, free_ram, observed)
                        if fit:
                            budget = (free_ram if _uses_ram_budget(e, observed, aid)
                                      else free)
                            budget[aid] -= sz or 0
                            if e["provider"] in SINGLE_RESIDENT_PROVIDERS:
                                residents[(e["provider"], aid)] = e["model"]
                            placeable += 1
                    if placeable < need:
                        if size is None:
                            blocked = "model size unknown (set entry size MB)"
                        elif placeable == 0 and resident_blocked:
                            blocked = (f"capable hosts already serve another "
                                       f"managed {e['provider']} model")
                        else:
                            best = max(
                                ((free_ram if _uses_ram_budget(e, observed, aid)
                                  else free).get(aid, 0) for aid in live_capable),
                                default=0)
                            blocked = (
                                f"insufficient free VRAM/RAM (need "
                                f"{size + VRAM_HEADROOM_MB} MB incl. "
                                f"{VRAM_HEADROOM_MB} MB headroom; best candidate has "
                                f"{best} MB free)")
        out[k] = {"placed": len(placed), "want": want, "blocked": blocked}
    return out

def plan(desired: dict, observed: dict, ledger: dict, now: float) -> "list[Action]":
    actions: "list[Action]" = []
    # Track hypothetical VRAM/RAM commitments within this plan pass, separately.
    free = {aid: a["vram_free_mb"] for aid, a in observed["agents"].items()}
    free_ram = {aid: a.get("ram_free_mb", 0) for aid, a in observed["agents"].items()}
    last_action_ts = ledger.get("last_action_ts") or {}
    placed_at = ledger.get("placed_at") or {}
    in_flight = ledger.get("in_flight_migrations") or 0
    migrations_this_pass = 0             # failover loads queued so far in this pass
    touched: "set[str]" = set()          # agents with an action this pass
    entries = sorted(desired.get("entries") or [], key=lambda e: e["priority"])
    residents = _residents(desired, observed, ledger, now)
    managed = {(e["provider"], e["model"]) for e in entries}
    for e in entries:
        k = _key(e)
        if now < (ledger.get("backoff_until") or {}).get(k, 0):
            continue
        placed = _effective_placements(e, k, observed, ledger, now)
        want = e["min_replicas"]
        if len(placed) >= want:
            continue
        candidates = ([e["placement"]] if e["placement"] != "auto"
                      else list(observed["agents"].keys()))
        if _dwell_blocks(k, placed_at, observed, now):
            continue
        is_failover = _has_dead_candidate(e, candidates, observed)
        for aid in candidates:
            if len(placed) >= want:
                break
            if aid in placed or aid not in observed["agents"]:
                continue
            if now - last_action_ts.get(aid, 0) < COOLDOWN_S:
                continue
            # Global cap: at most one failover-class load queued per pass.
            if is_failover and in_flight + migrations_this_pass >= 1:
                continue
            if _resident_conflict(e, aid, residents, managed):
                continue
            a = observed["agents"][aid]
            fit, size = _fit_and_size(e, aid, a, free, free_ram, observed)
            if not fit:
                continue
            (free_ram if _uses_ram_budget(e, observed, aid) else free)[aid] -= size or 0
            if e["provider"] in SINGLE_RESIDENT_PROVIDERS:
                residents[(e["provider"], aid)] = e["model"]
            placed.append(aid)
            auto = _may_auto(e, desired, e["provider"])
            reason = (f"failover: {k} recovering onto {aid}" if is_failover
                      else f"{k}: {len(placed)}/{want} replicas placed")
            if e["provider"] == "llama" and a["server_state"] == "sleeping":
                actions.append(Action(
                    kind="wake", provider=e["provider"], model=e["model"],
                    agent_id=aid, reason=f"{k}: waking {aid} for placement",
                    auto=auto, entry_key=k))
            actions.append(Action(
                kind="load", provider=e["provider"], model=e["model"],
                agent_id=aid, reason=reason, auto=auto, entry_key=k))
            touched.add(aid)
            if is_failover:
                migrations_this_pass += 1
    # Autoscale pass: only entries already at/above min_replicas with headroom to scale.
    for e in entries:
        k = _key(e)
        if e["max_replicas"] <= e["min_replicas"]:
            continue
        if now < (ledger.get("backoff_until") or {}).get(k, 0):
            continue
        placed = _effective_placements(e, k, observed, ledger, now)
        if len(placed) < e["min_replicas"]:
            continue
        hist = (observed.get("sat_history") or {}).get(k, [])
        decision = evaluate_autoscale(e, placed, hist, now)
        if decision == "hold":
            continue
        auto = _may_auto(e, desired, e["provider"])
        if decision == "up":
            candidates = ([e["placement"]] if e["placement"] != "auto"
                          else list(observed["agents"].keys()))
            for aid in candidates:
                if aid in placed or aid not in observed["agents"] or aid in touched:
                    continue
                if now - last_action_ts.get(aid, 0) < COOLDOWN_S:
                    continue
                if _resident_conflict(e, aid, residents, managed):
                    continue
                a = observed["agents"][aid]
                fit, size = _fit_and_size(e, aid, a, free, free_ram, observed)
                if not fit:
                    continue
                (free_ram if _uses_ram_budget(e, observed, aid) else free)[aid] -= size or 0
                if e["provider"] in SINGLE_RESIDENT_PROVIDERS:
                    residents[(e["provider"], aid)] = e["model"]
                if e["provider"] == "llama" and a["server_state"] == "sleeping":
                    actions.append(Action(
                        kind="wake", provider=e["provider"], model=e["model"],
                        agent_id=aid, reason=f"{k}: waking {aid} for scale-up",
                        auto=auto, entry_key=k))
                actions.append(Action(
                    kind="scale_up", provider=e["provider"], model=e["model"],
                    agent_id=aid, reason=f"{k}: autoscale up -> {aid}",
                    auto=auto, entry_key=k))
                touched.add(aid)
                break
        else:                                 # decision == "down"
            if e["provider"] == "vllm":
                continue        # no agent-side unload path for vLLM
            # LRU among current placements; a placement missing from the
            # ledger (manager restart) sorts oldest.
            pak = placed_at.get(k) or {}
            lru_aid = min(placed, key=lambda aid: pak.get(aid, 0.0))
            if lru_aid in touched or now - last_action_ts.get(lru_aid, 0) < COOLDOWN_S:
                continue
            actions.append(Action(
                kind="scale_down", provider=e["provider"], model=e["model"],
                agent_id=lru_aid, reason=f"{k}: autoscale down -> {lru_aid}",
                auto=auto, entry_key=k))
            touched.add(lru_aid)
    return actions
