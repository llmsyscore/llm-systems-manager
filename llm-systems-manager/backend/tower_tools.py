"""Tower tool registry (#924): a static, schema-validated allowlist of read
tools with capped results. No shell/file/HTTP/config-write tool; no runtime registration."""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
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
    options: Optional[Callable[[dict], list]] = None    # act tools: option chips the approval card offers
    precheck: Optional[Callable[[dict], Optional[str]]] = None    # act tools: a reason to skip the action, else None


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


WAIT_EVERY_S = 5.0
HEARTBEAT_EVERY_S = 60.0
WAIT_MAX_S = 600
MONITOR_LIVE_S = 900
MONITOR_CARD_S = 1800
_TURN = threading.local()


def turn_begin(emit: Callable[[dict], None], cancelled: Callable[[], bool], heartbeat: Optional[Callable[[], None]] = None) -> None:
    """Binds this thread's turn so waiting tools can report progress, stop on cancel and keep the model awake."""
    _TURN.emit, _TURN.cancelled, _TURN.heartbeat = emit, cancelled, heartbeat


def turn_end() -> None:
    _TURN.emit = _TURN.cancelled = _TURN.heartbeat = None


def wait_for(check: Callable[[], Any], *, timeout_s: float, label: str, every_s: Optional[float] = None) -> "tuple[Any, int, str]":
    """Polls check() until it returns a truthy value: (value, waited_s, ok|timeout|cancelled). Emits a waiting status
    each round and a model heartbeat every HEARTBEAT_EVERY_S."""
    emit = getattr(_TURN, "emit", None) or (lambda ev: None)
    cancelled = getattr(_TURN, "cancelled", None) or (lambda: False)
    heartbeat = getattr(_TURN, "heartbeat", None)
    every_s = WAIT_EVERY_S if every_s is None else every_s
    t0 = time.monotonic()
    last_beat = t0
    while True:
        waited = int(time.monotonic() - t0)
        if cancelled():
            return None, waited, "cancelled"
        try:
            val = check()
        except Exception as e:  # noqa: BLE001 — a failed poll is a miss, not the end of the wait
            log.debug("tower wait poll failed (%s): %s", label, type(e).__name__)
            val = None
        if val:
            return val, waited, "ok"
        if waited >= timeout_s:
            return None, waited, "timeout"
        emit({"event": "status", "state": "waiting", "name": label, "elapsed_s": waited, "timeout_s": int(timeout_s)})
        if heartbeat is not None and time.monotonic() - last_beat >= HEARTBEAT_EVERY_S:
            last_beat = time.monotonic()
            try:
                heartbeat()
            except Exception as e:  # noqa: BLE001 — a missed heartbeat never ends the wait
                log.debug("tower heartbeat failed: %s", type(e).__name__)
        time.sleep(max(0.0, min(every_s, timeout_s - (time.monotonic() - t0))))


def wait_result(what: str, target: str, val: Any, waited: int, how: str) -> dict:
    """The wait_until tool's result: the data when the condition held, else a plain reason."""
    if how == "ok":
        return {"ok": True, "waited_s": waited, "what": what, "target": target, "result": val}
    reason = "the wait was stopped" if how == "cancelled" else f"still not there after {waited} s; check again later"
    return {"ok": False, "waited_s": waited, "what": what, "target": target, "message": reason}


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


def apply_options(card: dict, chosen) -> "tuple[dict, Optional[str]]":
    """The operator's picks for a card's option chips, each checked against the offered choices; (values, error)."""
    offered = {str(o.get("name")): o for o in ((card or {}).get("options") or []) if isinstance(o, dict) and o.get("name")}
    out: dict = {}
    for name, o in offered.items():
        raw = (chosen or {}).get(name, o.get("value")) if isinstance(chosen, dict) else o.get("value")
        val = str(raw) if raw is not None else ""
        allowed = [str(c.get("value") if isinstance(c, dict) else c) for c in (o.get("choices") or [])]
        if not allowed:
            continue
        if val not in allowed:
            return {}, f"{o.get('label') or name}: {val!r} is not one of the offered choices"
        out[name] = val
    return out, None


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
    "notification": "Alert notifications are configured in the Events tab › Settings › Notifications (the alarm console's "
                    "own settings page): the Channels section holds email, webhook, Discord, SMS and toast; the Policies "
                    "section decides which alerts go to which channel, with dwell and cooldown. Nothing about notifications "
                    "lives under Admin.",
    "channel": "Notification channels (email, webhook, Discord, SMS, toast) are managed in the Events tab › Settings › "
               "Notifications, Channels section; each channel is enabled separately and must be routed by a policy to send anything.",
    "policy": "Notification policies live in the Events tab › Settings › Notifications, Policies section: which rules or "
              "severities route to which channels, dwell time before the first message and cooldown between repeats.",
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
HISTORY_POINTS_MAX = 120
HISTORY_GROUP_POINTS = 720
HISTORY_SINCE_MAX_MIN = 43200
HISTORY_AE_POINTS_MAX = 1500
_METRIC_HELP = "metric must be one of " + ", ".join(HISTORY_METRICS) + " or an alarm-engine source/name like system/cpu_total"


def history_metric(metric) -> "tuple[str, str, str, Optional[str]]":
    """(source, name, unit, error) for a named metric or an alarm-engine source/name pair (#992)."""
    m = str(metric or "").strip()
    if m in HISTORY_METRICS:
        source, name, unit = HISTORY_METRICS[m]
        return source, name, unit, None
    source, _, name = m.partition("/")
    if source and name:
        return source, name, metric_unit(source, name), None
    return "", "", "", _METRIC_HELP


def history_range(window, since=None, until=None, now: Optional[float] = None) -> "tuple[float, float, str, Optional[str]]":
    """(start, end, label, error): since/until win, then a named window, then a span like 4h (#992)."""
    now = time.time() if now is None else now
    if since or until:
        start, end, label, err = time_range(None, since, until, now)
        if err:
            return 0.0, 0.0, "", err
        start = now - 86400 if start is None else start
        end = now if end is None else end
        return (0.0, 0.0, "", "until must be after since") if end <= start else (start, end, label, None)
    w = str(window or "24h")
    if w in HISTORY_WINDOWS:
        return now - HISTORY_WINDOWS[w] * 60, now, w, None
    t = parse_when(w, now) if _RELATIVE.match(w) else None
    if t is None or t >= now:
        return 0.0, 0.0, "", "window must be one of " + ", ".join(HISTORY_WINDOWS) + ", a span like 4h, or use since/until"
    return t, now, f"last {w}", None


def history_buckets(vals: list, group_by: str) -> list:
    """(epoch, value) pairs grouped by local day or hour: min/avg/max and the sample count per bucket."""
    fmt = "%Y-%m-%d" if group_by == "day" else "%Y-%m-%d %H:00"
    groups: dict = {}
    for ts, v in vals:
        groups.setdefault(time.strftime(fmt, time.localtime(ts)), []).append(v)
    return [{"period": k, "min": round(min(g), 2), "avg": round(sum(g) / len(g), 2), "max": round(max(g), 2), "samples": len(g)}
            for k, g in sorted(groups.items())]
_SPARK = "▁▂▃▄▅▆▇█"


SNAPSHOT_MINUTES = 60
SNAPSHOT_POINTS = 40
ENERGY_SPANS = {"today": "today", "24h": 86400, "7d": 7 * 86400, "month": 30 * 86400}
ENERGY_BUCKET_CAP = {"day": 62, "hour": 72}
_ENERGY_KEYS = ("kwh", "cost_usd", "avg_watts", "tokens_gen", "tokens_prompt", "active_pct")


def energy_range(window: Optional[str], since=None, until=None, now: Optional[float] = None) -> "tuple[float, float, str, Optional[str]]":
    """(start, end, label, error) for the energy tool: since/until win; a window is today, 24h, 7d or month (30 days)."""
    now = time.time() if now is None else now
    if since or until:
        start, end, label, err = time_range(None, since, until, now)
        if err:
            return 0.0, 0.0, "", err
        start = now - 86400 if start is None else start
        end = now if end is None else end
        if end <= start:
            return 0.0, 0.0, "", "until must be after since"
        return start, end, label, None
    span = ENERGY_SPANS.get(window or "today", "today")
    if span == "today":
        lt = time.localtime(now)
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return start, now, "today", None
    return now - span, now, ("last 30 days" if window == "month" else f"last {window}"), None


def energy_buckets(rows: list, group_by: str) -> "list[tuple[str, list, float]]":
    """Hourly ledger rows grouped by local day or hour: [(label, rows, bucket_seconds)] in time order."""
    fmt, span = ("%Y-%m-%d", 86400.0) if group_by == "day" else ("%Y-%m-%d %H:00", 3600.0)
    groups: dict = {}
    for r in rows:
        key = time.strftime(fmt, time.localtime(float(r.get("hour_ts") or 0)))
        groups.setdefault(key, []).append(r)
    return [(k, groups[k], span) for k in sorted(groups)]


def metric_unit(source: str, name: str) -> str:
    return next((u for s, n, u in HISTORY_METRICS.values() if s == source and n == name), "")


def snapshot_from_points(points: list, alert: dict, minutes: int = SNAPSHOT_MINUTES) -> Optional[dict]:
    """The metric series behind an alert as [[epoch, value], …] with its unit, threshold and value (#980)."""
    metric = str((alert or {}).get("metric") or "")
    source, _, name = metric.partition("/")
    vals = []
    for p in points or []:
        ts, v = parse_ts((p or {}).get("timestamp")), (p or {}).get("value")
        if ts is not None and isinstance(v, (int, float)) and math.isfinite(v):
            vals.append([int(ts), round(float(v), 3)])
    if not vals:
        return None
    vals.sort()
    thr, cur = alert.get("threshold"), alert.get("value")
    return {"metric": metric, "unit": metric_unit(source, name), "minutes": int(minutes), "points": vals,
            "threshold": float(thr) if isinstance(thr, (int, float)) else None,
            "value": float(cur) if isinstance(cur, (int, float)) else None}


def history_summary(points: list, metric: str, window: str, host: str, unit: str = "", *, max_points: int = HISTORY_POINTS,
                    group_by: Optional[str] = None, start: Optional[float] = None, end: Optional[float] = None) -> dict:
    """min/avg/max, latest, trend, a sparkline and the bucketed series for one metric's history points;
    start/end narrow the points, group_by day|hour adds per-bucket stats."""
    vals = []
    for p in points or []:
        ts, v = parse_ts((p or {}).get("timestamp")), (p or {}).get("value")
        if ts is not None and isinstance(v, (int, float)) and math.isfinite(v):
            if (start is not None and ts < start) or (end is not None and ts > end):
                continue
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
            "series": [{"t": local_ts(ts), "v": round(x, 2)} for ts, x in vals[-max(1, int(max_points)):]],
            **({"groups": history_buckets(vals, group_by)} if group_by in ("day", "hour") else {})}


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


AUDIT_COUNT_MAX = 200
_SECRET_KEY_RE = re.compile(r"(token|password|passwd|secret|api_key|apikey|private_key|credential)", re.I)
_CHANNEL_SECRET_RE = re.compile(r"(token|password|passwd|secret|api_key|apikey|private_key|credential|url|headers|authorization|auth_)", re.I)


def mask_secrets(obj, key_re=None):
    """A copy of a config mapping with every secret-looking key's value masked, at any depth; key_re widens the match."""
    key_re = key_re or _SECRET_KEY_RE
    if isinstance(obj, dict):
        return {k: (_SECRET_MASK if key_re.search(str(k)) and v not in (None, "", [], {}) else mask_secrets(v, key_re)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_secrets(v, key_re) for v in obj]
    return obj


def audit_status_clause(status) -> "tuple[Optional[str], list]":
    """(sql, params) for an audit status filter: an exact code like 403 or a class like 4xx; (None, []) when blank or bad."""
    s = str(status or "").strip().lower()
    if re.fullmatch(r"[1-5]xx", s):
        lo = int(s[0]) * 100
        return "status >= ? AND status < ?", [lo, lo + 100]
    if re.fullmatch(r"\d{3}", s):
        return "status = ?", [int(s)]
    return None, []


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
                   "log_tail", "config_get", "help", "support", "wait_until", "audit_log")
ACT_TOOL_NAMES = ("load_model", "unload_model", "wake_server", "restart_provider", "start_benchmark", "ack_alert", "close_alert",
                  "resume_alert")
WAIT_KINDS = ("host_awake", "model_loaded", "model_ready", "run_done", "reportcard_done")
BENCH_KINDS = ("live", "reportcard")
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


WAKE_TIMEOUT_S = 320    # longer than the agent's 300 s warm-up read timeout


def _no_bench(a: dict) -> dict:
    return {"ok": False, "message": "benchmarks are not wired"}


def alert_precheck(deps: dict, verb: str, alert_id: str) -> Optional[str]:
    """Reason an ack/close should not run, from the alert's current status; None when it may proceed."""
    fn = deps.get("alert")
    if fn is None:
        return None
    try:
        row = fn(alert_id)
    except Exception as e:  # noqa: BLE001 — an unreadable alarm engine still lets the action try
        log.warning("tower alert precheck failed: %s", type(e).__name__)
        return None
    if not row:
        return f"alert {alert_id} was not found"
    status = str(row.get("status") or "")
    if verb == "resume":
        return None if status == "ignored" else f"alert {alert_id} is {status or 'not ignored'}; there is no ignore window to end"
    if status == "closed":
        return f"alert {alert_id} is already closed; nothing to {verb}"
    if verb == "acknowledge" and status == "acknowledged":
        return f"alert {alert_id} is already acknowledged"
    if verb == "acknowledge" and status == "ignored":
        until = row.get("ignored_until") or "later"
        return f"alert {alert_id} is ignored until {until}; end the ignore window first (resume_alert) or close it"
    return None


def wait_check(deps: dict, what: str, a: dict) -> "tuple[Callable[[], Any], str]":
    """(check, target) for a wait_until kind; check returns the data once the condition holds."""
    host, model = str(a.get("host") or ""), str(a.get("model") or "")
    if what == "host_awake":
        def check():
            row = deps["host"](host, "all") or {}
            states = row.get("provider_states") or {}
            return row if str(states.get("llama.cpp") or states.get("llama") or "").lower() == "awake" else None
        return check, host
    if what == "model_loaded":
        def check():
            row = deps["host"](host, "all") or {}
            states = row.get("provider_states") or {}
            if str(states.get("llama.cpp") or states.get("llama") or "").lower() != "awake":
                return None
            rows = deps["models"](host, None)
            rows = rows.get("models") if isinstance(rows, dict) else rows
            hit = [r for r in rows or [] if r.get("loaded") and model.lower() in str(r.get("model") or "").lower()]
            return hit[0] if hit else None
        return check, f"{model} on {host}"
    if what == "model_ready":
        return (lambda: (deps.get("probe") or (lambda _h, _m: None))(host, model)), f"{model} on {host}"
    if what == "run_done":
        rid = str(a.get("run_id") or "")
        return (lambda: (deps.get("run_result") or (lambda _r: None))(rid)), rid
    jid = str(a.get("job_id") or "")
    return (lambda: (deps.get("card_result") or (lambda _j: None))(jid)), jid


def wait_until(deps: dict, a: dict) -> dict:
    what = str(a.get("what") or "")
    need = {"host_awake": ("host",), "model_loaded": ("host", "model"), "model_ready": ("host", "model"), "run_done": ("run_id",),
            "reportcard_done": ("job_id",)}
    missing = [k for k in need.get(what, ()) if not a.get(k)]
    if missing:
        return {"ok": False, "message": f"{', '.join(missing)} required for {what}"}
    check, target = wait_check(deps, what, a)
    timeout = max(1, min(int(a.get("timeout_s") or 300), WAIT_MAX_S))
    val, waited, how = wait_for(check, timeout_s=timeout, label=f"{what.replace('_', ' ')} · {target}")
    return wait_result(what, target, val, waited, how)


def build_registry(deps: dict) -> "dict[str, Tool]":
    """Read tools plus the six act tools; every callable comes from `deps` (injected)."""
    def config_get(a):
        path = str(a.get("path") or "")
        if a.get("host"):
            return deps["agent_config"](a["host"], path or None)
        if path.startswith("alarm."):
            return (deps.get("alarm_config") or (lambda _p: {"error": "alarm engine reads are not wired"}))(path)
        if not path.startswith(("manager.", "alarm_engine.", "openclaw.", "influxdb.", "notifications.", "logging.")):
            raise ValueError("not a setting (give a dotted manager/alarm-engine path, alarm.rules / alarm.channels / "
                             "alarm.policies / alarm.settings, or host for an agent's configuration)")
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
        Tool("host_history", "One metric's history: min/avg/max, latest, trend, a sparkline and up to `points` bucketed points "
             "(default 24). Several hosts (comma-separated) or all return one summary per host, plus each series when "
             "series is true. since/until or a span like 4h replace the window; group_by adds per-day or per-hour stats.",
             _obj({"host": {"type": "string"},
                   "metric": {"type": "string", "description": "One of " + ", ".join(HISTORY_METRICS) + ", or an alarm-engine source/name such as system/cpu_total"},
                   "window": {"type": "string", "default": "24h", "description": "1h, 6h, 24h, 7d, 30d or a span like 4h"},
                   "since": {"type": "string", "description": "Range start: 4h, 3d, a date or ISO-8601 (overrides window)"},
                   "until": {"type": "string", "description": "Range end, same forms as since (default now)"},
                   "points": {"type": "integer", "minimum": 1, "maximum": HISTORY_POINTS_MAX, "default": HISTORY_POINTS},
                   "series": {"type": "boolean", "default": False},
                   "group_by": {"type": "string", "enum": ["day", "hour"]}}, ["host", "metric"]),
             "read", "read", lambda a: deps["host_history"](a["host"], a["metric"], a.get("window", "24h"), a)),
        Tool("models", "Models on every host: loaded now (loaded_on) and available to load (available_on), per provider.",
             _obj({"host": {"type": "string"}, "provider": {"type": "string", "enum": ["llama", "lms", "vllm"]}}), "read", "read",
             lambda a: deps["models"](a.get("host"), a.get("provider"))),
        Tool("model_profiles", "Saved llama-server config profiles per host and model (host=name, a comma-separated "
             "list, or all): the active profile, the other profile names, and (when a model is given) the active or "
             "named profile's values.",
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
        Tool("energy_summary", "Energy and cost for a window or a since/until range: fleet totals plus one row per host "
             "(kWh, cost, average watts, tokens). group_by adds a per-day or per-hour breakdown; host narrows to one host.",
             _obj({"window": {"type": "string", "enum": ["today", "24h", "7d", "month"], "default": "today"},
                   "since": {"type": "string", "description": "Range start: 3d, 2h, a date or ISO-8601 (overrides window)"},
                   "until": {"type": "string", "description": "Range end, same forms as since (default now)"},
                   "group_by": {"type": "string", "enum": ["day", "hour"]},
                   "host": {"type": "string"}}), "read", "read",
             lambda a: deps["energy"](a.get("window") or "today", a),
             summary=lambda a, r: " · ".join(x for x in ((r.get("window") if isinstance(r, dict) else None) or a.get("window") or "today",
                                                      f"by {a['group_by']}" if a.get("group_by") else "", a.get("host") or "") if x)),
        Tool("gateway_flow", "Gateway clients, hosts, throughput and in-flight requests right now.", _obj({}), "read", "read",
             lambda a: deps["flow"]()),
        Tool("recent_runs", "Recent Report Card / benchmark / autotune / quality runs, newest first, each with its host, "
             "configuration (bench set, output length, samples, concurrency; objective and mode; provider and preset) and "
             "results. run_id returns that one run in full; host filters by host.",
             _obj({"tool": {"type": "string", "enum": ["reportcard", "benchmark", "autotune", "quality"]},
                   "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                   "run_id": {"type": "string"}, "host": {"type": "string"}}), "read", "read",
             lambda a: deps["runs"](a.get("tool"), a["count"], a)),
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
             "alarm.rules, alarm.channels, alarm.policies and alarm.settings list the alarm engine's rules, notification "
             "channels, notification policies and settings (add .<name or id> for one). Not model configs — use model_profiles.",
             _obj({"path": {"type": "string"}, "host": {"type": "string", "description": "An agent hostname: returns that agent's configuration (secrets masked); path then narrows to one dotted key"}}),
             "read", "read", config_get, role="admin"),
        Tool("help", "Explains a dashboard concept or feature: a short note when one exists, plus the best-matching "
             "sections of the product docs (README, API reference, architecture, components, deployment). doc narrows "
             "the search to one document.",
             _obj({"topic": {"type": "string"}, "doc": {"type": "string", "enum": list(DOCS)}}, ["topic"]), "read", "read",
             lambda a: help_answer(a["topic"], deps, a.get("doc"))),
        Tool("support", "Where to get help with LLM Systems Manager: the developer (llmsyscore), website, repository, "
             "issue tracker, docs, and what to include in a help request. Use it when the operator asks for help or "
             "support, or when you cannot fully answer a question.", _obj({}), "read", "read",
             lambda a: support_info()),
        Tool("wait_until", "Waits for a slow operation to finish, polling every few seconds up to timeout_s (default 300, "
             "max 600), then returns the current data: host_awake (host), model_loaded (host, model: awake with the model "
             "resident), model_ready (host, model: the model answered a one-token prompt on that host), run_done (run_id of "
             "a live bench), reportcard_done (job_id). Use it after waking or loading a model or starting a benchmark when "
             "the operator wants the outcome, not just the start.",
             _obj({"what": {"type": "string", "enum": list(WAIT_KINDS)}, "host": {"type": "string"}, "model": {"type": "string"},
                   "run_id": {"type": "string"}, "job_id": {"type": "string"},
                   "timeout_s": {"type": "integer", "minimum": 1, "maximum": WAIT_MAX_S, "default": 300}}, ["what"]),
             "read", "read", lambda a: wait_until(deps, a),
             summary=lambda a, r: f"{str(a.get('what') or '').replace('_', ' ')} · {(r or {}).get('target') or '-'} · "
                                  f"{'ready' if (r or {}).get('ok') else 'not yet'} after {(r or {}).get('waited_s', 0)} s"),
        Tool("audit_log", "Audit-log entries, newest first, with total and next_offset for paging: who did what and when. "
             "Filter by actor, action prefix, a window or since/until, free-text search, outcome and HTTP status.",
             _obj({"window": {"type": "string", "enum": ["1h", "24h", "7d", "30d", "90d"], "default": "24h"},
                   "since": {"type": "string", "description": "Range start: 2h, 3d, a date or ISO-8601 (overrides window)"},
                   "until": {"type": "string", "description": "Range end, same forms as since"},
                   "actor": {"type": "string"}, "action": {"type": "string"},
                   "search": {"type": "string", "description": "Case-insensitive text over actor, action, target and detail"},
                   "outcome": {"type": "string", "enum": ["ok", "denied", "error"]},
                   "status": {"type": "string", "description": "An HTTP status such as 403, or a class such as 4xx"},
                   "count": {"type": "integer", "minimum": 1, "maximum": AUDIT_COUNT_MAX, "default": 20},
                   "offset": {"type": "integer", "minimum": 0, "default": 0}}), "read", "read",
             lambda a: deps["audit"](a.get("window", "24h"), a.get("actor"), a.get("action"), a.get("count", 20), a), role="admin"),
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
        Tool("start_benchmark", "Start a live benchmark or a report card on a host with a model it has loaded (asks the operator "
             "first; the approval card offers the bench set, output length and samples, or the provider).",
             _obj({"kind": {"type": "string", "enum": list(BENCH_KINDS)}, "host": {"type": "string"}, "model": {"type": "string"},
                   "provider": _PROVIDER_ENUM}, ["kind", "host", "model"]),
             "act", "operate", lambda a: (deps.get("bench_start") or _no_bench)(a),
             options=lambda a: (deps.get("bench_options") or (lambda _a: []))(a)),
        Tool("ack_alert", "Acknowledge an alert in the alarm engine (asks the operator first). Checks the alert's "
             "current status first: an already acknowledged or closed alert is reported, not re-actioned.",
             _obj({"alert_id": {"type": "string"}}, ["alert_id"]), "act", "operate", lambda a: _act(deps, "ack_alert", "ack", a["alert_id"]),
             precheck=lambda a: alert_precheck(deps, "acknowledge", a["alert_id"])),
        Tool("close_alert", "Close an alert in the alarm engine (asks the operator first). Checks the alert's current "
             "status first: an already closed alert is reported, not re-actioned.",
             _obj({"alert_id": {"type": "string"}}, ["alert_id"]), "act", "operate", lambda a: _act(deps, "close_alert", "close", a["alert_id"]),
             precheck=lambda a: alert_precheck(deps, "close", a["alert_id"])),
        Tool("resume_alert", "End an ignored alert's ignore window early (asks the operator first): the alert returns to "
             "active and its rule resumes; this is not a close.",
             _obj({"alert_id": {"type": "string"}}, ["alert_id"]), "act", "operate", lambda a: _act(deps, "resume_alert", "resume", a["alert_id"]),
             precheck=lambda a: alert_precheck(deps, "resume", a["alert_id"])),
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
        "start_benchmark": ("Start a live bench" if args.get("kind") == "live" else "Start a report card", f"{host} · {model}",
                            "Runs the benchmark on that host against the model it has loaded; results land in the Benchmark tab and recent_runs.",
                            "Nothing is loaded or unloaded; the host answers more slowly while it runs."),
        "ack_alert": (f"Acknowledge alert {aid}", f"alert {aid}", "Marks the alert acknowledged in the alarm engine.",
                      "The rule keeps evaluating; nothing on a host changes."),
        "resume_alert": (f"End the ignore window of alert {aid}", f"alert {aid}", "The alert returns to active and its rule resumes evaluating.",
                         "Does not close the alert."),
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


DOCS = {"readme": "README.md", "api": "docs/API_REFERENCE.md", "architecture": "docs/ARCHITECTURE.md",
        "components": "docs/COMPONENTS.md", "deployment": "docs/DEPLOYMENT.md"}
DOCS_ROOT = Path(__file__).resolve().parents[2]
DOC_HITS = 3
DOC_SNIPPET = 700
_DOC_CACHE: "dict[str, tuple[float, list]]" = {}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.+/-]{1,}")
_STOP = {"the", "and", "for", "with", "how", "what", "does", "this", "that", "are", "you", "can", "use", "from", "into", "when"}

SUPPORT = {"developer": "llmsyscore", "website": "https://www.llmsyscore.com", "email": "support@llmsyscore.com",
           "repository": "https://github.com/llmsyscore/llm-systems-manager",
           "issues": "https://github.com/llmsyscore/llm-systems-manager/issues",
           "docs": "https://github.com/llmsyscore/llm-systems-manager/tree/main/docs"}


def support_info() -> dict:
    return {**SUPPORT, "how": "Email support, open an issue on the repository for bugs and questions, or use the website's contact page.",
            "include": ["what you tried and what happened", "manager, alarm engine and agent versions (service_health)",
                        "host OS and provider (llama.cpp, LM Studio, vLLM)", "the relevant log lines (log_tail)"]}


def doc_sections(text: str) -> list:
    """[(heading path, body)] from Markdown: every heading opens a section; the path keeps the parent headings."""
    out, stack, body = [], [], []
    head = ""
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            if head or body:
                out.append((head, "\n".join(body).strip()))
            level, title = len(m.group(1)), m.group(2).strip()
            stack = stack[: level - 1] + [title]
            head, body = " › ".join(s for s in stack if s), []
        else:
            body.append(line)
    if head or body:
        out.append((head, "\n".join(body).strip()))
    return [(h, b) for h, b in out if b]


def _doc_load(name: str, root: Path) -> list:
    path = root / DOCS[name]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    hit = _DOC_CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        secs = doc_sections(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    _DOC_CACHE[str(path)] = (mtime, secs)
    return secs


def docs_search(query: str, doc: Optional[str] = None, limit: int = DOC_HITS, root: Optional[Path] = None) -> dict:
    """Best-matching doc sections for the query terms: {hits: [{doc, section, text}], searched: [...]}."""
    root = root or DOCS_ROOT
    terms = [t for t in _WORD_RE.findall(str(query or "").lower()) if t not in _STOP]
    names = [doc] if doc in DOCS else list(DOCS)
    if not terms:
        return {"hits": [], "searched": [DOCS[n] for n in names], "note": "no search terms"}
    scored = []
    for name in names:
        for head, body in _doc_load(name, root):
            low, hl = body.lower(), head.lower()
            score = sum((3 if t in hl else 0) + min(low.count(t), 5) for t in terms)
            if score:
                scored.append((score, name, head, body))
    scored.sort(key=lambda s: -s[0])
    hits = [{"doc": DOCS[n], "section": h, "text": b[:DOC_SNIPPET] + ("…" if len(b) > DOC_SNIPPET else "")}
            for _s, n, h, b in scored[: max(1, min(int(limit or DOC_HITS), 5))]]
    return {"hits": hits, "searched": [DOCS[n] for n in names]}


def help_answer(topic: str, deps: dict, doc: Optional[str] = None) -> dict:
    """The help tool's result: the built-in note (when one matches) plus matching doc sections."""
    note = (deps.get("help") or default_help)(topic)
    out: dict = {"topic": topic}
    if not note.startswith("No note for"):
        out["note"] = note
    fn = deps.get("docs")
    if fn is not None:
        try:
            out.update(fn(topic, doc))
        except Exception as e:  # noqa: BLE001 — the note still answers when the docs cannot be read
            log.warning("tower docs search failed: %s", type(e).__name__)
            out["note_docs"] = "the product docs could not be searched"
    if "note" not in out and not out.get("hits"):
        out["note"] = note
    return out


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
        hosts = here(e.get("catalog_hosts") or e.get("hosts") or [])
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
              audit_rows: Callable[[str, Optional[str], Optional[str], int], list],
              bench_start: Optional[Callable[[dict], dict]] = None,
              bench_options: Optional[Callable[[dict], list]] = None,
              card_result: Optional[Callable[[str], Optional[dict]]] = None) -> dict:
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
                "ignored_until": local_ts(a.get("ignored_until") or None), "incident": a.get("incident_id") or None}

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

    def _history_one(host, metric, window, a):
        agent = _agent_by_hostname(host)
        if not agent:
            return {"host": host, "error": "unknown host"}
        hostname = agent.get("hostname") or host
        source, name, unit, err = history_metric(metric)
        if err:
            return {"host": hostname, "error": err}
        now = time.time()
        start, end, label, err = history_range(window, a.get("since"), a.get("until"), now)
        if err:
            return {"host": hostname, "error": err}
        note = None
        since_min = int(math.ceil(round((now - start) / 60, 3)))
        if since_min > HISTORY_SINCE_MAX_MIN:
            since_min, start = HISTORY_SINCE_MAX_MIN, now - HISTORY_SINCE_MAX_MIN * 60
            if end <= start:
                return {"host": hostname, "error": "the alarm engine keeps 30 days of history; that range is older than that"}
            note = "the alarm engine keeps 30 days of history; the range was clipped"
        group_by = a.get("group_by") if a.get("group_by") in ("day", "hour") else None
        points = max(1, min(int(a.get("points") or HISTORY_POINTS), HISTORY_POINTS_MAX))
        want = HISTORY_GROUP_POINTS if group_by else points
        frac = max(0.05, (end - start) / max(1.0, now - start))
        q = urllib.parse.urlencode({"since_minutes": max(1, since_min), "hostname": hostname,
                                    "max_points": min(HISTORY_AE_POINTS_MAX, int(math.ceil(want / frac))), "agg": "mean"})
        r = _ae(f"/api/alarm/metrics/{urllib.parse.quote(source, safe='')}/{urllib.parse.quote(name, safe='')}?{q}")
        if not r.ok:
            return {"host": hostname, "error": "history unavailable from the alarm engine"}
        body = r.json()
        out = history_summary(body if isinstance(body, list) else [], str(metric), label, hostname, unit,
                              max_points=points, group_by=group_by, start=start, end=end)
        if note:
            out["note"] = note
        return out

    def host_history(host, metric, window="24h", a=None):
        a = a or {}
        names = host_names(host, _known_hosts())
        if not names:
            return {"error": "unknown host"}
        if len(names) == 1:
            return _history_one(names[0], metric, window, a)
        per, label = [], str(window or "24h")
        for n in names:
            one = _history_one(n, metric, window, a)
            if not a.get("series"):
                one.pop("series", None)
            label = one.pop("window", label)
            per.append({k: v for k, v in one.items() if k != "metric"})
        return {"metric": str(metric), "window": label, "unit": history_metric(metric)[2], "hosts": per}

    def alert(aid):
        r = _ae(f"/api/alarm/alerts/{urllib.parse.quote(str(aid), safe='')}")
        return _alert_row(r.json()) if r.ok else None

    _ALARM_PATHS = {"rules": "/api/alarm/rules", "channels": "/api/alarm/notifications/channels",
                    "policies": "/api/alarm/notifications/configs", "settings": "/api/alarm/admin/config"}

    def alarm_config(path):
        """alarm.<rules|channels|policies|settings>[.<name or id>] read from the alarm engine, secrets masked (#1021)."""
        parts = str(path or "").split(".", 2)
        kind = parts[1].strip().lower() if len(parts) > 1 else ""
        want = parts[2].strip().lower() if len(parts) > 2 else ""
        if kind not in _ALARM_PATHS:
            return {"error": "alarm paths are alarm.rules, alarm.channels, alarm.policies or alarm.settings"}
        r = _ae(_ALARM_PATHS[kind])
        if not r.ok:
            return {"kind": kind, "error": f"alarm engine returned HTTP {r.status_code}"}
        body = r.json()
        if kind == "settings":
            sections = body.get("sections") if isinstance(body, dict) else None
            data = sections if isinstance(sections, dict) else body
            if want:
                data = {k: v for k, v in (data or {}).items() if k.lower() == want or k.lower().startswith(want)}
            return {"kind": kind, "settings": mask_secrets(data)}
        rows = body if isinstance(body, list) else (body.get("items") or body.get("rules") or []) if isinstance(body, dict) else []
        if want:
            rows = [x for x in rows if want in {str(x.get(k) or "").lower() for k in ("id", "name", "rule_id", "channel_id", "config_id")}
                    or want in str(x.get("name") or "").lower()]
        rows = rows[:100]
        return {"kind": kind, "count": len(rows), kind: mask_secrets(rows, _CHANNEL_SECRET_RE if kind == "channels" else None)}

    def metric_snapshot(alert_row, minutes=SNAPSHOT_MINUTES):
        source, _, name = str((alert_row or {}).get("metric") or "").partition("/")
        if not source or not name:
            return None
        q = {"since_minutes": int(minutes), "max_points": SNAPSHOT_POINTS, "agg": "mean"}
        if alert_row.get("host"):
            q["hostname"] = str(alert_row["host"])
        r = _ae(f"/api/alarm/metrics/{urllib.parse.quote(source, safe='')}/{urllib.parse.quote(name, safe='')}?{urllib.parse.urlencode(q)}")
        if not r.ok:
            return None
        body = r.json()
        return snapshot_from_points(body if isinstance(body, list) else [], alert_row, minutes)

    def energy_summary(window="today", a=None):
        a = a or {}
        now = time.time()
        start, end, label, err = energy_range(window, a.get("since"), a.get("until"), now)
        if err:
            return {"error": err}
        cfg = energy._cfg_energy(ctx)
        factory = energy._conn_factory
        if factory is None:
            return {"window": label, "error": "energy accounting not started"}
        qs, qe = int(start // 3600) * 3600, int(math.ceil(end / 3600)) * 3600
        rows = energy.query_rows(factory(), qs, qe)
        host = str(a.get("host") or "").strip()
        if host:
            rows = [r for r in rows if str(r.get("hostname") or "").lower() == host.lower()]
        prices = (cfg["price_kwh"], cfg["cloud_price_in_per_mtok"], cfg["cloud_price_out_per_mtok"])
        s = energy.summarize(rows, max(0.0, min(qe, now) - qs), *prices)
        out = {"window": label, "start": local_ts(qs), "end": local_ts(min(qe, now)), "totals": s.get("totals"), "hosts": s.get("hosts")}
        if qs != start or qe != end:
            out["note"] = "the ledger is hourly, so the range was widened to whole hours"
        if host:
            out["host"] = host
            if not rows:
                out["note"] = f"no energy rows for {host} in this range"
        gb = a.get("group_by")
        if gb in ENERGY_BUCKET_CAP:
            buckets = energy_buckets(rows, gb)
            cap = ENERGY_BUCKET_CAP[gb]
            if len(buckets) > cap:
                out["note"] = f"{len(buckets)} {gb}s in range; showing the last {cap}"
                buckets = buckets[-cap:]
            out["breakdown"] = [{"period": k, **{key: energy.summarize(rs, span, *prices)["totals"].get(key) for key in _ENERGY_KEYS}}
                                for k, rs, span in buckets]
        return out

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
        wanted = [h.lower() for h in host_names(host, [hn for _aid, hn in named])]
        bad = [h for h in wanted if h not in {hn.lower() for _aid, hn in named}]
        if bad:
            return {"error": "unknown host: " + ", ".join(bad)}
        want = wanted[0] if len(wanted) == 1 else None
        picked = [(aid, hn) for aid, hn in named if not wanted or hn.lower() in wanted]

        def _known(hn):
            """Model ids the host has now (loaded or available), by the models reader; None when it cannot say."""
            try:
                rows_ = deps_out["models"](hn)
            except Exception:  # noqa: BLE001 — an unreadable host keeps the saved entries
                return None
            rows_ = rows_.get("models") if isinstance(rows_, dict) else rows_
            ids = set()
            for r in rows_ or []:
                if hn.lower() in [str(h).lower() for h in (r.get("loaded_on") or []) + (r.get("available_on") or [])]:
                    ids.add(str(r.get("model")))
            return ids or None

        def _entries(aid, hn):
            """(present, stale, verified) profile entries for a host: smoke-test artefacts dropped, absent models flagged;
            verified is False when the host reported no models, so nothing could be checked (#1001)."""
            saved = {m: e for m, e in (store.get_agent(aid) or {}).items() if not str(m).startswith("smoke-test")}
            known = _known(hn)
            if known is None:
                return saved, {}, False
            return {m: e for m, e in saved.items() if m in known}, {m: e for m, e in saved.items() if m not in known}, True

        _UNVERIFIED = "the host reported no models, so these saved entries could not be checked against what it has"

        if not model:
            hosts_out = []
            for aid, hn in picked:
                present, stale, verified = _entries(aid, hn)
                if not present and not stale:
                    continue
                row = {"host": hn, "models": [{"model": mid, "active": e.get("active"), "profiles": list((e.get("profiles") or {}).keys())}
                                              for mid, e in present.items()]}
                if not verified:
                    row["verified"] = False
                    row["note"] = _UNVERIFIED
                if stale:
                    row["stale"] = sorted(stale)
                    row["note"] = "stale entries are saved profiles for models the host no longer has"
                hosts_out.append(row)
            return {"hosts": hosts_out}
        rows = []
        for aid, hn in picked:
            present, stale, verified = _entries(aid, hn)
            mid = _match_model(present, str(model))
            if mid is not None:
                row = _profile_row(hn, present[mid], mid, profile)
                rows.append(row if verified or "error" in row else {**row, "verified": False, "note": _UNVERIFIED})
                continue
            mid = _match_model(stale, str(model))
            if mid is not None:
                rows.append({**_profile_row(hn, stale[mid], mid, profile), "present": False,
                             "note": "the host no longer has this model; the profile is a leftover"})
        if want:
            return rows[0] if rows else {"error": "unknown model"}
        return {"rows": rows}

    def agent_config(host, path=None):
        import agent_registry
        agent = _agent_by_hostname(host)
        if not agent:
            return {"host": host, "error": "unknown host"}
        hostname = agent.get("hostname") or host
        r, _tried, err = agent_registry.agent_request("GET", agent, "/config", timeout=10,
                                                      headers={"Authorization": f"Bearer {agent.get('token') or ''}"})
        if r is None or not r.ok:
            return {"host": hostname, "error": f"could not reach the agent on {hostname}"}
        try:
            cfg = r.json()
        except ValueError:
            return {"host": hostname, "error": "the agent returned no configuration"}
        cfg = mask_secrets(cfg if isinstance(cfg, dict) else {})
        if not path:
            return {"host": hostname, "config": cfg}
        node = cfg
        for part in str(path).split("."):
            if not isinstance(node, dict) or part not in node:
                lower = {str(k).lower(): k for k in node} if isinstance(node, dict) else {}
                if part.lower() not in lower:
                    return {"host": hostname, "path": path, "error": "no such key", "keys": sorted(cfg)[:60]}
                part = lower[part.lower()]
            node = node[part]
        return {"host": hostname, "path": path, "value": node}

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
        return _agent_post(host, "llama", "/llama/server/wake", timeout=WAKE_TIMEOUT_S, need_fresh=True)

    def restart(provider, host):
        return _agent_post(host, provider, f"/{provider}/server/restart", timeout=60)

    def probe(host, model):
        """One-token completion to a llama model on one host; {ok, latency_ms, model} when it answered, else None (#1025)."""
        import agent_registry
        agent = _agent_by_hostname(host)
        if not agent:
            return None
        body = {"model": model, "messages": [{"role": "user", "content": "."}], "max_tokens": 1, "temperature": 0}
        t0 = time.monotonic()
        r, _tried, _err = agent_registry.agent_request("POST", agent, gateway._AGENT_PATHS["llama"]["chat/completions"], json=body,
                                                       headers={"Authorization": f"Bearer {agent.get('token') or ''}"}, timeout=(5, 90))
        if r is None or r.status_code != 200:
            return None
        try:
            data = r.json() or {}
        except ValueError:
            return None
        if not data.get("choices"):
            return None
        return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000), "model": data.get("model") or model}

    def pinned(provider, host, model):
        import agent_registry
        try:
            agent = agent_registry.pinned_agent(provider, model)
        except Exception:  # noqa: BLE001 — an unreadable registry never confirms a pin
            return False
        return bool(agent) and str(agent.get("hostname") or "").lower() == str(host or "").lower()

    deps_out = {
        "hosts": hosts_overview, "host": host_detail, "host_history": host_history,
        "models": models, "profiles": profiles,
        "alarms": alarms, "alarm_search": alarm_search, "alarm_history": alarm_history, "alert": alert,
        "metric_snapshot": metric_snapshot,
        "energy": energy_summary, "flow": gateway.flow_payload,
        "runs": tools_runs, "speed": speed_table, "health": service_health,
        "log_tail": log_tail, "config_get": config_get, "agent_config": agent_config, "help": default_help, "audit": audit_rows,
        "load": load, "unload": unload, "wake": wake, "restart": restart, "ack": base["ack"], "close": base["close"],
        "resume": base.get("resume") or (lambda aid: (False, "resume is not wired")), "alarm_config": alarm_config, "probe": probe,
        "pinned": pinned, "docs": docs_search,
        "run_result": lambda rid: next(iter(tools_runs(None, 1, {"run_id": rid}) or []), None) if rid else None,
        "card_result": card_result or (lambda jid: None),
        "bench_start": bench_start or (lambda a: {"ok": False, "message": "benchmarks are not wired"}),
        "bench_options": bench_options or (lambda a: []),
    }
    return deps_out
