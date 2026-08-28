"""llama.cpp provider spec."""
from __future__ import annotations

import time

from . import ProviderSpec, gpu_rollup_add, int_or_none, new_gpu_rollup, register


def clean_display_model(raw) -> "str | None":
    """Bare model id for display. ' (unloaded)' means nothing is loaded → None;
    ' (sleeping)' models are still resident, so the name is kept."""
    if not isinstance(raw, str):
        return None
    if raw.endswith(" (unloaded)"):
        return None
    return raw.replace(" (sleeping)", "").strip() or None


def _fleet_aggregate(samples: dict[str, dict]) -> dict:
    """samples: {agent_id: {"sample": dict, "last_seen": float}} from STORE.all_for('llama')."""
    now = time.time()
    total_tps = 0.0
    total_pps = 0.0
    gpu_acc = new_gpu_rollup()
    online = 0
    awake = 0
    models: set[str] = set()
    agent_rows: list[dict] = []
    for aid, wrap in samples.items():
        s = wrap.get("sample") or {}
        last_seen = float(wrap.get("last_seen") or 0)
        is_online = (now - last_seen) < SPEC.online_threshold_s if last_seen else False
        row_gpu: dict = {}
        llama = s.get("llama") or {}
        m = clean_display_model(llama.get("model"))
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
            row_gpu = gpu_rollup_add(gpu_acc, s)
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
            "ctx": int_or_none(llama.get("n_ctx")) if is_online else None,
            "total_tokens_generated": int_or_none(llama.get("total_tokens_generated")) if is_online else None,
            "total_tokens_prompted": int_or_none(llama.get("total_tokens_prompted")) if is_online else None,
            "power_watts": row_gpu.get("power_watts"),
            "thermal_crit": bool(row_gpu.get("thermal_crit")),
            "age_s": round(now - last_seen, 1) if last_seen else None,
        })
    return {
        "provider": "llama",
        "throughput": {"total_tps": total_tps, "total_pps": total_pps},
        "gpu": gpu_acc,
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
    single_resident=True,
    gateway_enabled=True,
    sub_tab_keys=("llamacpp",),
    aggregator=_fleet_aggregate,
)

register(SPEC)
