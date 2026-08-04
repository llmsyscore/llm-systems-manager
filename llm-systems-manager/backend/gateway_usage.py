"""#496: in-memory cumulative token counters from gateway-proxied OpenAI
responses, for providers without native token telemetry (LMS)."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("llm-systems-manager.gateway-usage")

_lock = threading.Lock()
_counters: dict = {}

# Previous pusher snapshot per agent: (epoch_s, gen_cum, prompt_cum).
# Touched only by the pusher thread (and tests), so no lock needed.
PUSH_INTERVAL_S = 15.0
_prev_push: dict = {}


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
