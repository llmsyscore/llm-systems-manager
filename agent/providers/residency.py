"""Per-model residency model: build, aggregate, reconcile, project. Stdlib only."""
from __future__ import annotations

from typing import Any, Optional

PROVIDERS = ("llama", "vllm", "lms")
STATUSES = ("loading", "loaded", "sleeping", "unloaded", "downloading", "failed", "unknown")
AGGREGATES = ("active", "loading", "sleeping", "idle", "off", "unknown")
SERVER_STATES = ("up", "down", "unknown")

PROFILE_PERFORMANCE = "performance"
PROFILE_POWERSAVE = "powersave"
HOLD = "hold"

_RESIDENT = ("loading", "loaded", "sleeping")


def model_entry(provider: str, model_id: str, status: str, ts: float) -> dict[str, Any]:
    st = status if status in STATUSES else "unknown"
    return {"provider": provider, "model_id": str(model_id), "status": st, "ts": float(ts)}


def llama_models_from_api(data: list, props_sleeping: dict[str, Optional[bool]],
                          ts: float) -> list[dict[str, Any]]:
    """Entries from a /v1/models body; no `status` objects means a single-model build."""
    out: list[dict[str, Any]] = []
    entries = [m for m in (data or []) if isinstance(m, dict) and m.get("id")]
    has_status = any(isinstance(m.get("status"), dict) for m in entries)
    for m in entries:
        mid = str(m["id"])
        if has_status:
            st = m.get("status") if isinstance(m.get("status"), dict) else {}
            sv = str(st.get("value") or "").lower()
            status = sv if sv in STATUSES else "unknown"
        else:
            status = "loaded"
        if status == "loaded" and props_sleeping.get(mid) is True:
            status = "sleeping"
        out.append(model_entry("llama", mid, status, ts))
    return out


def vllm_models_from_api(ids: Optional[list], ts: float) -> tuple[list[dict[str, Any]], str]:
    """(entries, server). None ids = server unreachable."""
    if ids is None:
        return [], "down"
    return [model_entry("vllm", i, "loaded", ts) for i in ids if i], "up"


def lms_models_from_ps(rows: Optional[list], server_on: Optional[bool],
                       ts: float) -> tuple[list[dict[str, Any]], str]:
    """(entries, server) from `lms ps` rows; every row is a loaded instance."""
    server = "unknown" if server_on is None else ("up" if server_on else "down")
    if rows is None:
        return [], server
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        mid = r.get("model") or r.get("identifier")
        if mid:
            out.append(model_entry("lms", mid, "loaded", ts))
    return out, server


def aggregate(models: list[dict[str, Any]], servers: dict[str, str]) -> str:
    statuses = [m.get("status") for m in models]
    if "loaded" in statuses:
        return "active"
    if "loading" in statuses or "downloading" in statuses:
        return "loading"
    if "sleeping" in statuses:
        return "sleeping"
    states = list(servers.values())
    if not states or "unknown" in states:
        return "unknown"
    if "up" in states:
        return "idle"
    return "off"


def reconcile(prev: Optional[dict], *, models: list[dict[str, Any]], servers: dict[str, str],
              ts: float, source: str = "reconciled", unknown_ticks_max: int = 6) -> dict[str, Any]:
    agg = aggregate(models, servers)
    ticks = (int((prev or {}).get("unknown_ticks") or 0) + 1) if agg == "unknown" else 0
    return {"models": [dict(m) for m in models], "servers": dict(servers), "aggregate": agg,
            "ts": float(ts), "source": source, "unknown_ticks": ticks,
            "unknown_ticks_max": int(unknown_ticks_max)}


def apply_sse_delta(res: Optional[dict], model_id: str, status: Optional[str],
                    ts: float) -> dict[str, Any]:
    """Merge one llama model status; unknown/None status leaves the entry untouched."""
    base = dict(res or {"models": [], "servers": {}, "unknown_ticks": 0, "unknown_ticks_max": 6})
    models = [dict(m) for m in base.get("models") or []]
    servers = dict(base.get("servers") or {})
    servers.setdefault("llama", "up")
    sv = str(status or "").lower()
    if sv in STATUSES and sv != "unknown":
        hit = False
        for m in models:
            if m["provider"] == "llama" and m["model_id"] == str(model_id):
                m["status"], m["ts"] = sv, float(ts)
                hit = True
        if not hit:
            models.append(model_entry("llama", model_id, sv, ts))
    out = reconcile(base, models=models, servers=servers, ts=ts, source="sse",
                    unknown_ticks_max=int(base.get("unknown_ticks_max") or 6))
    out["unknown_ticks"] = 0 if out["aggregate"] != "unknown" else out["unknown_ticks"]
    return out


def desired_profile(res: Optional[dict], unknown_ticks_max: int = 6) -> str:
    if not res:
        return HOLD
    agg = res.get("aggregate")
    if agg in ("active", "loading"):
        return PROFILE_PERFORMANCE
    if agg in ("sleeping", "idle", "off"):
        return PROFILE_POWERSAVE
    limit = int(res.get("unknown_ticks_max") or unknown_ticks_max)
    return PROFILE_POWERSAVE if int(res.get("unknown_ticks") or 0) >= limit else HOLD


def legacy_state(res: Optional[dict]) -> str:
    agg = (res or {}).get("aggregate")
    if agg in ("active", "loading", "idle"):
        return "awake"
    if agg == "sleeping":
        return "sleeping"
    return "unknown"


def changed_models(prev: Optional[dict], cur: dict) -> list[str]:
    """'provider:model_id' keys whose status differs between prev and cur."""
    def keyed(r):
        return {f"{m['provider']}:{m['model_id']}": m.get("status") for m in (r or {}).get("models") or []}
    a, b = keyed(prev), keyed(cur)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def provider_subset(res: Optional[dict], provider: str) -> Optional[dict[str, Any]]:
    """HostResidency restricted to one provider (its models + its server entry)."""
    if not res:
        return None
    models = [m for m in res.get("models") or [] if m.get("provider") == provider]
    servers = {provider: (res.get("servers") or {}).get(provider, "unknown")}
    out = reconcile(None, models=models, servers=servers, ts=float(res.get("ts") or 0.0),
                    source=str(res.get("source") or "reconciled"),
                    unknown_ticks_max=int(res.get("unknown_ticks_max") or 6))
    out["unknown_ticks"] = int(res.get("unknown_ticks") or 0)
    return out
