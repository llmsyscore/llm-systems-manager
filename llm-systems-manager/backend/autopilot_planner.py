"""#472 pure planner: state in -> actions out. No I/O, no clocks."""
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
    if entry["provider"] not in a["provider_caps"] or not a["live"]:
        return False
    size = observed.get("model_sizes_mb", {}).get(
        f"{entry['provider']}:{entry['model']}")
    if size is None:
        return False
    return a["vram_free_mb"] >= size + VRAM_HEADROOM_MB

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
        for aid in candidates:
            if len(placed) >= want:
                break
            if aid in placed or aid not in observed["agents"]:
                continue
            size = observed.get("model_sizes_mb", {}).get(
                f"{e['provider']}:{e['model']}")
            if e["placement"] != "auto" and aid == e["placement"]:
                fit = size is not None and free.get(aid, 0) >= size + VRAM_HEADROOM_MB \
                      and observed["agents"][aid]["live"]
            else:
                fit = _fits(e, aid, observed) and \
                      free.get(aid, 0) >= (size or 0) + VRAM_HEADROOM_MB
            if not fit:
                continue
            free[aid] -= size or 0
            placed.append(aid)
            actions.append(Action(
                kind="load", provider=e["provider"], model=e["model"],
                agent_id=aid,
                reason=f"{k}: {len(placed)}/{want} replicas placed",
                auto=_may_auto(e, desired, e["provider"]), entry_key=k))
    return actions
