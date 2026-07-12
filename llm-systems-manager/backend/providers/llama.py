"""llama.cpp provider spec."""
from __future__ import annotations

import time

from . import ProviderSpec, register


def _fleet_aggregate(samples: dict[str, dict]) -> dict:
    """samples: {agent_id: {"sample": dict, "last_seen": float}} from STORE.all_for('llama')."""
    now = time.time()
    total_tps = 0.0
    total_pps = 0.0
    max_temp = 0.0
    max_vram_pct = 0.0
    total_power = 0.0
    online = 0
    awake = 0
    models: set[str] = set()
    agent_rows: list[dict] = []
    for aid, wrap in samples.items():
        s = wrap.get("sample") or {}
        last_seen = float(wrap.get("last_seen") or 0)
        is_online = (now - last_seen) < SPEC.online_threshold_s if last_seen else False
        llama = s.get("llama") or {}
        m = llama.get("model")
        if isinstance(m, str):
            m = m.replace(" (sleeping)", "").replace(" (unloaded)", "").strip() or None
        # GPU metrics live nested under sample["gpu"] (collect_system_metrics
        # shape) — the flat gpu_* names only exist post-flatten in the AE.
        gpu = s.get("gpu") or {}
        state = llama.get("state") or "unknown"
        if is_online and state == "awake":
            awake += 1
            if m:
                models.add(m)
        tps = llama.get("tokens_per_second") if is_online else None
        pps = llama.get("prompt_tokens_per_second") if is_online else None
        if isinstance(tps, (int, float)):
            total_tps += float(tps)
        if isinstance(pps, (int, float)):
            total_pps += float(pps)
        if is_online:
            online += 1
            t = gpu.get("temperature_c")
            if isinstance(t, (int, float)) and t > max_temp:
                max_temp = float(t)
            v = gpu.get("vram_usage_percent")
            if isinstance(v, (int, float)) and v > max_vram_pct:
                max_vram_pct = float(v)
            p = gpu.get("power_watts")
            if isinstance(p, (int, float)):
                total_power += float(p)
        agent_rows.append({
            "agent_id": aid,
            "online": is_online,
            # Offline agents report their last-known state as "stale" so a
            # consumer rendering the row without re-checking online doesn't
            # show a stale "awake".
            "state": state if is_online else "stale",
            "model": m if is_online else None,
            "tokens_per_second": tps if isinstance(tps, (int, float)) else None,
            "prompt_tokens_per_second": pps if isinstance(pps, (int, float)) else None,
            "age_s": round(now - last_seen, 1) if last_seen else None,
        })
    return {
        "provider": "llama",
        "throughput": {"total_tps": total_tps, "total_pps": total_pps},
        "gpu": {"max_temp_c": max_temp, "max_vram_pct": max_vram_pct,
                "total_power_watts": total_power},
        "agent_count_total": len(samples),
        "agent_count_online": online,
        "awake_agent_count": awake,
        "active_models": sorted(models),
        "active_model_count": len(models),
        "agents": agent_rows,
    }


SPEC = ProviderSpec(
    name="llama",
    label="llama.cpp",
    capability_key="llama",
    online_threshold_s=30.0,
    push_endpoint_legacy="/api/remote/host-metrics",
    default_picker="pool",
    pin_dict_key="llama_model_pins",
    gateway_enabled=True,
    sub_tab_keys=("llamacpp",),
    aggregator=_fleet_aggregate,
)

register(SPEC)
