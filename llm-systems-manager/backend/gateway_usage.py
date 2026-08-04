"""#496: in-memory cumulative token counters from gateway-proxied OpenAI
responses, for providers without native token telemetry (LMS)."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("llm-systems-manager.gateway-usage")

_lock = threading.Lock()
_counters: dict = {}

# Per-agent ring of (epoch_s, gen_cum, prompt_cum) counter samples, appended
# every SAMPLE_INTERVAL_S by the sampler thread; feeds history_rates().
SAMPLE_INTERVAL_S = 5.0
_HISTORY_MAXLEN = 720
_history: dict = {}


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


def sample_now(now: "float | None" = None) -> None:
    """Append one (ts, gen, prompt) counter sample per agent to its ring."""
    ts = time.time() if now is None else float(now)
    with _lock:
        for aid, c in _counters.items():
            ring = _history.setdefault(aid, deque(maxlen=_HISTORY_MAXLEN))
            ring.append((ts, c["gen"], c["prompt"]))


def history_rates(agent_id: str) -> list:
    """[{ts, gen_tps, prompt_tps}] from consecutive ring samples; drops
    counter resets and non-positive gaps."""
    with _lock:
        samples = list(_history.get(agent_id) or ())
    out = []
    for (t0, g0, p0), (t1, g1, p1) in zip(samples, samples[1:]):
        dt = t1 - t0
        if dt <= 0 or g1 < g0 or p1 < p0:
            continue
        out.append({
            "ts": datetime.fromtimestamp(t1, tz=timezone.utc).isoformat(),
            "gen_tps": round((g1 - g0) / dt, 2),
            "prompt_tps": round((p1 - p0) / dt, 2),
        })
    return out


def start_sampler() -> None:
    """Daemon thread sampling counters every SAMPLE_INTERVAL_S; no-op under pytest."""
    if "pytest" in sys.modules:
        return

    def _loop():
        while True:
            try:
                sample_now()
            except Exception as e:
                log.debug("gateway usage sample failed: %s", e)
            time.sleep(SAMPLE_INTERVAL_S)

    threading.Thread(target=_loop, name="gateway-usage-sampler",
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
