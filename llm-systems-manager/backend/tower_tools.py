"""Tower tool registry (#924): a static, schema-validated allowlist of read
tools with capped results. No shell/file/HTTP/config-write tool; no runtime registration."""
from __future__ import annotations

import json
import logging
import math
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


def filter_alerts(rows: list, *, status: str = "all", window: Optional[str] = None, host: Optional[str] = None,
                  rule: Optional[str] = None, now: Optional[float] = None) -> list:
    """Alert rows (as _alert_row shapes) narrowed by status, window, host and rule substring."""
    now = time.time() if now is None else now
    start = now - WINDOWS[window] if window in WINDOWS else None
    out = []
    for r in rows:
        st = str(r.get("status") or "")
        if status == "active" and st not in _LIVE_STATUSES:
            continue
        if status == "closed" and st in _LIVE_STATUSES:
            continue
        if start is not None:
            ts = parse_ts(r.get("triggered_at"))
            if ts is None or ts < start:
                continue
        if host and str(r.get("host") or "").lower() != str(host).lower():
            continue
        if rule and str(rule).lower() not in str(r.get("rule") or "").lower():
            continue
        out.append(r)
    return out


def window_underfilled(rows: list, window: Optional[str], now: Optional[float] = None) -> bool:
    """True when the oldest fetched row is newer than the window start, so the window needs more rows."""
    if window not in WINDOWS:
        return False
    stamps = [t for t in (parse_ts(r.get("triggered_at")) for r in rows) if t]
    if not stamps:
        return False
    return min(stamps) > (time.time() if now is None else now) - WINDOWS[window]


def text_bars(pairs: "list[tuple[str, int]]", width: int = 20) -> str:
    """One line per (label, count): label, a █ bar scaled to the largest count, the count."""
    if not pairs:
        return ""
    top = max(c for _, c in pairs) or 1
    w = max(len(str(l)) for l, _ in pairs)
    return "\n".join(f"{str(l):<{w}}  {'█' * max(1, round(c * width / top)) if c else ''} {c}" for l, c in pairs)


def alarm_stats(rows: list, group_by: str = "rule", top: int = 10, label: str = "") -> dict:
    """Counts per group (with severity split) plus a text chart; day groups are UTC dates."""
    def key(r):
        if group_by == "day":
            ts = parse_ts(r.get("triggered_at"))
            return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "unknown"
        return str(r.get(group_by) or "unknown")
    groups: "dict[str, dict]" = {}
    for r in rows:
        g = groups.setdefault(key(r), {"key": None, "count": 0, "critical": 0, "warning": 0, "info": 0})
        g["count"] += 1
        sev = str(r.get("severity") or "").lower()
        if sev in ("critical", "warning", "info"):
            g[sev] += 1
    for k, g in groups.items():
        g["key"] = k
    ordered = sorted(groups.values(), key=(lambda g: g["key"]) if group_by == "day" else (lambda g: (-g["count"], g["key"])))
    if group_by == "day":
        ordered = ordered[-top:]
    else:
        ordered = ordered[:top]
    stamps = [t for t in (parse_ts(r.get("triggered_at")) for r in rows) if t]
    return {"window": label, "group_by": group_by, "total": len(rows), "groups": len(groups),
            "first": time.strftime("%Y-%m-%d %H:%M", time.gmtime(min(stamps))) if stamps else None,
            "last": time.strftime("%Y-%m-%d %H:%M", time.gmtime(max(stamps))) if stamps else None,
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


# Every tool name build_registry can return, for the settings chip list.
READ_TOOL_NAMES = ("hosts_overview", "host_detail", "models", "model_profiles", "alarms", "alarm_history",
                   "alert_detail", "energy_summary", "gateway_flow", "recent_runs", "bench_speed", "service_health",
                   "log_tail", "config_get", "help", "audit_log")
ACT_TOOL_NAMES = ("load_model", "unload_model", "wake_server", "restart_provider", "ack_alert", "close_alert")
TOOL_NAMES = READ_TOOL_NAMES + ACT_TOOL_NAMES
PROVIDER_LABEL = {"llama": "llama.cpp", "lms": "LM Studio", "vllm": "vLLM"}
_PROVIDER_ENUM = {"type": "string", "enum": ["llama", "lms", "vllm"]}
# vLLM has no load/unload endpoint, so those two tools drop it from their enum.
_LOAD_PROVIDER_ENUM = {"type": "string", "enum": ["llama", "lms"]}


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
        Tool("hosts_overview", "All hosts: providers, online, resident model, busy, watts.", _obj({}), "read", "read",
             lambda a: deps["hosts"]()),
        Tool("host_detail", "Everything about one host: hardware (CPU model, cores, RAM, GPU, VRAM, disks, OS, agent version) "
             "and live state (CPU pct and temp, RAM, swap, disks, IO, net, GPU clocks/temps/power/VRAM, watts, UPS, "
             "throughput window, llama/LM Studio/vLLM runtime).",
             _obj({"host": {"type": "string"}}, ["host"]), "read", "read", lambda a: deps["host"](a["host"]) or {"error": "unknown host"}),
        Tool("models", "Models on every host: loaded now (loaded_on) and available to load (available_on), per provider.",
             _obj({"host": {"type": "string"}, "provider": {"type": "string", "enum": ["llama", "lms", "vllm"]}}), "read", "read",
             lambda a: deps["models"](a.get("host"), a.get("provider"))),
        Tool("model_profiles", "Saved llama-server config profiles per host and model: the active profile, the other "
             "profile names, and (when a model is given) the active or named profile's values.",
             _obj({"host": {"type": "string"}, "model": {"type": "string"}, "profile": {"type": "string"}}),
             "read", "read", lambda a: deps["profiles"](a.get("host"), a.get("model"), a.get("profile"))),
        Tool("alarms", "Alerts from the alarm engine, newest first; filter by status, time window, host or rule.",
             _obj({"status": {"type": "string", "enum": ["active", "all", "closed"], "default": "active"},
                   "window": {"type": "string", "enum": list(WINDOWS)},
                   "host": {"type": "string"}, "rule": {"type": "string"},
                   "count": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10}}), "read", "read",
             lambda a: deps["alarms"](a["status"], a["count"], a.get("window"), a.get("host"), a.get("rule"))),
        Tool("alarm_history", "Alert counts over a time window grouped by rule, host, severity or day, with a text bar "
             "chart; use it for trends, totals and top offenders.",
             _obj({"window": {"type": "string", "enum": ["24h", "7d", "30d", "90d"], "default": "30d"},
                   "group_by": {"type": "string", "enum": ["rule", "host", "severity", "day"], "default": "rule"},
                   "host": {"type": "string"}, "rule": {"type": "string"},
                   "top": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10}}), "read", "read",
             lambda a: deps["alarm_history"](a["window"], a["group_by"], a["top"], a.get("host"), a.get("rule"))),
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
        Tool("log_tail", "Last lines of a provider's server log on a host.",
             _obj({"host": {"type": "string"}, "provider": {"type": "string", "enum": ["llama", "lms", "vllm"], "default": "llama"},
                   "lines": {"type": "integer", "minimum": 5, "maximum": 80, "default": 40}}, ["host"]), "read", "read",
             lambda a: deps["log_tail"](a["host"], a.get("provider", "llama"), a["lines"])),
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
                "triggered_at": a.get("created_at"), "last_seen": a.get("last_evaluated_at")}

    def _alerts(include_closed: bool, limit: int) -> list:
        q = "&include_closed=true" if include_closed else ""
        r = _ae(f"/api/alarm/alerts/?limit={int(limit)}{q}")
        r.raise_for_status()
        body = r.json()
        return [_alert_row(a) for a in (body if isinstance(body, list) else [])]

    def alarms(status="active", count=10, window=None, host=None, rule=None):
        wide = bool(window or host or rule) or status != "active"
        rows = _alerts(status != "active", 1000 if wide else int(count))
        return filter_alerts(rows, status=status, window=window, host=host, rule=rule)[:int(count)]

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
                                    "created_at": a.get("created_at"), "last_evaluated_at": a.get("closed_at") or a.get("created_at")}))
        return rows

    def alarm_history(window="30d", group_by="rule", top=10, host=None, rule=None):
        rows = _alerts(True, 1000)
        cap = "capped at the newest 1000 alerts"
        if len(rows) >= 1000 and window_underfilled(rows, window):
            rows, cap = _alerts_csv(), "capped at the newest 10000 alerts"
        rows = filter_alerts(rows, status="all", window=window, host=host, rule=rule)
        out = alarm_stats(rows, group_by, int(top), f"last {window}")
        if len(rows) >= 1000:
            out["note"] = cap
        return out

    def _agent_by_hostname(host):
        import agent_registry
        agents = agent_registry.load_agents().get("agents") or {}
        return next((dict(a, agent_id=aid) for aid, a in agents.items()
                     if a.get("status") == "approved" and str(a.get("hostname") or "").lower() == str(host).lower()), None)

    def _liveness(agent):
        import agent_registry
        return agent_registry.agent_liveness(agent)

    def hosts_overview():
        import agent_registry
        agents = agent_registry.load_agents().get("agents") or {}
        by_host = {str(a.get("hostname") or "").lower(): a for a in agents.values()}
        return [with_liveness(r, by_host.get(str(r.get("hostname") or "").lower()), _liveness) for r in base["fleet"]()]

    def host_detail(name):
        row = base["host"](name)
        agent = _agent_by_hostname(name)
        if row is None or agent is None:
            return row
        buckets = energy.store_view_from_provider_state().get(agent["agent_id"]) or {}
        sample, _ls = discord_bot._freshest(buckets)
        return {**with_liveness(row, agent, _liveness), "hardware": hardware_block(sample, agent),
                "live": live_block(sample, buckets)}

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

    def log_tail(host, provider="llama", lines=40):
        agent = _agent_by_hostname(host)
        if not agent:
            return {"error": "unknown host"}
        res = discord_bot._agent_json(agent, f"/{provider}/log/tail") or {}
        out = res.get("lines") if isinstance(res, dict) else res
        return (out or [])[-int(lines):]

    def models(host=None, provider=None):
        import agent_registry
        import autopilot
        import provider_state
        agents = agent_registry.load_agents().get("agents") or {}
        loaded = loaded_models(agents, lambda prov, aid: (provider_state.STORE.get(prov, aid) or {}).get("sample") or {},
                               autopilot._LOADED_BY_PROVIDER, host, provider)
        return models_rows(gateway_entries(), loaded, host, provider)

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

    def _agent_post(host, provider, path, **kw):
        agent = _agent_by_hostname(host)
        if not agent:
            return False, "unknown host"
        if provider and not (agent.get("capabilities") or {}).get(_cap_key(provider)):
            return False, f"{host} does not serve {PROVIDER_LABEL.get(provider, provider)}"
        return discord_bot._agent_call(agent, "POST", path, **kw)

    def load(provider, host, model):
        return _agent_post(host, provider, f"/{provider}/load", json={"model": model}, timeout=120)

    def unload(provider, host, model):
        return _agent_post(host, provider, f"/{provider}/unload", json={"model": model}, timeout=60)

    def wake(host):
        return _agent_post(host, "llama", "/llama/server/wake", timeout=75)

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
        "hosts": hosts_overview, "host": host_detail,
        "models": models, "profiles": profiles,
        "alarms": alarms, "alarm_history": alarm_history, "alert": alert, "energy": energy_summary, "flow": gateway.flow_payload,
        "runs": tools_runs, "speed": speed_table, "health": service_health,
        "log_tail": log_tail, "config_get": config_get, "help": default_help, "audit": audit_rows,
        "load": load, "unload": unload, "wake": wake, "restart": restart, "ack": base["ack"], "close": base["close"],
        "pinned": pinned,
    }
