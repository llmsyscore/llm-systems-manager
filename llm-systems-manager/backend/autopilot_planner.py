"""#472 pure planner: state in -> actions out. No I/O, no clocks.
Sleep actions use entry_key=f"host/{agent_id}", model="", provider=""."""
from __future__ import annotations
from dataclasses import dataclass

COOLDOWN_S = 120
DWELL_S = 600
VRAM_HEADROOM_MB = 1024

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

def _placements(entry, observed) -> "list[str]":
    return [aid for aid, a in observed["agents"].items()
            if entry["model"] in (a["loaded"].get(entry["provider"]) or [])]

def _fits(entry, aid, observed) -> bool:
    a = observed["agents"][aid]
    # A sleeping agent is placeable; it will be woken before the load.
    live_or_wakeable = a["live"] or a["server_state"] == "sleeping"
    if entry["provider"] not in a["provider_caps"] or not live_or_wakeable:
        return False
    size = observed.get("model_sizes_mb", {}).get(
        f"{entry['provider']}:{entry['model']}")
    if size is None:
        return False
    return a["vram_free_mb"] >= size + VRAM_HEADROOM_MB

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

def plan(desired: dict, observed: dict, ledger: dict, now: float) -> "list[Action]":
    actions: "list[Action]" = []
    # Track hypothetical VRAM commitments within this plan pass.
    free = {aid: a["vram_free_mb"] for aid, a in observed["agents"].items()}
    last_action_ts = ledger.get("last_action_ts") or {}
    placed_at = ledger.get("placed_at") or {}
    in_flight = ledger.get("in_flight_migrations") or 0
    touched: "set[str]" = set()          # agents with an action this pass
    entries = sorted(desired.get("entries") or [], key=lambda e: e["priority"])
    for e in entries:
        k = _key(e)
        if now < (ledger.get("backoff_until") or {}).get(k, 0):
            continue
        placed = _placements(e, observed)
        want = e["min_replicas"]
        if len(placed) >= want:
            continue
        candidates = ([e["placement"]] if e["placement"] != "auto"
                      else list(observed["agents"].keys()))
        if _dwell_blocks(k, placed_at, observed, now):
            continue
        is_failover = _has_dead_candidate(e, candidates, observed)
        if is_failover and in_flight >= 1:
            continue
        for aid in candidates:
            if len(placed) >= want:
                break
            if aid in placed or aid not in observed["agents"]:
                continue
            if now - last_action_ts.get(aid, 0) < COOLDOWN_S:
                continue
            a = observed["agents"][aid]
            size = observed.get("model_sizes_mb", {}).get(
                f"{e['provider']}:{e['model']}")
            if e["placement"] != "auto" and aid == e["placement"]:
                fit = e["provider"] in a["provider_caps"] and \
                      size is not None and free.get(aid, 0) >= size + VRAM_HEADROOM_MB \
                      and (a["live"] or a["server_state"] == "sleeping")
            else:
                fit = _fits(e, aid, observed) and \
                      free.get(aid, 0) >= (size or 0) + VRAM_HEADROOM_MB
            if not fit:
                continue
            free[aid] -= size or 0
            placed.append(aid)
            auto = _may_auto(e, desired, e["provider"])
            reason = (f"failover: {k} recovering onto {aid}" if is_failover
                      else f"{k}: {len(placed)}/{want} replicas placed")
            if a["server_state"] == "sleeping":
                actions.append(Action(
                    kind="wake", provider=e["provider"], model=e["model"],
                    agent_id=aid, reason=f"{k}: waking {aid} for placement",
                    auto=auto, entry_key=k))
            actions.append(Action(
                kind="load", provider=e["provider"], model=e["model"],
                agent_id=aid, reason=reason, auto=auto, entry_key=k))
            touched.add(aid)
    for aid, hcfg in (desired.get("hosts") or {}).items():
        mins = hcfg.get("sleep_after_idle_min") or 0
        if mins <= 0 or aid in touched:
            continue
        a = observed["agents"].get(aid)
        if a is None or not a["live"] or a["server_state"] != "awake":
            continue
        idle_since = a.get("idle_since")
        if idle_since is None or now - idle_since < mins * 60:
            continue
        if "lms" in a["provider_caps"]:
            continue
        actions.append(Action(
            kind="sleep", provider="", model="", agent_id=aid,
            reason=f"idle {mins}m -> sleep", auto=False,
            entry_key=f"host/{aid}"))
        touched.add(aid)
    return actions
