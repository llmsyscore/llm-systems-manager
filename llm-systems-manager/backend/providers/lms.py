"""LM Studio provider spec."""
from __future__ import annotations

import time

from . import ProviderSpec, register


def _fleet_aggregate(samples: dict[str, dict]) -> dict:
    """samples: {agent_id: {"sample": dict, "last_seen": float}} from STORE.all_for('lms')."""
    now = time.time()
    online = 0
    busy_agents = 0
    total_loaded = 0
    busy_processes = 0
    total_processes = 0
    server_on = 0
    agent_rows: list[dict] = []
    for aid, wrap in samples.items():
        s = wrap.get("sample") or {}
        last_seen = float(wrap.get("last_seen") or 0)
        is_online = (now - last_seen) < SPEC.online_threshold_s if last_seen else False
        models = s.get("models") or []
        ps = s.get("ps") or []
        srv_on = bool((s.get("server") or {}).get("on") or len(models) > 0)
        # Loaded models come from `ps` (loaded instances), not `models`
        # (the /v1/models download catalog). STOPPED rows are unloaded.
        loaded_now = sum(1 for p in ps if str(p.get("status", "")).upper() != "STOPPED")
        busy_now = sum(1 for p in ps
                       if str(p.get("status", "")).upper() not in ("IDLE", "STOPPED", ""))
        if is_online:
            online += 1
            if srv_on:
                server_on += 1
            total_loaded += loaded_now
            total_processes += len(ps)
            busy_processes += busy_now
            if busy_now > 0:
                busy_agents += 1
        # Offline agents report zeroed per-row counts so a consumer rendering
        # the row without re-checking online doesn't surface stale values.
        agent_rows.append({
            "agent_id": aid,
            "online": is_online,
            "server_on": srv_on if is_online else False,
            "loaded_model_count": loaded_now if is_online else 0,
            "busy_process_count": busy_now if is_online else 0,
            "age_s": round(now - last_seen, 1) if last_seen else None,
        })
    return {
        "provider": "lms",
        "agent_count_total": len(samples),
        "agent_count_online": online,
        "server_on_count": server_on,
        "busy_agent_count": busy_agents,
        "loaded_model_count_total": total_loaded,
        "process_count_total": total_processes,
        "busy_process_count_total": busy_processes,
        "agents": agent_rows,
    }


SPEC = ProviderSpec(
    name="lms",
    label="LM Studio",
    capability_key="lms",
    online_threshold_s=15.0,
    push_endpoint_legacy="/api/remote/lmstudio",
    default_picker="first_approved",
    sub_tab_keys=("lmstudio",),
    aggregator=_fleet_aggregate,
)

register(SPEC)
