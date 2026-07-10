"""vLLM provider — systemd lifecycle, Prometheus metrics, journal log,
svcconfig, OpenAI passthrough, opt-in LoRA."""

from __future__ import annotations

import json
import logging
import queue as _queue_lib
import subprocess
import threading
import time
from typing import Any, Iterator, Optional

import requests
from fastapi import Header, HTTPException, Query
from fastapi.responses import StreamingResponse

import stream_pool

# Minimal spec the agent's heartbeat body emits — see providers/llama.py.
PROVIDER_SPEC = {
    "name": "vllm",
    "capability_key": "vllm",
    "push_endpoint": "/api/remote/provider-state",
}

log = logging.getLogger("llm-systems-agent.providers.vllm")

_ctx = None

_vllm_session: Optional[requests.Session] = None
_vllm_session_lock = threading.Lock()


def set_context(ctx) -> None:
    global _ctx
    _ctx = ctx


def _require_ctx():
    if _ctx is None:
        raise RuntimeError("providers.vllm.set_context() not called")
    return _ctx


def _get_session() -> requests.Session:
    # Double-checked init so non-vLLM hosts don't carry a Session.
    global _vllm_session
    if _vllm_session is None:
        with _vllm_session_lock:
            if _vllm_session is None:
                _vllm_session = requests.Session()
    return _vllm_session


def _vllm_check_enabled() -> None:
    if not _require_ctx().config.VLLM_ENABLED:
        raise HTTPException(status_code=503, detail="vllm not enabled on this agent")


# ── Metrics collector (called from _build_metric_sample in main) ───────

_vllm_prev_counters: Optional[dict] = None
_vllm_info_cache: dict[str, Any] = {}
_vllm_info_ts: float = 0.0


def _parse_prom_families(text: str) -> "dict[str, list[float]]":
    """Prometheus text → {family_name: [sample values]}; drops comments + non-finite."""
    out: dict[str, list[float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name_part, _, val_part = line.rpartition(" ")
        name = name_part.split("{", 1)[0].strip()
        if not name:
            continue
        try:
            v = float(val_part)
        except ValueError:
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        out.setdefault(name, []).append(v)
    return out


def _fam_sum(fams: dict, name: str) -> Optional[float]:
    vals = fams.get(name)
    return sum(vals) if vals else None


def _derive_vllm_fields(fams: dict, prev: Optional[dict], now_mono: float) -> dict:
    gen = _fam_sum(fams, "vllm:generation_tokens_total")
    prompt = _fam_sum(fams, "vllm:prompt_tokens_total")
    # Family name varies by vLLM version: gpu_cache_usage_perc vs kv_cache_usage_perc.
    kv_vals = fams.get("vllm:gpu_cache_usage_perc") or fams.get("vllm:kv_cache_usage_perc")
    tps = pps = None
    if prev and gen is not None and prev.get("gen") is not None:
        dt = now_mono - prev["mono"]
        if dt > 0 and gen >= prev["gen"]:
            tps = round((gen - prev["gen"]) / dt, 2)
        if dt > 0 and prompt is not None and prev.get("prompt") is not None and prompt >= prev["prompt"]:
            pps = round((prompt - prev["prompt"]) / dt, 2)
    run = _fam_sum(fams, "vllm:num_requests_running")
    wait = _fam_sum(fams, "vllm:num_requests_waiting")
    return {
        "requests_running": int(run) if run is not None else None,
        "requests_waiting": int(wait) if wait is not None else None,
        "kv_cache_usage_pct": round(max(kv_vals) * 100.0, 1) if kv_vals else None,
        "tokens_per_second": tps,
        "prompt_tokens_per_second": pps,
        "total_tokens_generated": int(gen) if gen is not None else None,
        "total_tokens_prompted": int(prompt) if prompt is not None else None,
    }


def collect_vllm_for_metrics() -> dict[str, Any]:
    """Per-poll snapshot for the metric sample + provider-state push. {} when disabled."""
    global _vllm_prev_counters, _vllm_info_cache, _vllm_info_ts
    ctx = _require_ctx()
    if not ctx.config.VLLM_ENABLED:
        return {}
    now = time.monotonic()
    poll_s = float(getattr(ctx.config, "POLL_INTERVAL_S", 5) or 5)
    if _vllm_info_cache and (now - _vllm_info_ts) < poll_s:
        return dict(_vllm_info_cache)
    out: dict[str, Any] = {"state": "down", "model": None, "models": []}
    base = ctx.config.VLLM_API_URL.rstrip("/")
    try:
        r = _get_session().get(f"{base}/v1/models", timeout=3)
        if r.ok:
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
            out["state"] = "running"
            out["models"] = ids
            out["model"] = ids[0] if ids else None
    except Exception as e:
        log.debug("vllm /v1/models unreachable: %s", e)
    if out["state"] == "running":
        try:
            m = _get_session().get(f"{base}/metrics", timeout=3)
            if m.ok:
                fams = _parse_prom_families(m.text)
                out.update(_derive_vllm_fields(fams, _vllm_prev_counters, now))
                _vllm_prev_counters = {
                    "mono": now,
                    "gen": _fam_sum(fams, "vllm:generation_tokens_total"),
                    "prompt": _fam_sum(fams, "vllm:prompt_tokens_total"),
                }
        except Exception as e:
            log.debug("vllm /metrics scrape failed: %s", e)
    else:
        _vllm_prev_counters = None
    _vllm_info_cache = out
    _vllm_info_ts = now
    return dict(out)


# ── systemd lifecycle ──────────────────────────────────────────────────

def _vllm_systemctl(action: str, timeout: int = 30) -> dict[str, Any]:
    try:
        r = subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", action, _require_ctx().config.VLLM_SYSTEMD_UNIT],
            capture_output=True, text=True, timeout=timeout,
        )
        log.info("vllm %s: rc=%s %s", action, r.returncode, r.stderr.strip())
        return {"ok": r.returncode == 0, "error": r.stderr.strip() or None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def vllm_server_status_endpoint(
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    _vllm_check_enabled()
    try:
        r = subprocess.run(
            ["systemctl", "status", _require_ctx().config.VLLM_SYSTEMD_UNIT, "--no-pager", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        return {"ok": True, "output": r.stdout + r.stderr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def vllm_server_start_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _vllm_check_enabled()
    return _vllm_systemctl("start")


def vllm_server_stop_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _vllm_check_enabled()
    return _vllm_systemctl("stop")


def vllm_server_restart_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _vllm_check_enabled()
    return _vllm_systemctl("restart", timeout=60)


# ── Journal log tail + SSE stream ──────────────────────────────────────

_log_queue: "_queue_lib.Queue[str]" = _queue_lib.Queue(maxsize=4096)
_log_lock = threading.Lock()
_log_streaming = False


def vllm_log_tail(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Last 100 journal lines for the vLLM unit (no streaming)."""
    _require_ctx().check_bearer(authorization); _vllm_check_enabled()
    try:
        r = subprocess.run(
            ["journalctl", "-u", _require_ctx().config.VLLM_SYSTEMD_UNIT,
             "-n", "100", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False,
                "error": (r.stderr.strip() or "journalctl failed") +
                         " — is the agent user in the systemd-journal group?"}
    return {"ok": True, "lines": [l for l in r.stdout.splitlines() if l.strip()]}


def _vllm_log_streamer() -> None:
    """journalctl -f → _log_queue; evicts oldest when the queue is full."""
    global _log_streaming
    proc = None
    try:
        proc = subprocess.Popen(
            ["journalctl", "-u", _require_ctx().config.VLLM_SYSTEMD_UNIT,
             "-n", "100", "-f", "-o", "cat"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
        )
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            if not _log_streaming:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            try:
                _log_queue.put(line, timeout=1)
            except _queue_lib.Full:
                try:
                    _log_queue.get_nowait()
                except _queue_lib.Empty:
                    pass
                try:
                    _log_queue.put_nowait(line)
                except _queue_lib.Full:
                    pass
    except Exception as e:
        try:
            _log_queue.put_nowait(f"[log stream error: {e}]")
        except _queue_lib.Full:
            pass
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        with _log_lock:
            _log_streaming = False


def vllm_log_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    """SSE stream of vLLM journal lines (bearer header OR ?token= stream auth)."""
    _require_ctx().check_stream_auth(authorization, token, "/vllm/log/stream")
    _vllm_check_enabled()

    global _log_streaming
    with _log_lock:
        if not _log_streaming:
            _log_streaming = True
            while not _log_queue.empty():
                try:
                    _log_queue.get_nowait()
                except Exception:
                    break
            threading.Thread(target=_vllm_log_streamer, daemon=True).start()

    def generate() -> Iterator[bytes]:
        while True:
            try:
                line = _log_queue.get(timeout=15)
                yield f"data: {json.dumps({'line': line})}\n\n".encode()
            except _queue_lib.Empty:
                yield b'data: {"keepalive": true}\n\n'

    if not stream_pool.POOL.try_acquire():
        raise HTTPException(status_code=503, detail="agent at stream capacity; retry shortly")
    return StreamingResponse(
        stream_pool.guarded_async(generate()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Models ─────────────────────────────────────────────────────────────

def vllm_models_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _vllm_check_enabled()
    try:
        r = _get_session().get(f"{ctx.config.VLLM_API_URL.rstrip('/')}/v1/models", timeout=5)
        if r.ok:
            return {"data": r.json().get("data", []) or []}
        return {"data": [], "error": f"vllm /v1/models HTTP {r.status_code}"}
    except Exception as e:
        return {"data": [], "error": str(e)}


# ── Route registration ─────────────────────────────────────────────────

_ROUTES: tuple = (
    # (method, path, handler)
    ("GET",  "/vllm/server/status",  vllm_server_status_endpoint),
    ("POST", "/vllm/server/start",   vllm_server_start_endpoint),
    ("POST", "/vllm/server/stop",    vllm_server_stop_endpoint),
    ("POST", "/vllm/server/restart", vllm_server_restart_endpoint),
    ("GET",  "/vllm/log/tail",       vllm_log_tail),
    ("GET",  "/vllm/log/stream",     vllm_log_stream),
    ("GET",  "/vllm/models",         vllm_models_endpoint),
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
