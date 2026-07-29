"""Fleet autopilot (#472): state, observer, executors, reconciler, routes."""
from __future__ import annotations

import agent_registry  # type: ignore[import-not-found]  # sibling

_PROVIDERS = ("llama", "vllm", "lms")
_AUTOSCALE_DEFAULTS = {"target_saturation": 0.75, "up_window_s": 120,
                       "down_window_s": 900}
_DEFAULT_STATE = {"enabled": False, "entries": [], "hosts": {}}


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
    return (data.get("global") or {}).get("autopilot") or dict(_DEFAULT_STATE)


def set_state(state: dict) -> None:
    with agent_registry.agents_lock:
        data = agent_registry.load_agents()
        data.setdefault("global", {})["autopilot"] = state
        agent_registry.save_agents(data)
