"""Tower tool registry (#924): a static, schema-validated allowlist of read
tools with capped results. No shell/file/HTTP/config-write tool; no runtime registration."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("llm-systems-manager.tower")

RESULT_CAP = 4096
TIERS = ("read", "operate", "admin")
_ROLE_RANK = {"operator": 0, "admin": 1}
_SECRET_MASK = "***"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: dict
    kind: str            # read | act
    tier: str            # read | operate | admin
    run: Callable[[dict], Any]
    role: str = "operator"
    summary: Optional[Callable[[dict, Any], str]] = None


def _obj(props: dict, required: "list[str]" = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


def cap_result(obj: Any, limit: int = RESULT_CAP) -> Any:
    """Shrinks lists/dicts/strings until the JSON form fits `limit`; marks truncation."""
    if len(json.dumps(obj, default=str)) <= limit:
        return obj
    if isinstance(obj, list):
        out = list(obj)
        while out and len(json.dumps(out, default=str)) > limit - 24:
            out = out[: max(1, len(out) * 3 // 4)] if len(out) > 1 else []
        result = {"items": out, "truncated": True}
    elif isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if isinstance(v, (list, dict)):
                result[k] = cap_result(v, limit // 2)
            elif isinstance(v, str) and len(v) > limit // 2:
                result[k] = v[: limit // 2] + "…"
            else:
                result[k] = v
        result["truncated"] = True
    else:
        result = None
    if result is not None and len(json.dumps(result, default=str)) <= limit + 64:
        return result
    # Shrinks the JSON slice until the wrapper fits.
    s = json.dumps(obj, default=str)
    n = max(0, limit - 32)
    while True:
        out = {"text": s[:n] + "…", "truncated": True}
        if n == 0 or len(json.dumps(out, default=str)) <= limit:
            return out
        n = n * 3 // 4


def validate_args(tool: Tool, args: Any) -> "tuple[dict, Optional[str]]":
    """Coerce args to the tool schema: unknown keys dropped, defaults filled."""
    try:
        if not isinstance(args, dict):
            args = {}
        props = tool.params.get("properties") or {}
        out: dict = {}
        for key, spec in props.items():
            if key not in args:
                if "default" in spec:
                    out[key] = spec["default"]
                continue
            v = args[key]
            t = spec.get("type")
            if t == "integer":
                if (isinstance(v, bool) or not isinstance(v, (int, float))
                        or (isinstance(v, float) and not math.isfinite(v))
                        or int(v) != v):
                    return {}, f"{key} must be an integer"
                v = int(v)
                if "minimum" in spec and v < spec["minimum"]:
                    return {}, f"{key} must be at least {spec['minimum']}"
                if "maximum" in spec and v > spec["maximum"]:
                    return {}, f"{key} must be at most {spec['maximum']}"
            elif t == "string":
                if not isinstance(v, str):
                    return {}, f"{key} must be a string"
                v = v.strip()[:200]
                if "enum" in spec and v not in spec["enum"]:
                    return {}, f"{key} must be one of {', '.join(spec['enum'])}"
            elif t == "boolean":
                if not isinstance(v, bool):
                    return {}, f"{key} must be true or false"
            out[key] = v
        for key in tool.params.get("required") or []:
            if key not in out or out[key] in ("", None):
                return {}, f"{key} is required"
        return out, None
    except Exception:
        return {}, "invalid arguments"


def run_tool(tool: Tool, args: dict) -> "tuple[Any, bool]":
    try:
        return cap_result(tool.run(args)), True
    except Exception as e:  # noqa: BLE001 — surfaced to the model as data
        log.warning("tower tool %s failed: %s: %s", tool.name, type(e).__name__, e)
        return {"error": f"{tool.name} could not be read"}, False


def catalog(registry: dict, cfg: Any, role: str) -> "list[Tool]":
    cap = str(getattr(cfg, "capabilities", "read") or "read")
    allowed = TIERS[: TIERS.index(cap) + 1] if cap in TIERS else ("read",)
    disabled = {str(x).strip() for x in (getattr(cfg, "disabled_tools", None) or [])}
    rank = _ROLE_RANK.get(role or "operator", 0)
    return [t for t in registry.values()
            if t.tier in allowed and t.name not in disabled and _ROLE_RANK.get(t.role, 0) <= rank]


def openai_schema(tool: Tool) -> dict:
    return {"type": "function", "function": {"name": tool.name, "description": tool.description,
                                             "parameters": tool.params}}


def prompt_catalog(tools: "list[Tool]") -> str:
    lines = ["You can call these tools. To call one, reply with ONLY this fenced block, never <tool_call> or other tags:",
             "```tool", '{"name": "<tool>", "args": {…}}', "```", "Tools:"]
    for t in tools:
        tag = " (action, needs approval)" if t.kind == "act" else ""
        lines.append(f"- {t.name}{tag}: {t.description} args={json.dumps(t.params.get('properties') or {}, separators=(',', ':'))}")
    return "\n".join(lines)


def summary_line(tool: Tool, args: dict, result: Any, ms: int) -> str:
    verb = "read" if tool.kind == "read" else "ran"
    label = tool.name.replace("_", " ")
    tgt = args.get("host") or args.get("model") or args.get("alert_id") or args.get("path") or args.get("window") or ""
    if tool.summary:
        try:
            tgt = tool.summary(args, result) or tgt
        except Exception:  # noqa: BLE001 — a summary hook never breaks the tick line
            log.debug("tool summary failed for %s", tool.name, exc_info=True)
    return f"{verb} {label}" + (f" · {tgt}" if tgt else "") + f" · {ms} ms"


_HELP = {
    "slot pressure": "Slot pressure means every llama-server slot is busy and requests queue; add slots or shorten contexts.",
    "idle sleep": "llama-server enters idle sleep after the configured wait timeout; the next request pays a wake-up.",
    "autopilot": "Model Autopilot keeps pinned models resident and places replicas by the measured speed table.",
    "gateway": "The inference gateway is one OpenAI-compatible URL that routes each model to the host serving it.",
    "energy": "Energy accounting attributes measured watts and token counters to hourly rows per host.",
    "report card": "Report Card is a standardized bench producing TTFT, tok/s, VRAM, watts and $/Mtok.",
    "profiles": "Model profiles are saved llama-server flag sets per host and model (LLM Control tab); "
                "the active one is applied when the model loads.",
}


WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400}
_LIVE_STATUSES = ("active", "acknowledged")


def parse_ts(v) -> Optional[float]:
    """ISO-8601 (with Z or offset, or naive = UTC) to epoch seconds; None when unparsable."""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v.strip():
        return None
    import datetime as _dt
    try:
        d = _dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def local_ts(v):
    """An alarm-engine timestamp rewritten in the manager's local zone (ISO-8601 with offset); unparsable values pass through."""
    ts = parse_ts(v)
    if ts is None:
        return v
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


_RELATIVE = re.compile(r"^(\d+)\s*(m|h|d|w|min|mins|minutes?|hours?|days?|weeks?)(?:\s+ago)?$", re.I)
_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_SEARCH_FIELDS = ("message", "rule", "host", "metric", "id")


def parse_when(v, now: Optional[float] = None) -> Optional[float]:
    """An operator time to epoch seconds: relative shorthand (30m, 2h, 3d, 1w), a date (local midnight)
    or ISO-8601 (naive = local); None when unparsable."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    if not s:
        return None
    m = _RELATIVE.match(s)
    if m:
        return (time.time() if now is None else now) - int(m.group(1)) * _UNIT_S[m.group(2)[0].lower()]
    import datetime as _dt
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d.timestamp()


def time_range(window: Optional[str], since=None, until=None, now: Optional[float] = None) -> "tuple[Optional[float], Optional[float], str, Optional[str]]":
    """(start, end, label, error): since/until win over a window; an unparsable bound is the error."""
    now = time.time() if now is None else now
    if since or until:
        start = parse_when(since, now) if since else None
        end = parse_when(until, now) if until else None
        if since and start is None:
            return None, None, "", "since is not a time (use 2h, 3d, 1w, a date or ISO-8601)"
        if until and end is None:
            return None, None, "", "until is not a time (use 2h, 3d, 1w, a date or ISO-8601)"
        label = " ".join(x for x in (f"since {since}" if since else "", f"until {until}" if until else "") if x)
        return start, end, label, None
    if window in WINDOWS:
        return now - WINDOWS[window], None, f"last {window}", None
    return None, None, "", None


def filter_alerts(rows: list, *, status: str = "all", window: Optional[str] = None, host: Optional[str] = None,
                  rule: Optional[str] = None, now: Optional[float] = None, severity: Optional[str] = None,
                  search: Optional[str] = None, start: Optional[float] = None, end: Optional[float] = None) -> list:
    """Alert rows (as _alert_row shapes) narrowed by status, time range, host, rule, severity and free text."""
    now = time.time() if now is None else now
    if start is None and window in WINDOWS:
        start = now - WINDOWS[window]
    needle = str(search or "").strip().lower()
    out = []
    for r in rows:
        st = str(r.get("status") or "")
        if status == "active" and st not in _LIVE_STATUSES:
            continue
        if status == "closed" and st in _LIVE_STATUSES:
            continue
        if start is not None or end is not None:
            ts = parse_ts(r.get("triggered_at"))
            if ts is None or (start is not None and ts < start) or (end is not None and ts >= end):
                continue
        if host and str(r.get("host") or "").lower() != str(host).lower():
            continue
        if rule and str(rule).lower() not in str(r.get("rule") or "").lower():
            continue
        if severity and str(r.get("severity") or "").lower() != str(severity).lower():
            continue
        if needle and not any(needle in str(r.get(k) or "").lower() for k in _SEARCH_FIELDS):
            continue
        out.append(r)
    return out


def window_underfilled(rows: list, window: Optional[str] = None, now: Optional[float] = None,
                       start: Optional[float] = None) -> bool:
    """True when the oldest fetched row is newer than the range start, so the range needs more rows than were fetched."""
    if start is None and window in WINDOWS:
        start = (time.time() if now is None else now) - WINDOWS[window]
    if start is None:
        return False
    stamps = [t for t in (parse_ts(r.get("triggered_at")) for r in rows) if t]
    return bool(stamps) and min(stamps) > start


def incidents(rows: list) -> list:
    """Alerts folded by incident: the root (its own id) leads, members counted, newest incident first."""
    groups: "dict[str, list]" = {}
    for r in rows:
        groups.setdefault(str(r.get("incident") or r.get("id") or ""), []).append(r)
    out = []
    for key, members in groups.items():
        root = next((m for m in members if str(m.get("id")) == key), members[-1])
        stamps = [t for t in (parse_ts(m.get("triggered_at")) for m in members) if t]
        sev = max((str(m.get("severity") or "").lower() for m in members), key=lambda s: _SEV_RANK.get(s, 0))
        out.append({"incident": key, "rule": root.get("rule"), "host": root.get("host"), "severity": sev,
                    "status": "active" if any(str(m.get("status")) in _LIVE_STATUSES for m in members) else "closed",
                    "members": len(members), "first": local_ts(min(stamps)) if stamps else None,
                    "last": local_ts(max(stamps)) if stamps else None,
                    "alerts": [{"id": m.get("id"), "rule": m.get("rule"), "host": m.get("host")} for m in members[:10]]})
    return sorted(out, key=lambda i: str(i.get("last") or ""), reverse=True)


_SEV_RANK = {"info": 1, "warning": 2, "critical": 3}


def text_bars(pairs: "list[tuple[str, int]]", width: int = 20) -> str:
    """One line per (label, count): label, a █ bar scaled to the largest count, the count."""
    if not pairs:
        return ""
    top = max(c for _, c in pairs) or 1
    w = max(len(str(l)) for l, _ in pairs)
    return "\n".join(f"{str(l):<{w}}  {'█' * max(1, round(c * width / top)) if c else ''} {c}" for l, c in pairs)


_TIME_GROUPS = {"day": "%Y-%m-%d", "hour": "%Y-%m-%d %H:00", "hour_of_day": "%H:00"}


def alarm_stats(rows: list, group_by: str = "rule", top: int = 10, label: str = "", tz=None,
                then_by: Optional[str] = None) -> dict:
    """Counts per group (with severity split) plus a text chart; time groups and first/last are local (tz overrides).
    `then_by` adds a second breakdown (top 5) inside every group."""
    import datetime as _dt
    zone = tz or _dt.datetime.now().astimezone().tzinfo

    def fmt(ts: float, pattern: str) -> str:
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).astimezone(zone).strftime(pattern)

    def key_of(r, dim):
        if dim in _TIME_GROUPS:
            ts = parse_ts(r.get("triggered_at"))
            return fmt(ts, _TIME_GROUPS[dim]) if ts else "unknown"
        return str(r.get(dim) or "unknown")

    def key(r):
        return key_of(r, group_by)
    groups: "dict[str, dict]" = {}
    for r in rows:
        g = groups.setdefault(key(r), {"key": None, "count": 0, "critical": 0, "warning": 0, "info": 0})
        g["count"] += 1
        sev = str(r.get("severity") or "").lower()
        if sev in ("critical", "warning", "info"):
            g[sev] += 1
        if then_by:
            sub = g.setdefault("_sub", {})
            sub[key_of(r, then_by)] = sub.get(key_of(r, then_by), 0) + 1
    for k, g in groups.items():
        g["key"] = k
        sub = g.pop("_sub", None)
        if sub is not None:
            g[f"by_{then_by}"] = [{"key": sk, "count": n} for sk, n in sorted(sub.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    by_time = group_by in _TIME_GROUPS
    ordered = sorted(groups.values(), key=(lambda g: g["key"]) if by_time else (lambda g: (-g["count"], g["key"])))
    if by_time and group_by != "hour_of_day":
        ordered = ordered[-top:]
    else:
        ordered = ordered[:top] if not by_time else ordered
    stamps = [t for t in (parse_ts(r.get("triggered_at")) for r in rows) if t]
    return {"window": label, "group_by": group_by, "total": len(rows), "groups": len(groups),
            "first": fmt(min(stamps), "%Y-%m-%d %H:%M") if stamps else None,
            "last": fmt(max(stamps), "%Y-%m-%d %H:%M") if stamps else None,
            "timezone": _dt.datetime.now(zone).tzname() or "local",
            "by": ordered, "chart": text_bars([(g["key"], g["count"]) for g in ordered])}


def with_liveness(row: dict, agent: Optional[dict], liveness_of: Callable[[dict], str]) -> dict:
    """Online from the agent heartbeat (hosts with no inference provider post no samples)."""
    if not agent:
        return row
    lv = liveness_of(agent)
    return {**row, "online": lv == "live", "liveness": lv}


_LLAMA_LIVE = ("state", "model", "build", "n_ctx", "total_slots", "active_slots", "requests_processing", "requests_deferred",
               "kv_cache_tokens", "kv_cache_usage_ratio", "tokens_per_second", "prompt_tokens_per_second", "is_sleeping",
               "total_tokens_generated", "total_tokens_prompted", "download_progress", "port")
_VLLM_LIVE = ("state", "model", "kv_cache_usage_pct", "requests_running", "requests_waiting", "port")


def live_block(sample: dict, buckets: dict) -> dict:
    """Everything live about a host from its freshest sample: CPU, RAM, swap, disks, IO, net, GPU, power, UPS,
    throughput window, and each provider's runtime block. Empty sections are dropped."""
    import energy
    sysb = energy._sys_block(sample or {})
    gb = lambda b: round(float(b) / 1e9, 1) if isinstance(b, (int, float)) else None  # noqa: E731
    clean = lambda d: {k: v for k, v in (d or {}).items() if v not in (None, {}, [], "")}  # noqa: E731
    ram, swap = sysb.get("ram") or {}, sysb.get("swap") or {}
    watts, source = energy.extract_power(sample or {})
    out = {
        "cpu": clean({"pct": sysb.get("cpu_total"), "temp_c": sysb.get("cpu_temp_c"), "governor": sysb.get("cpu_governor"),
                      "per_core_pct": sysb.get("cpu_per_core")}),
        "ram": clean({"pct": ram.get("percent"), "used_gb": gb(ram.get("used_bytes")), "available_gb": gb(ram.get("available_bytes")),
                      "total_gb": gb(ram.get("total_bytes"))}),
        "swap": clean({"pct": swap.get("percent"), "used_gb": gb(swap.get("used_bytes")), "total_gb": gb(swap.get("total_bytes"))}),
        "disks": [clean({"mount": d.get("mountpoint"), "used_pct": d.get("percent"), "free_gb": gb(d.get("free_bytes"))})
                  for d in (sysb.get("disk") or []) if isinstance(d, dict)][:8],
        "disk_io": clean(sysb.get("disk_io")), "net": clean({k: v for k, v in (sysb.get("net") or {}).items() if not k.endswith("_per_s")}),
        "gpu": clean(sysb.get("gpu")), "power": clean({"watts": watts, "source": source}),
        "ups": clean(sysb.get("ups")), "mac_power": clean((sample or {}).get("mac_power") or sysb.get("mac_power")),
        "throughput_window": clean(sysb.get("throughput_window")),
    }
    provs = {}
    for prov, (s, _ls) in (buckets or {}).items():
        if prov == "llama" and isinstance(s.get("llama"), dict):
            provs["llama"] = clean({k: s["llama"].get(k) for k in _LLAMA_LIVE})
        elif prov == "vllm" and isinstance(s.get("vllm"), dict):
            provs["vllm"] = clean({k: s["vllm"].get(k) for k in _VLLM_LIVE})
        elif prov == "lms":
            ps = [clean({"model": r.get("model"), "status": r.get("status")}) for r in (s.get("ps") or []) if isinstance(r, dict)]
            provs["lms"] = clean({"server_on": (s.get("server") or {}).get("on"), "loaded": ps})
    out["providers"] = provs
    return {k: v for k, v in out.items() if v not in (None, {}, [])}


def hardware_block(sample: dict, agent: dict) -> dict:
    """Static hardware facts from a host's freshest sample plus its agent record."""
    import energy
    sysb = energy._sys_block(sample or {})
    gpu = sysb.get("gpu") or {}
    ram = sysb.get("ram") or {}
    gb = lambda b: round(float(b) / 1e9, 1) if isinstance(b, (int, float)) and b > 0 else None  # noqa: E731
    disks = [{"mount": d.get("mountpoint"), "total_gb": gb(d.get("total_bytes")), "used_pct": d.get("percent")}
             for d in (sysb.get("disk") or []) if isinstance(d, dict)][:6]
    cores = len(sysb.get("cpu_per_core") or []) or None
    out = {"cpu": sysb.get("cpu_name"), "cores": cores, "ram_gb": gb(ram.get("total_bytes")),
           "gpu": {"name": gpu.get("name"), "vendor": gpu.get("vendor"), "vram_gb": gb(gpu.get("vram_total_bytes"))} if gpu else None,
           "disks": disks, "os": agent.get("os"), "role": agent.get("role"), "agent_version": agent.get("version")}
    return {k: v for k, v in out.items() if v not in (None, [], {})}


_SECTIONS = ("all", "summary", "hardware", "live", "cpu", "ram", "gpu", "disks", "power", "providers")
_SUMMARY_KEYS = ("hostname", "online", "liveness", "age_s", "cpu_pct", "ram_pct", "gpu_pct", "gpu_temp_c", "watts", "provider_states")


def host_section(detail: dict, section: str) -> dict:
    """One slice of a full host_detail dict: the summary row, hardware, live, or one live block with its
    hardware facts (#982). `all` is the whole dict."""
    if section in ("all", "", None):
        return detail
    if section == "summary":
        return {k: detail[k] for k in _SUMMARY_KEYS if k in detail}
    hw, live = detail.get("hardware") or {}, detail.get("live") or {}
    out = {"hostname": detail.get("hostname"), "online": detail.get("online")}
    parts = {
        "hardware": {"hardware": hw},
        "live": {"live": live},
        "cpu": {"cpu_model": hw.get("cpu"), "cores": hw.get("cores"), "cpu": live.get("cpu")},
        "ram": {"ram_gb": hw.get("ram_gb"), "ram": live.get("ram"), "swap": live.get("swap")},
        "gpu": {"gpu_model": hw.get("gpu"), "gpu": live.get("gpu")},
        "disks": {"disks": live.get("disks") or hw.get("disks"), "disk_io": live.get("disk_io")},
        "power": {"watts": detail.get("watts"), "power": live.get("power"), "ups": live.get("ups"), "mac_power": live.get("mac_power")},
        "providers": {"provider_states": detail.get("provider_states"), "providers": live.get("providers")},
    }
    out.update({k: v for k, v in parts.get(section, {}).items() if v not in (None, {}, [])})
    return out


def host_names(value, known: "list[str]") -> "list[str]":
    """Host targets from a tool arg: `all` is every known host, otherwise the comma-separated names (case kept)."""
    raw = str(value or "").strip()
    if raw.lower() == "all":
        return list(known)
    seen, out = set(), []
    for n in (x.strip() for x in raw.split(",")):
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


# Tower metric names for host_history -> (alarm-engine source, metric_name, unit).
HISTORY_METRICS = {
    "cpu_pct": ("system", "cpu_total", "%"), "ram_pct": ("system", "ram_percent", "%"),
    "gpu_util_pct": ("system", "gpu_gpu_util_percent", "%"), "vram_pct": ("system", "gpu_vram_usage_percent", "%"),
    "gpu_temp_c": ("system", "gpu_temperature_c", "C"), "gpu_watts": ("system", "gpu_power_watts", "W"),
    "psu_watts": ("system", "liquidctl_psu_Total power output_value", "W"),
    "llama_tps": ("llama", "tokens_per_second", "tok/s"), "vllm_kv_pct": ("vllm", "kv_cache_usage_pct", "%"),
}
HISTORY_WINDOWS = {"1h": 60, "6h": 360, "24h": 1440, "7d": 10080, "30d": 43200}
HISTORY_POINTS = 24
_SPARK = "▁▂▃▄▅▆▇█"


def history_summary(points: list, metric: str, window: str, host: str, unit: str = "") -> dict:
    """min/avg/max, latest, trend, a sparkline and the bucketed series for one metric's history points."""
    vals = []
    for p in points or []:
        ts, v = parse_ts((p or {}).get("timestamp")), (p or {}).get("value")
        if ts is not None and isinstance(v, (int, float)) and math.isfinite(v):
            vals.append((ts, float(v)))
    vals.sort()
    base = {"host": host, "metric": metric, "unit": unit, "window": window, "points": len(vals)}
    if not vals:
        return {**base, "note": "no samples in this window"}
    v = [x for _, x in vals]
    lo, hi = min(v), max(v)
    spark = "".join(_SPARK[int(round((x - lo) / (hi - lo) * 7))] if hi > lo else _SPARK[3] for x in v)
    third = max(1, len(v) // 3)
    head, tail = sum(v[:third]) / third, sum(v[-third:]) / third
    trend = "flat" if hi == lo or abs(tail - head) < 0.05 * (hi - lo) else ("rising" if tail > head else "falling")
    return {**base, "first": local_ts(vals[0][0]), "last": local_ts(vals[-1][0]),
            "min": round(lo, 2), "avg": round(sum(v) / len(v), 2), "max": round(hi, 2), "latest": round(v[-1], 2),
            "trend": trend, "sparkline": spark,
            "series": [{"t": local_ts(ts), "v": round(x, 2)} for ts, x in vals[-HISTORY_POINTS:]]}


_OVERVIEW_SORTS = ("hostname", "watts", "cpu_pct", "ram_pct", "gpu_pct", "gpu_temp_c", "age_s")


def overview_rows(rows: list, *, provider: Optional[str] = None, online: Optional[bool] = None,
                  busy: Optional[bool] = None, sort_by: Optional[str] = None) -> list:
    """hosts_overview rows narrowed by provider, online and busy, ordered by sort_by (numbers descending,
    hostname and age ascending, missing values last) (#983)."""
    out = [r for r in rows
           if (not provider or provider in (r.get("providers") or []))
           and (online is None or bool(r.get("online")) == online)
           and (busy is None or bool(r.get("busy")) == busy)]
    if sort_by == "hostname":
        out.sort(key=lambda r: str(r.get("hostname") or "").lower())
    elif sort_by in _OVERVIEW_SORTS:
        asc = sort_by == "age_s"
        def key(r):
            v = r.get(sort_by)
            missing = not isinstance(v, (int, float))
            return (missing, (v if asc else -v) if not missing else 0, str(r.get("hostname") or "").lower())
        out.sort(key=key)
    return out


LOG_SOURCES = ("llama", "lms", "vllm", "agent", "manager", "alarm_engine")
LOG_LINES_MAX = 200
# A level token bounded by brackets, whitespace or a colon: "[ERROR]", " ERROR ", "error:", never "severity=critical".
_LOG_LEVEL_RE = {"error": re.compile(r"(?:^|[\s\[(])(?:ERROR|CRITICAL|FATAL|[Ee]rror|Exception|Traceback)(?=[\]\s:)]|$)"),
                 "warning": re.compile(r"(?:^|[\s\[(])(?:WARNING|WARN|[Ww]arning)(?=[\]\s:)]|$)")}
_LOG_CONTINUATION = re.compile(r"^(?:\s+|Traceback|  File |\w+(?:Error|Exception)\b)")
_LOG_TS_RE = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")


def _line_ts(line: str) -> Optional[float]:
    m = _LOG_TS_RE.match(line or "")
    return parse_when(m.group(1).replace(",", ".").replace(" ", "T", 1)) if m else None


def filter_log_lines(lines: list, *, search: Optional[str] = None, level: Optional[str] = None,
                     since: Optional[float] = None, before: int = 0, count: int = 40) -> dict:
    """The newest `count` lines matching search/level/since, skipping the newest `before` matches (#984);
    a line without a timestamp inherits the previous line's time so tracebacks stay with their header."""
    needle = str(search or "").strip().lower()
    lvl = _LOG_LEVEL_RE.get(str(level or "").lower())
    kept, last_ts, prev_kept = [], None, False
    for ln in lines or []:
        ln = str(ln or "")
        ts = _line_ts(ln)
        if ts is not None:
            last_ts = ts
        if since is not None and (last_ts is None or last_ts < since):
            prev_kept = False
            continue
        if needle and needle not in ln.lower():
            prev_kept = False
            continue
        # A level match keeps its continuation lines (traceback frames) too.
        if lvl and not lvl.search(ln) and not (prev_kept and ts is None and _LOG_CONTINUATION.match(ln)):
            prev_kept = False
            continue
        kept.append(ln)
        prev_kept = True
    total = len(kept)
    before = max(0, int(before or 0))
    end = max(0, total - before)
    start = max(0, end - max(1, int(count or 1)))
    return {"lines": kept[start:end], "matched": total, "older": start, "newer": total - end}


# Every tool name build_registry can return, for the settings chip list.
READ_TOOL_NAMES = ("hosts_overview", "host_detail", "host_history", "models", "model_profiles", "alarms", "alarm_history",
                   "alert_detail", "energy_summary", "gateway_flow", "recent_runs", "bench_speed", "service_health",
                   "log_tail", "config_get", "help", "audit_log")
ACT_TOOL_NAMES = ("load_model", "unload_model", "wake_server", "restart_provider", "ack_alert", "close_alert")
TOOL_NAMES = READ_TOOL_NAMES + ACT_TOOL_NAMES
PROVIDER_LABEL = {"llama": "llama.cpp", "lms": "LM Studio", "vllm": "vLLM"}
_PROVIDER_ENUM = {"type": "string", "enum": ["llama", "lms", "vllm"]}
_GROUP_DIMS = ("rule", "host", "severity", "status", "day", "hour", "hour_of_day")
# vLLM has no load/unload endpoint, so those two tools drop it from their enum.
_LOAD_PROVIDER_ENUM = {"type": "string", "enum": ["llama", "lms"]}


_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)
_CONNECT_ERRORS = ("request failed", "no callback URL recorded", "no response")


def scrub_agent_error(err, host: str) -> str:
    """Agent-call failure text safe to show: connect failures become one fixed line, URLs are removed (#957)."""
    text = str(err or "")
    if not text or _URL_RE.search(text) or any(text.endswith(m) or text == m for m in _CONNECT_ERRORS):
        return f"could not reach the agent on {host}"
    return text


def _act(deps: dict, name: str, key: str, *args) -> dict:
    """Runs one act dep `(…) -> (ok, err)`, always returning the `{ok, message}` shape act tools promise."""
    fn = deps.get(key)
    if fn is None:
        return {"ok": False, "message": "action not wired"}
    try:
        ok, err = fn(*args)
    except Exception as e:  # noqa: BLE001 — never lets a raw exception (host/token text) reach the model
        log.warning("tower act %s failed: %s: %s", name, type(e).__name__, e)
        return {"ok": False, "message": f"{name} failed: {type(e).__name__}"}
    return {"ok": bool(ok), "message": (err or "failed") if not ok else "done"}


def build_registry(deps: dict) -> "dict[str, Tool]":
    """Read tools plus the six act tools; every callable comes from `deps` (injected)."""
    def config_get(a):
        path = a["path"]
        if not path.startswith(("manager.", "alarm_engine.", "openclaw.", "influxdb.", "notifications.", "logging.")):
            raise ValueError("not a setting")
        return deps["config_get"](path)

    tools = [
        Tool("hosts_overview", "Every host in one call: providers, online, resident model, busy, watts, CPU %, RAM %, GPU % "
             "and GPU temp. Filter by provider, online or busy; sort_by ranks hosts (watts, cpu_pct, ram_pct, gpu_pct, "
             "gpu_temp_c, age_s, hostname). Current state only: trends are host_history.",
             _obj({"provider": _PROVIDER_ENUM, "online": {"type": "boolean"}, "busy": {"type": "boolean"},
                   "sort_by": {"type": "string", "enum": list(_OVERVIEW_SORTS)}}), "read", "read",
             lambda a: deps["hosts"](a.get("provider"), a.get("online"), a.get("busy"), a.get("sort_by"))),
        Tool("host_detail", "Current state of one or more hosts (host=name, a comma-separated list, or all): hardware "
             "(CPU model, cores, RAM, GPU, VRAM, disks, OS, agent version) and live state (CPU pct and temp, RAM, swap, "
             "disks, IO, net, GPU clocks/temps/power/VRAM, watts, UPS, throughput window, llama/LM Studio/vLLM runtime). "
             "section narrows it; several hosts return one summary row each. Trends are host_history; energy and cost "
             "totals are energy_summary; benchmark results are recent_runs and bench_speed.",
             _obj({"host": {"type": "string"}, "section": {"type": "string", "enum": list(_SECTIONS), "default": "all"}}, ["host"]),
             "read", "read", lambda a: deps["host"](a["host"], a.get("section", "all")) or {"error": "unknown host"}),
        Tool("host_history", "One metric's history over a window: min/avg/max, latest, trend and a sparkline; one host also "
             "returns up to 24 bucketed points, several hosts (comma-separated) or all return one summary per host.",
             _obj({"host": {"type": "string"}, "metric": {"type": "string", "enum": list(HISTORY_METRICS)},
                   "window": {"type": "string", "enum": list(HISTORY_WINDOWS), "default": "24h"}}, ["host", "metric"]),
             "read", "read", lambda a: deps["host_history"](a["host"], a["metric"], a.get("window", "24h"))),
        Tool("models", "Models on every host: loaded now (loaded_on) and available to load (available_on), per provider.",
             _obj({"host": {"type": "string"}, "provider": {"type": "string", "enum": ["llama", "lms", "vllm"]}}), "read", "read",
             lambda a: deps["models"](a.get("host"), a.get("provider"))),
        Tool("model_profiles", "Saved llama-server config profiles per host and model: the active profile, the other "
             "profile names, and (when a model is given) the active or named profile's values.",
             _obj({"host": {"type": "string"}, "model": {"type": "string"}, "profile": {"type": "string"}}),
             "read", "read", lambda a: deps["profiles"](a.get("host"), a.get("model"), a.get("profile"))),
        Tool("alarms", "Alerts from the alarm engine, newest first, with total and next_offset for paging. Filter by status, "
             "severity, host, rule, free-text search, a window or since/until (30m, 2h, 3d, 1w, a date, ISO-8601); "
             "group=incident folds member alerts under their root alert.",
             _obj({"status": {"type": "string", "enum": ["active", "all", "closed"], "default": "active"},
                   "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
                   "window": {"type": "string", "enum": list(WINDOWS)},
                   "since": {"type": "string"}, "until": {"type": "string"},
                   "host": {"type": "string"}, "rule": {"type": "string"}, "search": {"type": "string"},
                   "group": {"type": "string", "enum": ["none", "incident"], "default": "none"},
                   "count": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                   "offset": {"type": "integer", "minimum": 0, "default": 0}}), "read", "read",
             lambda a: deps["alarm_search"](a)),
        Tool("alarm_history", "Alert counts over a window or since/until, grouped by rule, host, severity, status, day, "
             "hour or hour_of_day, with a text bar chart; then_by adds a second breakdown per group. Filter by status, "
             "severity, host, rule or free text. Use it for trends, totals and top offenders.",
             _obj({"window": {"type": "string", "enum": ["24h", "7d", "30d", "90d"], "default": "30d"},
                   "since": {"type": "string"}, "until": {"type": "string"},
                   "group_by": {"type": "string", "enum": list(_GROUP_DIMS), "default": "rule"},
                   "then_by": {"type": "string", "enum": list(_GROUP_DIMS)},
                   "status": {"type": "string", "enum": ["all", "active", "closed"], "default": "all"},
                   "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
                   "host": {"type": "string"}, "rule": {"type": "string"}, "search": {"type": "string"},
                   "top": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}}), "read", "read",
             lambda a: deps["alarm_history"](a["window"], a["group_by"], a["top"], a.get("host"), a.get("rule"), a)),
        Tool("alert_detail", "One alert by id.", _obj({"alert_id": {"type": "string"}}, ["alert_id"]), "read", "read",
             lambda a: deps["alert"](a["alert_id"]) or {"error": "alert not found"}),
        Tool("energy_summary", "Energy and cost totals for a window.",
             _obj({"window": {"type": "string", "enum": ["today", "24h", "7d", "month"], "default": "today"}}), "read", "read",
             lambda a: deps["energy"](a["window"])),
        Tool("gateway_flow", "Gateway clients, hosts, throughput and in-flight requests right now.", _obj({}), "read", "read",
             lambda a: deps["flow"]()),
        Tool("recent_runs", "Recent Report Card / benchmark / autotune runs.",
             _obj({"tool": {"type": "string", "enum": ["reportcard", "benchmark", "autotune", "quality"]},
                   "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}}), "read", "read",
             lambda a: deps["runs"](a.get("tool"), a["count"])),
        Tool("bench_speed", "Measured decode speed per host for a model.", _obj({"model": {"type": "string"}}, ["model"]),
             "read", "read", lambda a: deps["speed"](a["model"])),
        Tool("service_health", "Manager, alarm engine and agent health.", _obj({}), "read", "read", lambda a: deps["health"]()),
        Tool("log_tail", "Newest lines of a log: provider server logs (llama, lms, vllm) or the agent log on a host "
             "(host=name, a comma-separated list, or all), or the manager / alarm_engine service log. search "
             "(case-insensitive), level (error, warning) and since (30m, 2h, a date, ISO) filter before the line cap; "
             "before skips the newest N matches to page back. host is not needed for manager or alarm_engine. "
             "Only what each log endpoint serves is searchable.",
             _obj({"host": {"type": "string"}, "provider": {"type": "string", "enum": list(LOG_SOURCES), "default": "llama"},
                   "lines": {"type": "integer", "minimum": 5, "maximum": LOG_LINES_MAX, "default": 40},
                   "search": {"type": "string"}, "level": {"type": "string", "enum": ["error", "warning"]},
                   "since": {"type": "string"}, "before": {"type": "integer", "minimum": 0, "default": 0}}),
             "read", "read", lambda a: deps["log_tail"](a.get("host"), a.get("provider", "llama"), a["lines"], a)),
        Tool("config_get", "A manager or alarm-engine setting by dotted path (secrets are masked); "
             "not model configs — use model_profiles.",
             _obj({"path": {"type": "string"}}, ["path"]), "read", "read", config_get, role="admin"),
        Tool("help", "Short explanation of a dashboard concept.", _obj({"topic": {"type": "string"}}, ["topic"]), "read", "read",
             lambda a: deps["help"](a["topic"])),
        Tool("audit_log", "Recent audit-log entries: who did what and when; filter by actor, action prefix and time window.",
             _obj({"window": {"type": "string", "enum": ["1h", "24h", "7d", "30d"], "default": "24h"},
                   "actor": {"type": "string"}, "action": {"type": "string"},
                   "count": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}}), "read", "read",
             lambda a: deps["audit"](a.get("window", "24h"), a.get("actor"), a.get("action"), a.get("count", 20)), role="admin"),
    ]
    tools += [
        Tool("load_model", "Load a model on a host's provider server (asks the operator first).",
             _obj({"provider": _LOAD_PROVIDER_ENUM, "host": {"type": "string"}, "model": {"type": "string"}}, ["provider", "host", "model"]),
             "act", "operate", lambda a: _act(deps, "load_model", "load", a["provider"], a["host"], a["model"])),
        Tool("unload_model", "Unload a model from a host's provider server (asks the operator first).",
             _obj({"provider": _LOAD_PROVIDER_ENUM, "host": {"type": "string"}, "model": {"type": "string"}}, ["provider", "host", "model"]),
             "act", "operate", lambda a: _act(deps, "unload_model", "unload", a["provider"], a["host"], a["model"])),
        Tool("wake_server", "Wake a sleeping llama-server on a host (asks the operator first).",
             _obj({"host": {"type": "string"}}, ["host"]), "act", "operate", lambda a: _act(deps, "wake_server", "wake", a["host"])),
        Tool("restart_provider", "Restart a provider server on a host; in-flight requests fail (admin, asks first).",
             _obj({"provider": _PROVIDER_ENUM, "host": {"type": "string"}}, ["provider", "host"]),
             "act", "admin", lambda a: _act(deps, "restart_provider", "restart", a["provider"], a["host"]), role="admin"),
        Tool("ack_alert", "Acknowledge an alert in the alarm engine (asks the operator first).",
             _obj({"alert_id": {"type": "string"}}, ["alert_id"]), "act", "operate", lambda a: _act(deps, "ack_alert", "ack", a["alert_id"])),
        Tool("close_alert", "Close an alert in the alarm engine (asks the operator first).",
             _obj({"alert_id": {"type": "string"}}, ["alert_id"]), "act", "operate", lambda a: _act(deps, "close_alert", "close", a["alert_id"])),
    ]
    return {t.name: t for t in tools}


def action_card(tool: Tool, args: dict) -> dict:
    """What an act tool will do, where, and what it will not do — the approval card's copy."""
    if tool.kind != "act":
        return {}
    prov = PROVIDER_LABEL.get(str(args.get("provider") or ""), args.get("provider") or "")
    host, model, aid = args.get("host") or "", args.get("model") or "", args.get("alert_id") or ""
    cards = {
        "load_model": (f"Load {model}", f"{host} · {prov}", "Asks the host's agent to load the model into its server.",
                       "Nothing is unloaded; other hosts are untouched."),
        "unload_model": (f"Unload {model}", f"{host} · {prov}", "Frees the model's memory on that host.",
                         "No other model or host changes."),
        "wake_server": ("Wake llama-server", f"{host} · llama.cpp", "Sends a one-token completion so the server leaves idle sleep.",
                        "No model is loaded or unloaded."),
        "restart_provider": (f"Restart {prov}", f"{host} · {prov}", "Restarts the provider server on that host; requests in flight fail.",
                             "The resident model reloads on start; no other host changes."),
        "ack_alert": (f"Acknowledge alert {aid}", f"alert {aid}", "Marks the alert acknowledged in the alarm engine.",
                      "The rule keeps evaluating; nothing on a host changes."),
        "close_alert": (f"Close alert {aid}", f"alert {aid}", "Closes the alert in the alarm engine.",
                        "It reopens if the rule fires again."),
    }
    title, target, does, not_ = cards.get(tool.name, (tool.name.replace("_", " "), host or aid, tool.description, ""))
    return {"title": title, "target": target, "does": does, "not": not_}


def default_help(topic: str) -> str:
    key = (topic or "").strip().lower()
    for k, v in _HELP.items():
        if k in key or key in k:
            return v
    return "No note for that topic; the docs at /docs cover the dashboard tabs."


def loaded_models(agents: dict, sample_of: Callable[[str, str], dict], loaded_by_provider: dict,
                  host: Optional[str] = None, provider: Optional[str] = None) -> "dict[tuple, list]":
    """{(provider, model): [hostnames]} for every model resident on an approved agent right now."""
    out: "dict[tuple, list]" = {}
    for aid, a in agents.items():
        if a.get("status") != "approved":
            continue
        hn = str(a.get("hostname") or "")
        if host and hn.lower() != str(host).lower():
            continue
        for prov, fn in loaded_by_provider.items():
            if provider and prov != provider:
                continue
            for m in fn(sample_of(prov, aid) or {}):
                out.setdefault((prov, m), []).append(hn)
    return out


def models_rows(entries: list, loaded: "dict[tuple, list]", host: Optional[str] = None,
                provider: Optional[str] = None) -> list:
    """Merges the gateway catalogue with the resident set; loaded rows first."""
    rows: dict = {}
    here = lambda hs: [h for h in hs if not host or str(h).lower() == str(host).lower()]  # noqa: E731
    loaded_here = {k for k, hs in loaded.items() if here(hs)}
    for e in entries:
        prov, mid = e.get("provider") or "llama", e.get("id")
        if not mid or (provider and prov != provider):
            continue
        hosts = here(e.get("hosts") or [])
        if host and not hosts and (prov, mid) not in loaded_here:
            continue
        rows[(prov, mid)] = {"model": mid, "provider": prov, "loaded": False, "loaded_on": [], "available_on": hosts}
    for (prov, mid), hs in loaded.items():
        hs = here(hs)
        if not hs or (provider and prov != provider):
            continue
        r = rows.setdefault((prov, mid), {"model": mid, "provider": prov, "loaded": False, "loaded_on": [], "available_on": []})
        r["loaded"], r["loaded_on"] = True, sorted(set(hs))
    return sorted(rows.values(), key=lambda r: (not r["loaded"], r["provider"], r["model"]))


def prod_deps(ctx, *, db_path: str, tools_runs: Callable[[Optional[str], int], list],
              speed_table: Callable[[str], list], service_health: Callable[[], dict],
              gateway_entries: Callable[[], list],
              audit_rows: Callable[[str, Optional[str], Optional[str], int], list]) -> dict:
    """Production readers: Discord bot deps for hosts/host/alarms, plus models from the
    gateway index + polled provider state, energy, flow, runs, speed, health, log tail, masked config."""
    import urllib.parse
    import discord_bot
    import energy
    import gateway
    import providers as providers_mod
    import settings_catalog
    base = discord_bot.prod_deps(ctx)

    def _ae(path):
        getter = getattr(ctx, "alarm_engine_url", None)
        root = str((getter() if callable(getter) else getter) or "").rstrip("/")
        return ctx.ae_session.get(f"{root}{path}", timeout=10)

    def _alert_row(a: dict) -> dict:
        metric = "/".join(str(a.get(k) or "") for k in ("metric_source", "metric_name")).strip("/")
        return {"id": a.get("alert_id"), "rule": a.get("rule_name"), "severity": a.get("severity"),
                "status": a.get("status"), "host": a.get("source_host"), "message": a.get("message"),
                "metric": metric or None, "value": a.get("current_value"), "threshold": a.get("threshold_value"),
                "triggered_at": local_ts(a.get("created_at")), "last_seen": local_ts(a.get("last_evaluated_at")),
                "acknowledged_at": local_ts(a.get("acknowledged_at") or None), "closed_at": local_ts(a.get("closed_at") or None),
                "incident": a.get("incident_id") or None}

    def _alerts(include_closed: bool, limit: int) -> list:
        q = "&include_closed=true" if include_closed else ""
        r = _ae(f"/api/alarm/alerts/?limit={int(limit)}{q}")
        r.raise_for_status()
        body = r.json()
        return [_alert_row(a) for a in (body if isinstance(body, list) else [])]

    def _rows_for(include_closed: bool, start: Optional[float], want: Optional[int] = None) -> "tuple[list, Optional[str]]":
        """Newest 1000 alerts, or the CSV export (10000) when the range start or a page past 1000 reaches
        further back; (rows, cap note)."""
        rows = _alerts(include_closed, 1000)
        if len(rows) < 1000:
            return rows, None
        if (start is not None and window_underfilled(rows, start=start)) or (want is not None and want > len(rows)):
            return _alerts_csv(), "capped at the newest 10000 alerts"
        return rows, "capped at the newest 1000 alerts"

    def alarms(status="active", count=10, window=None, host=None, rule=None):
        """Plain rows (the watcher and Discord read this); alarm_search is the tool's paged view."""
        wide = bool(window or host or rule) or status != "active"
        rows = _alerts(status != "active", 1000 if wide else int(count))
        return filter_alerts(rows, status=status, window=window, host=host, rule=rule)[:int(count)]

    def alarm_search(a: dict) -> dict:
        status, count, offset = a.get("status", "active"), int(a.get("count") or 10), int(a.get("offset") or 0)
        start, end, label, err = time_range(a.get("window"), a.get("since"), a.get("until"))
        if err:
            return {"error": err}
        narrow = any(a.get(k) for k in ("window", "since", "until", "host", "rule", "severity", "search"))
        grouped = a.get("group") == "incident"
        if not narrow and not grouped and not offset and status == "active":
            rows, note = _alerts(False, count), None
        else:
            rows, note = _rows_for(status != "active", start, offset + count)
        rows = filter_alerts(rows, status=status, host=a.get("host"), rule=a.get("rule"), severity=a.get("severity"),
                             search=a.get("search"), start=start, end=end)
        items = incidents(rows) if grouped else rows
        page = items[offset:offset + count]
        out = {"incidents" if grouped else "alerts": page, "total": len(items), "offset": offset,
               "next_offset": offset + count if offset + count < len(items) else None}
        if label:
            out["range"] = label
        if note:
            out["note"] = note
        return out

    def _alerts_csv() -> list:
        """The alarm engine's CSV export (newest 10000 alerts, closed included) as _alert_row shapes."""
        import csv
        import io
        r = _ae("/api/alarm/alerts/export?format=csv")
        r.raise_for_status()
        rows = []
        for a in csv.DictReader(io.StringIO(r.text)):
            rows.append(_alert_row({"alert_id": a.get("alert_id"), "rule_name": a.get("rule_name"), "severity": a.get("severity"),
                                    "status": a.get("status"), "source_host": a.get("source_host"), "message": a.get("message"),
                                    "created_at": a.get("created_at"), "last_evaluated_at": a.get("closed_at") or a.get("created_at"),
                                    "acknowledged_at": a.get("acknowledged_at"), "closed_at": a.get("closed_at")}))
        return rows

    def alarm_history(window="30d", group_by="rule", top=10, host=None, rule=None, a: Optional[dict] = None):
        a = a or {}
        start, end, label, err = time_range(window, a.get("since"), a.get("until"))
        if err:
            return {"error": err}
        rows, note = _rows_for(True, start)
        rows = filter_alerts(rows, status=a.get("status", "all"), host=host, rule=rule, severity=a.get("severity"),
                             search=a.get("search"), start=start, end=end)
        out = alarm_stats(rows, group_by, int(top), label, then_by=a.get("then_by") or None)
        if note:
            out["note"] = note
        return out

    def _agent_by_hostname(host):
        import agent_registry
        agents = agent_registry.load_agents().get("agents") or {}
        return next((dict(a, agent_id=aid) for aid, a in agents.items()
                     if a.get("status") == "approved" and str(a.get("hostname") or "").lower() == str(host).lower()), None)

    def _liveness(agent):
        import agent_registry
        return agent_registry.agent_liveness(agent)

    _OVERVIEW_METRICS = ("cpu_pct", "ram_pct", "gpu_pct", "gpu_temp_c")

    def hosts_overview(provider=None, online=None, busy=None, sort_by=None):
        import agent_registry
        agents = agent_registry.load_agents().get("agents") or {}
        by_host = {str(a.get("hostname") or "").lower(): a for a in agents.values()}
        rows = []
        for r in base["fleet"]():
            row = with_liveness(r, by_host.get(str(r.get("hostname") or "").lower()), _liveness)
            detail = base["host"](r.get("hostname")) or {}
            row.update({k: detail.get(k) for k in _OVERVIEW_METRICS if detail.get(k) is not None})
            rows.append(row)
        return overview_rows(rows, provider=provider, online=online, busy=busy, sort_by=sort_by)

    def _one_host(name):
        row = base["host"](name)
        agent = _agent_by_hostname(name)
        if row is None or agent is None:
            return row
        buckets = energy.store_view_from_provider_state().get(agent["agent_id"]) or {}
        sample, _ls = discord_bot._freshest(buckets)
        return {**with_liveness(row, agent, _liveness), "hardware": hardware_block(sample, agent),
                "live": live_block(sample, buckets)}

    def agent_registry_agents() -> dict:
        import agent_registry
        return agent_registry.load_agents().get("agents") or {}

    def _known_hosts():
        return sorted(str(a.get("hostname")) for a in agent_registry_agents().values() if a.get("status") == "approved" and a.get("hostname"))

    def host_detail(name, section="all"):
        names = host_names(name, _known_hosts())
        if not names:
            return None
        if len(names) == 1:
            d = _one_host(names[0])
            return host_section(d, section) if d else None
        rows = []
        for n in names:
            d = _one_host(n)
            rows.append(host_section(d, "summary" if section in ("all", "", None) else section) if d else {"hostname": n, "error": "unknown host"})
        return {"hosts": rows}

    def _history_one(host, metric, window):
        agent = _agent_by_hostname(host)
        if not agent:
            return {"host": host, "error": "unknown host"}
        source, name, unit = HISTORY_METRICS[metric]
        q = urllib.parse.urlencode({"since_minutes": HISTORY_WINDOWS.get(window, 1440), "hostname": agent.get("hostname") or host,
                                    "max_points": HISTORY_POINTS, "agg": "mean"})
        r = _ae(f"/api/alarm/metrics/{urllib.parse.quote(source, safe='')}/{urllib.parse.quote(name, safe='')}?{q}")
        if not r.ok:
            return {"host": agent.get("hostname") or host, "error": "history unavailable from the alarm engine"}
        body = r.json()
        return history_summary(body if isinstance(body, list) else [], metric, window, agent.get("hostname") or host, unit)

    def host_history(host, metric, window="24h"):
        names = host_names(host, _known_hosts())
        if not names:
            return {"error": "unknown host"}
        if len(names) == 1:
            return _history_one(names[0], metric, window)
        per = []
        for n in names:
            one = _history_one(n, metric, window)
            one.pop("series", None)
            per.append({k: v for k, v in one.items() if k not in ("metric", "window")})
        return {"metric": metric, "window": window, "unit": HISTORY_METRICS[metric][2], "hosts": per}

    def alert(aid):
        r = _ae(f"/api/alarm/alerts/{urllib.parse.quote(str(aid), safe='')}")
        return _alert_row(r.json()) if r.ok else None

    def energy_summary(window="today"):
        now = time.time()
        spans = {"today": now - (now % 86400), "24h": now - 86400, "7d": now - 7 * 86400, "month": now - 30 * 86400}
        start = int(spans[window] // 3600) * 3600
        end = int(now // 3600 + 1) * 3600
        cfg = energy._cfg_energy(ctx)
        factory = energy._conn_factory
        if factory is None:
            return {"window": window, "error": "energy accounting not started"}
        rows = energy.query_rows(factory(), start, end)
        s = energy.summarize(rows, max(0.0, now - start), cfg["price_kwh"], cfg["cloud_price_in_per_mtok"], cfg["cloud_price_out_per_mtok"])
        return {"window": window, "totals": s.get("totals"), "hosts": s.get("hosts")}

    def _manager_log_lines():
        import os
        path = os.path.join(str(getattr(getattr(ctx, "settings", None), "paths", None).log_dir), "llm-systems-manager.log")
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > 50 * 1024:
                    f.seek(size - 50 * 1024)
                    f.readline()
                return [ln.decode("utf-8", errors="replace").rstrip() for ln in f.read().splitlines()], None
        except FileNotFoundError:
            return [], "log file does not exist yet"
        except OSError as e:
            return [], f"log unreadable: {type(e).__name__}"

    def _log_lines(source, host) -> "tuple[list, Optional[str]]":
        """(lines, note) for one log; the host is ignored for the manager and alarm engine logs."""
        if source == "manager":
            return _manager_log_lines()
        if source == "alarm_engine":
            r = _ae("/api/alarm/admin/log/tail")
            if r.status_code in (401, 403):
                return [], "the alarm engine refused the management token"
            if r.status_code == 404:
                return [], "the alarm engine is too old to serve its log"
            body = r.json() if r.ok else {}
            return list(body.get("lines") or []), body.get("note")
        agent = _agent_by_hostname(host)
        if not agent:
            return [], "unknown host"
        if source in PROVIDER_LABEL and not (agent.get("capabilities") or {}).get(_cap_key(source)):
            return [], f"{host} does not serve {PROVIDER_LABEL[source]}"
        if source == "lms":
            return [], "LM Studio serves no log through the agent"
        res = discord_bot._agent_json(agent, "/agent/log/tail" if source == "agent" else f"/{source}/log/tail")
        if not isinstance(res, dict):
            return [], f"could not reach the agent on {host}"
        return list(res.get("lines") or []), res.get("note")

    def log_tail(host, provider="llama", lines=40, a: Optional[dict] = None):
        a = a or {}
        since = None
        if a.get("since"):
            since = parse_when(a["since"])
            if since is None:
                return {"error": "since is not a time (use 30m, 2h, 3d, a date or ISO-8601)"}
        filt = dict(search=a.get("search"), level=a.get("level"), since=since, before=int(a.get("before") or 0))
        if provider in ("manager", "alarm_engine"):
            raw, note = _log_lines(provider, None)
            out = {"source": provider, **filter_log_lines(raw, count=int(lines), **filt)}
            return {**out, "note": note} if note else out
        # `all` for a provider log means every host that serves that provider.
        known = _known_hosts() if provider not in PROVIDER_LABEL else [
            str(a.get("hostname")) for a in (agent_registry_agents().values())
            if a.get("status") == "approved" and a.get("hostname") and (a.get("capabilities") or {}).get(_cap_key(provider))]
        names = host_names(host, sorted(known))
        if not names:
            if str(host or "").strip().lower() == "all":
                return {"source": provider, "hosts": [], "note": f"no host serves {PROVIDER_LABEL[provider]}"}
            return {"error": f"host is required for {provider} logs (a name, a comma-separated list, or all)"}
        per_host = max(5, int(lines) // len(names)) if len(names) > 1 else int(lines)
        rows = []
        for n in names:
            raw, note = _log_lines(provider, n)
            one = {"host": n, "source": provider, **filter_log_lines(raw, count=per_host, **filt)}
            if note:
                one["note"] = note
            rows.append(one)
        return rows[0] if len(rows) == 1 else {"hosts": rows}

    def models(host=None, provider=None):
        import agent_registry
        import autopilot
        import provider_state
        agents = agent_registry.load_agents().get("agents") or {}
        unsure: "list[str]" = []

        def sample_of(prov, aid):
            snap = provider_state.STORE.get(prov, aid) or {}
            if prov == "llama" and ((agents.get(aid) or {}).get("capabilities") or {}).get(_cap_key("llama")):
                fresh, why = _llama_fresh({"agent_id": aid})
                if not fresh:
                    unsure.append(f"{(agents.get(aid) or {}).get('hostname') or aid} ({why})")
                    return {}
            return snap.get("sample") or {}

        loaded = loaded_models(agents, sample_of, autopilot._LOADED_BY_PROVIDER, host, provider)
        rows = models_rows(gateway_entries(), loaded, host, provider)
        if unsure:
            return {"models": rows, "note": "llama residency not counted for hosts without a fresh sample: " + ", ".join(sorted(unsure))}
        return rows

    def _match_model(models: dict, want: str):
        """Exact model id, else case-insensitive, else the first substring hit."""
        if want in models:
            return want
        low = want.lower()
        return next((k for k in models if k.lower() == low),
                    next((k for k in models if low in k.lower()), None))

    def _profile_row(hostname, entry, model_id, profile):
        names = list((entry.get("profiles") or {}).keys())
        pick = profile or entry.get("active")
        if pick not in names:
            return {"error": "unknown profile"}
        return {"host": hostname, "model": model_id, "active": entry.get("active"), "profiles": names,
                "values": entry["profiles"][pick]}

    def profiles(host=None, model=None, profile=None):
        import agent_registry
        import model_profiles
        store = model_profiles.STORE
        if store is None:
            return {"error": "profile store not available"}
        agents = agent_registry.load_agents().get("agents") or {}
        named = [(aid, str(a.get("hostname") or "")) for aid, a in agents.items() if a.get("status") == "approved"]
        want = str(host).strip().lower() if host else None
        if want and want not in {h.lower() for _aid, h in named}:
            return {"error": "unknown host"}
        picked = [(aid, hn) for aid, hn in named if not want or hn.lower() == want]
        if not model:
            return {"hosts": [{"host": hn, "models": [
                {"model": mid, "active": e.get("active"), "profiles": list((e.get("profiles") or {}).keys())}
                for mid, e in (store.get_agent(aid) or {}).items()]}
                for aid, hn in picked if store.get_agent(aid)]}
        rows = []
        for aid, hn in picked:
            models_ = store.get_agent(aid) or {}
            mid = _match_model(models_, str(model))
            if mid is not None:
                rows.append(_profile_row(hn, models_[mid], mid, profile))
        if want:
            return rows[0] if rows else {"error": "unknown model"}
        return {"rows": rows}

    def config_get(path):
        entry = next((e for e in settings_catalog.CATALOG if e["path"] == path), None)
        if entry is None:
            raise ValueError("not a setting")
        if entry.get("secret"):
            return {"path": path, "value": _SECRET_MASK, "secret": True}
        d = settings_catalog.describe()
        return {"path": path, "value": d["values"].get(path, d.get("defaults", {}).get(path)), "help": entry["help"]}

    def _cap_key(provider):
        spec = providers_mod.get(provider)
        return spec.capability_key if spec else provider

    def _llama_fresh(agent) -> "tuple[bool, str]":
        """(fresh, reason): whether the host's llama sample is recent and the server answered it (#967)."""
        import autopilot
        import provider_state
        snap = provider_state.STORE.get("llama", agent["agent_id"]) or {}
        last_seen = snap.get("last_seen")
        age = (time.time() - float(last_seen)) if last_seen else None
        if age is None or age > provider_state.STALE_AFTER_S:
            return False, "stale"
        if not autopilot._llama_answered(snap.get("sample") or {}):
            return False, "unknown"
        return True, ""

    def _agent_post(host, provider, path, *, need_fresh=False, **kw):
        agent = _agent_by_hostname(host)
        if not agent:
            return False, "unknown host"
        if provider and not (agent.get("capabilities") or {}).get(_cap_key(provider)):
            return False, f"{host} does not serve {PROVIDER_LABEL.get(provider, provider)}"
        if need_fresh and provider == "llama":
            fresh, why = _llama_fresh(agent)
            if not fresh:
                log.info("tower act skipped host=%s reason=llama sample %s", host, why)
                return False, f"skipped: the llama sample from {host} is {why}; try again shortly"
        ok, err = discord_bot._agent_call(agent, "POST", path, **kw)
        return ok, (None if ok else scrub_agent_error(err, host))

    def load(provider, host, model):
        return _agent_post(host, provider, f"/{provider}/load", json={"model": model}, timeout=120, need_fresh=True)

    def unload(provider, host, model):
        return _agent_post(host, provider, f"/{provider}/unload", json={"model": model}, timeout=60)

    def wake(host):
        return _agent_post(host, "llama", "/llama/server/wake", timeout=75, need_fresh=True)

    def restart(provider, host):
        return _agent_post(host, provider, f"/{provider}/server/restart", timeout=60)

    def pinned(provider, host, model):
        import agent_registry
        try:
            agent = agent_registry.pinned_agent(provider, model)
        except Exception:  # noqa: BLE001 — an unreadable registry never confirms a pin
            return False
        return bool(agent) and str(agent.get("hostname") or "").lower() == str(host or "").lower()

    return {
        "hosts": hosts_overview, "host": host_detail, "host_history": host_history,
        "models": models, "profiles": profiles,
        "alarms": alarms, "alarm_search": alarm_search, "alarm_history": alarm_history, "alert": alert,
        "energy": energy_summary, "flow": gateway.flow_payload,
        "runs": tools_runs, "speed": speed_table, "health": service_health,
        "log_tail": log_tail, "config_get": config_get, "help": default_help, "audit": audit_rows,
        "load": load, "unload": unload, "wake": wake, "restart": restart, "ack": base["ack"], "close": base["close"],
        "pinned": pinned,
    }
