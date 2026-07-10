"""vLLM provider spec."""
from __future__ import annotations

import time

from . import ProviderSpec, register


def _fleet_aggregate(samples: dict[str, dict]) -> dict:
    """samples: {agent_id: {"sample": dict, "last_seen": float}} from STORE.all_for('vllm')."""
    now = time.time()
    online = 0
    server_on = 0
    req_running = 0
    req_waiting = 0
    total_tps = 0.0
    total_pps = 0.0
    max_kv = 0.0
    models: set[str] = set()
    agent_rows: list[dict] = []
    for aid, wrap in samples.items():
        s = wrap.get("sample") or {}
        last_seen = float(wrap.get("last_seen") or 0)
        is_online = (now - last_seen) < SPEC.online_threshold_s if last_seen else False
        v = s.get("vllm") or {}
        running = v.get("state") == "running"
        m = v.get("model")
        tps = v.get("tokens_per_second") if is_online else None
        pps = v.get("prompt_tokens_per_second") if is_online else None
        rr = v.get("requests_running")
        rw = v.get("requests_waiting")
        kv = v.get("kv_cache_usage_pct")
        if is_online:
            online += 1
            if running:
                server_on += 1
                if m:
                    models.add(m)
            if isinstance(rr, (int, float)):
                req_running += int(rr)
            if isinstance(rw, (int, float)):
                req_waiting += int(rw)
            if isinstance(tps, (int, float)):
                total_tps += float(tps)
            if isinstance(pps, (int, float)):
                total_pps += float(pps)
            if isinstance(kv, (int, float)) and kv > max_kv:
                max_kv = float(kv)
        # Offline agents report zeroed/None per-row values so a consumer
        # rendering the row without re-checking online never shows stale data.
        agent_rows.append({
            "agent_id": aid,
            "online": is_online,
            "server_on": running if is_online else False,
            "model": m if (is_online and running) else None,
            "requests_running": int(rr) if (is_online and isinstance(rr, (int, float))) else None,
            "tokens_per_second": tps if isinstance(tps, (int, float)) else None,
            "age_s": round(now - last_seen, 1) if last_seen else None,
        })
    return {
        "provider": "vllm",
        "agent_count_total": len(samples),
        "agent_count_online": online,
        "server_on_count": server_on,
        "requests_running_total": req_running,
        "requests_waiting_total": req_waiting,
        "throughput": {"total_tps": total_tps, "total_pps": total_pps},
        "max_kv_cache_pct": max_kv,
        "active_models": sorted(models),
        "active_model_count": len(models),
        "agents": agent_rows,
    }


SPEC = ProviderSpec(
    name="vllm",
    label="vLLM",
    capability_key="vllm",
    online_threshold_s=15.0,
    push_endpoint_legacy="",
    default_picker="first_approved",
    sub_tab_keys=("vllm",),
    aggregator=_fleet_aggregate,
)

register(SPEC)
