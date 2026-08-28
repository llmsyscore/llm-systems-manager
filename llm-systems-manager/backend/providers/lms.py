"""LM Studio provider spec."""
from __future__ import annotations

import time

import gateway_usage  # type: ignore[import-not-found]  # sibling

from . import ProviderSpec, gpu_rollup_add, int_or_none, new_gpu_rollup, register


def _fleet_aggregate(samples: dict[str, dict]) -> dict:
    """samples: {agent_id: {"sample": dict, "last_seen": float}} from STORE.all_for('lms')."""
    now = time.time()
    online = 0
    busy_agents = 0
    total_loaded = 0
    busy_processes = 0
    total_processes = 0
    server_on = 0
    gpu_acc = new_gpu_rollup()
    usage_totals = gateway_usage.counters()
    agent_rows: list[dict] = []
    for aid, wrap in samples.items():
        s = wrap.get("sample") or {}
        last_seen = float(wrap.get("last_seen") or 0)
        is_online = (now - last_seen) < SPEC.online_threshold_s if last_seen else False
        row_gpu: dict = {}
        models = s.get("models") or []
        ps = s.get("ps") or []
        srv_on = bool((s.get("server") or {}).get("on") or len(models) > 0)
        # Loaded models come from `ps` (loaded instances), not `models`
        # (the /v1/models download catalog). STOPPED rows are unloaded.
        active = [p for p in ps if str(p.get("status", "")).upper() != "STOPPED"]
        loaded_now = len(active)
        loaded_models = sorted(str(p["model"]) for p in active if p.get("model"))
        busy_now = sum(1 for p in active
                       if str(p.get("status", "")).upper() not in ("IDLE", ""))
        if is_online:
            online += 1
            row_gpu = gpu_rollup_add(gpu_acc, s)
            if srv_on:
                server_on += 1
            total_loaded += loaded_now
            total_processes += len(ps)
            busy_processes += busy_now
            if busy_now > 0:
                busy_agents += 1
        # Offline agents report zeroed per-row counts so a consumer rendering
        # the row without re-checking online doesn't surface stale values.
        ctxs = [p.get("context") for p in active
                if isinstance(p.get("context"), (int, float))]
        totals = usage_totals.get(aid) or {}
        gen_total = int_or_none(totals.get("gen"))
        prompt_total = int_or_none(totals.get("prompt"))
        agent_rows.append({
            "agent_id": aid,
            "online": is_online,
            "server_on": srv_on if is_online else False,
            "loaded_model_count": loaded_now if is_online else 0,
            "loaded_models": loaded_models if is_online else [],
            "busy_process_count": busy_now if is_online else 0,
            "ctx": int(max(ctxs)) if (is_online and ctxs) else None,
            "total_tokens_generated": gen_total if is_online else None,
            "total_tokens_prompted": prompt_total if is_online else None,
            "power_watts": row_gpu.get("power_watts"),
            "thermal_crit": bool(row_gpu.get("thermal_crit")),
            "age_s": round(now - last_seen, 1) if last_seen else None,
        })
    return {
        "provider": "lms",
        # Gateway-observed tok/s for ONLINE agents; same shape as llama/vllm.
        "throughput": gateway_usage.fleet_rates(
            [r["agent_id"] for r in agent_rows if r["online"]]),
        "gpu": gpu_acc,
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
    default_picker="pool",
    pin_dict_key="lms_model_pins",
    gateway_enabled=True,
    sub_tab_keys=("lmstudio",),
    aggregator=_fleet_aggregate,
)

register(SPEC)
