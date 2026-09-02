"""#496: in-memory cumulative token counters from gateway-proxied OpenAI
responses, for providers without native token telemetry (LMS)."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone

import rate_counter

log = logging.getLogger("llm-systems-manager.gateway-usage")

_lock = threading.Lock()
_counters: dict = {}

# Previous pusher snapshot per agent: (epoch_s, gen_cum, prompt_cum).
# Touched only by the pusher thread (and tests), so no lock needed.
PUSH_INTERVAL_S = 15.0
_prev_push: dict = {}
# Last rates the pusher computed per agent — the dashboard reads these so
# the card shows exactly what the alarm engine stores.
_last_rates: dict = {}


def record(agent_id: str, prompt_tokens, completion_tokens) -> None:
    """Add one response's usage to the agent's cumulative counters."""
    try:
        p = int(prompt_tokens or 0)
        g = int(completion_tokens or 0)
    except (TypeError, ValueError):
        return
    if not agent_id or (p <= 0 and g <= 0):
        return
    with _lock:
        c = _counters.setdefault(agent_id, {"gen": 0, "prompt": 0})
        c["gen"] += max(0, g)
        c["prompt"] += max(0, p)


def counters() -> dict:
    """Snapshot: {agent_id: {"gen": N, "prompt": N}}, cumulative since start."""
    with _lock:
        return {aid: dict(c) for aid, c in _counters.items()}


# In-flight gateway requests per agent. LM Studio publishes no request or
# queue telemetry of its own — the only lms metric that reaches the alarm
# engine is server_port — so proxied requests are the one countable signal.
_inflight: dict = {}


def begin(agent_id: str) -> None:
    if not agent_id:
        return
    with _lock:
        _inflight[agent_id] = _inflight.get(agent_id, 0) + 1


def end(agent_id: str) -> None:
    """Idempotent below zero: a double-close must not drive the count negative."""
    if not agent_id:
        return
    with _lock:
        n = _inflight.get(agent_id, 0) - 1
        if n > 0:
            _inflight[agent_id] = n
        else:
            _inflight.pop(agent_id, None)


def inflight(agent_id: "str | None" = None):
    """Count for one agent, or the whole {agent_id: n} snapshot."""
    with _lock:
        if agent_id is not None:
            return _inflight.get(agent_id, 0)
        return dict(_inflight)


# ── Per-client attribution (#797) ────────────────────────────────────
# One entry per (label, ip) seen on a gateway request, over a 15 min window.
CLIENT_WINDOW_S = 900.0
IDLE_AFTER_S = 600.0

_clients: dict = {}
_latency = rate_counter.SampleWindow(CLIENT_WINDOW_S)
_errors = rate_counter.RateCounter(CLIENT_WINDOW_S)


def _prune_clients(t: float) -> None:
    stale = [k for k, c in _clients.items()
             if c["inflight"] <= 0 and t - c["last_seen"] > CLIENT_WINDOW_S]
    for k in stale:
        _clients.pop(k, None)


def _client_entry(key: tuple) -> dict:
    c = _clients.get(key)
    if c is None:
        _prune_clients(time.time())
        c = {"label": key[0], "ip": key[1],
             "reqs": rate_counter.TimestampRing(CLIENT_WINDOW_S),
             "prompt": 0, "gen": 0, "last_seen": 0.0, "inflight": 0}
        _clients[key] = c
    return c


def client_begin(label: str, ip: "str | None",
                 now: "float | None" = None) -> tuple:
    """Record one inbound gateway request; returns the client key."""
    key = (str(label or "unknown"), str(ip or ""))
    with _lock:
        c = _client_entry(key)
        c["last_seen"] = c["reqs"].add(now)
        c["inflight"] += 1
    return key


def client_end(key: "tuple | None", now: "float | None" = None) -> None:
    """Idempotent below zero: a double-close must not go negative."""
    if not key:
        return
    with _lock:
        c = _clients.get(key)
        if c is None:
            return
        c["inflight"] = max(0, c["inflight"] - 1)
        c["last_seen"] = time.time() if now is None else float(now)


def client_record(key: "tuple | None", prompt_tokens, completion_tokens) -> None:
    """Add one response's usage to the client's token totals."""
    if not key:
        return
    try:
        pt, gt = int(prompt_tokens or 0), int(completion_tokens or 0)
    except (TypeError, ValueError):
        return
    with _lock:
        c = _clients.get(key)
        if c is None:
            return
        c["prompt"] += max(0, pt)
        c["gen"] += max(0, gt)


def record_latency(ms: float, now: "float | None" = None) -> None:
    """One request's wall time in ms (first byte for streams)."""
    _latency.add(ms, now=now)


def record_error(now: "float | None" = None) -> None:
    _errors.add(1, now=now)


def clients_snapshot(now: "float | None" = None) -> list:
    """Per-client rows, most recently seen first; idle after IDLE_AFTER_S."""
    t = time.time() if now is None else float(now)
    out = []
    with _lock:
        _prune_clients(t)
        rows = list(_clients.values())
        for c in rows:
            last = round(max(0.0, t - c["last_seen"]), 1)
            out.append({
                "label": c["label"],
                "ip": c["ip"],
                "req_per_min": c["reqs"].count_since(60.0, now=t),
                "inflight": c["inflight"],
                "prompt_tokens": c["prompt"],
                "gen_tokens": c["gen"],
                "last_seen_s": last,
                "state": "idle" if (c["inflight"] <= 0
                                    and last > IDLE_AFTER_S) else "ok",
            })
    out.sort(key=lambda r: r["last_seen_s"])
    return out


def client_totals(now: "float | None" = None) -> dict:
    """Fleet-wide gateway request/latency/error rollup over the window."""
    t = time.time() if now is None else float(now)
    rows = clients_snapshot(t)
    p50 = _latency.percentile(0.5, now=t)
    return {
        "req_per_min": sum(r["req_per_min"] for r in rows),
        "p50_ms": None if p50 is None else round(p50, 1),
        "errors_15m": _errors.total(now=t),
    }


def reset_clients() -> None:
    with _lock:
        _clients.clear()
    _latency.reset()
    _errors.reset()


def metric_points(agent_hosts: dict, now: "float | None" = None) -> list:
    """Alarm-engine metric points for every LMS agent: cumulative token
    totals plus tok/s rates derived from the previous call's snapshot."""
    ts = time.time() if now is None else float(now)
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    counts = counters()
    out = []
    for aid, host in (agent_hosts or {}).items():
        if not host:
            continue
        c = counts.get(aid) or {"gen": 0, "prompt": 0}
        prev = _prev_push.get(aid)
        _prev_push[aid] = (ts, c["gen"], c["prompt"])
        gen_tps = prompt_tps = 0.0
        if (prev and ts > prev[0]
                and c["gen"] >= prev[1] and c["prompt"] >= prev[2]):
            dt = ts - prev[0]
            gen_tps = (c["gen"] - prev[1]) / dt
            prompt_tps = (c["prompt"] - prev[2]) / dt
        _last_rates[aid] = {"gen_tps": round(gen_tps, 2),
                            "prompt_tps": round(prompt_tps, 2), "ts": iso}
        for name, val, unit in (
            ("lms_tokens_per_second",        round(gen_tps, 2),    "tok/s"),
            ("lms_prompt_tokens_per_second", round(prompt_tps, 2), "tok/s"),
            ("lms_gen_tokens_total",         float(c["gen"]),      "tokens"),
            ("lms_prompt_tokens_total",      float(c["prompt"]),   "tokens"),
        ):
            out.append({"source": "gateway", "metric_name": name,
                        "value": val, "unit": unit, "timestamp": iso,
                        "hostname": host})
    return out


def last_rates(agent_id: str) -> "dict | None":
    """The pusher's most recent {gen_tps, prompt_tps, ts} for an agent."""
    r = _last_rates.get(agent_id)
    return dict(r) if r else None


def fleet_rates(agent_ids, max_age_s: float = 60.0) -> dict:
    """Summed fresh gen/prompt tok/s across agents; stale entries drop out."""
    now = datetime.now(timezone.utc)
    tps = pps = 0.0
    for aid in agent_ids or ():
        r = _last_rates.get(aid)
        if not r:
            continue
        try:
            age = (now - datetime.fromisoformat(r["ts"])).total_seconds()
        except Exception:
            continue
        if age > max_age_s:
            continue
        tps += float(r.get("gen_tps") or 0.0)
        pps += float(r.get("prompt_tps") or 0.0)
    return {"total_tps": round(tps, 2), "total_pps": round(pps, 2)}


def start_pusher(push, agent_hosts_fn, interval_s: float = PUSH_INTERVAL_S) -> None:
    """Daemon thread pushing metric_points() batches every interval_s;
    no-op under pytest."""
    if "pytest" in sys.modules:
        return

    def _loop():
        while True:
            try:
                pts = metric_points(agent_hosts_fn() or {})
                if pts:
                    push(pts)
            except Exception as e:
                log.debug("gateway usage push failed: %s", e)
            time.sleep(interval_s)

    threading.Thread(target=_loop, name="gateway-usage-pusher",
                     daemon=True).start()


def usage_from_json_bytes(content) -> "tuple[int, int] | None":
    """(prompt_tokens, completion_tokens) from an OpenAI JSON body, or None."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return None
    return _usage_from_dict(data)


def completion_usage_from_json_bytes(content) -> "tuple[int, int] | None":
    """Usage tuple only when the body is a completion response — requires a
    'choices' list alongside 'usage', so wrapper payloads don't count (#629)."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        return None
    return _usage_from_dict(data)


def _usage_from_dict(data) -> "tuple[int, int] | None":
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    p, g = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not isinstance(p, (int, float)) and not isinstance(g, (int, float)):
        return None
    return int(p or 0), int(g or 0)


def _scan_line(line: bytes) -> "tuple[int, int] | None":
    line = line.strip()
    if not line.startswith(b"data:") or b'"usage"' not in line:
        return None
    return usage_from_json_bytes(line[5:].strip())


def _bare_usage(line: bytes) -> bool:
    """True for a usage-only SSE data line (no choices payload)."""
    line = line.strip()
    if not line.startswith(b"data:"):
        return False
    try:
        data = json.loads(line[5:].strip())
    except (TypeError, ValueError):
        return False
    return (isinstance(data, dict) and isinstance(data.get("usage"), dict)
            and not data.get("choices"))


def tap_sse(chunks, on_usage, strip_usage=False):
    """Pass-through generator over SSE chunks; on exhaustion reports the
    last usage object seen to on_usage(prompt_tokens, completion_tokens).
    strip_usage drops usage-only events (injected probes) from the relay."""
    buf = b""
    last = None
    try:
        for chunk in chunks:
            raw = chunk.encode() if isinstance(chunk, str) else chunk
            buf += raw
            if not strip_usage:
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    last = _scan_line(line) or last
                yield chunk
                continue
            out = bytearray()
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                u = _scan_line(line)
                if u:
                    last = u
                    if _bare_usage(line):
                        continue
                out += line + b"\n"
            if out:
                yield bytes(out)
        if strip_usage and buf:
            u = _scan_line(buf)
            last = u or last
            if not (u and _bare_usage(buf)):
                yield buf
            buf = b""
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            try:
                close()
            except Exception as e:
                log.debug("inner stream close failed: %s", e)
        if buf:
            last = _scan_line(buf) or last
        if last:
            try:
                on_usage(*last)
            except Exception as e:
                log.debug("usage callback failed: %s", e)
