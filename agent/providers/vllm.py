"""vLLM provider — systemd lifecycle, Prometheus metrics, journal log,
svcconfig, OpenAI passthrough, opt-in LoRA."""

from __future__ import annotations

import logging
import math
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import _shared

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
    # Double-checked lazy init of the shared Session.
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

# Last scrape's token counters ({mono, gen, prompt}); empty = no previous sample.
_vllm_rate_state: dict[str, Any] = {}


def _parse_prom_families(text: str) -> "dict[str, list[float]]":
    """Prometheus text → {family_name: [sample values]}; drops comments,
    non-finite values, and optional trailing timestamps."""
    out: dict[str, list[float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "}" in line:
            head, _, rest = line.partition("}")
            name = head.split("{", 1)[0].strip()
            fields = rest.split()
        else:
            fields = line.split()
            name = fields[0] if fields else ""
            fields = fields[1:]
        if not name or not fields:
            continue
        try:
            v = float(fields[0])
        except ValueError:
            continue
        if math.isnan(v) or math.isinf(v):
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
    ctx = _require_ctx()
    if not ctx.config.VLLM_ENABLED:
        return {}
    now = time.monotonic()
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
                out.update(_derive_vllm_fields(fams, _vllm_rate_state, now))
                _vllm_rate_state.update(
                    mono=now,
                    gen=_fam_sum(fams, "vllm:generation_tokens_total"),
                    prompt=_fam_sum(fams, "vllm:prompt_tokens_total"),
                )
        except Exception as e:
            log.debug("vllm /metrics scrape failed: %s", e)
    else:
        _vllm_rate_state.clear()
    return out


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

_log_state = _shared.LogStream()


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
    """journalctl -f → _log_state.queue; evicts oldest when the queue is full."""
    _log_state.pump(
        ["journalctl", "-u", _require_ctx().config.VLLM_SYSTEMD_UNIT,
         "-n", "100", "-f", "-o", "cat"])


def vllm_log_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    """SSE stream of vLLM journal lines (bearer header OR ?token= stream auth)."""
    _require_ctx().check_stream_auth(authorization, token, "/vllm/log/stream")
    _vllm_check_enabled()
    _log_state.ensure_started(_vllm_log_streamer)
    return _log_state.sse_response()


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


# ── svcconfig (ExecStart editing via the root-owned baked wrapper) ─────

_VLLM_SVCCONFIG_WRAPPER = "/usr/local/sbin/llm-vllm-svcconfig-apply"


def _vllm_svc_file_path() -> str:
    return f"/etc/systemd/system/{_require_ctx().config.VLLM_SYSTEMD_UNIT}"


def _parse_vllm_execstart(content: str) -> "tuple[Optional[str], list[dict]]":
    """ExecStart → (command head incl. positionals, flag list).
    A flag followed by a non-flag token is a value flag; else boolean."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("ExecStart="):
            continue
        parts = shlex.split(stripped[len("ExecStart="):].strip())
        head: list[str] = []
        i = 0
        while i < len(parts) and not parts[i].startswith("-"):
            head.append(parts[i])
            i += 1
        args: list[dict] = []
        while i < len(parts):
            p = parts[i]
            if p.startswith("-"):
                if i + 1 < len(parts) and not parts[i + 1].startswith("-"):
                    args.append({"flag": p, "value": parts[i + 1], "bool": False})
                    i += 2
                else:
                    args.append({"flag": p, "value": None, "bool": True})
                    i += 1
            else:
                i += 1
        return " ".join(head), args
    return None, []


def _svcconfig_tokens(head: str, args_list: list) -> "Optional[list[str]]":
    return _shared.build_svcconfig_tokens(shlex.split(head or ""), args_list)


def vllm_svcconfig_get(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _vllm_check_enabled()
    try:
        content = Path(_vllm_svc_file_path()).read_text()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    head, args = _parse_vllm_execstart(content)
    if head is None:
        return {"ok": False, "error": "ExecStart line not found in service file"}
    return {"ok": True, "binary": head, "args": args}


def vllm_svcconfig_post(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _vllm_check_enabled()
    tokens = _svcconfig_tokens(body.get("binary", ""), body.get("args", []))
    if not tokens:
        return {"ok": False, "error": "invalid ExecStart token (newline or non-string)"}
    return _shared.svcconfig_apply(
        _VLLM_SVCCONFIG_WRAPPER, _vllm_svc_file_path(), tokens,
        sudoers_hint="llm-vllm-svcconfig-apply",
        restart_unit=_require_ctx().config.VLLM_SYSTEMD_UNIT if body.get("restart") else None,
        restart_timeout=60)


# ── OpenAI passthrough (shared with providers/llama.py #214) ───────────

async def _vllm_openai_forward(sub: str, request: Request,
                               authorization: "Optional[str]"):
    """Narrow OpenAI passthrough to vLLM /v1/<sub>."""
    ctx = _require_ctx()
    ctx.check_bearer(authorization)
    _vllm_check_enabled()
    return await _shared.openai_forward(sub, request, ctx.config.VLLM_API_URL)


async def vllm_openai_chat(request: Request,
                           authorization: Optional[str] = Header(default=None)):
    return await _vllm_openai_forward("chat/completions", request, authorization)


async def vllm_openai_completions(request: Request,
                                  authorization: Optional[str] = Header(default=None)):
    return await _vllm_openai_forward("completions", request, authorization)


# ── LoRA adapters (opt-in; vLLM exposes these in trusted envs only) ────

def _vllm_lora_guard() -> None:
    if not getattr(_require_ctx().config, "VLLM_LORA_ENABLED", False):
        raise HTTPException(status_code=503,
                            detail="vllm LoRA endpoints not enabled (VLLM_LORA_ENABLED)")


def vllm_lora_load(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _vllm_check_enabled(); _vllm_lora_guard()
    name, path = body.get("lora_name"), body.get("lora_path")
    if not (name and path):
        raise HTTPException(status_code=400, detail="lora_name and lora_path required")
    try:
        r = _get_session().post(f"{ctx.config.VLLM_API_URL.rstrip('/')}/v1/load_lora_adapter",
                                json={"lora_name": name, "lora_path": path}, timeout=60)
        return {"ok": r.ok, "status": r.status_code, "output": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def vllm_lora_unload(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    ctx = _require_ctx()
    ctx.check_bearer(authorization); _vllm_check_enabled(); _vllm_lora_guard()
    name = body.get("lora_name")
    if not name:
        raise HTTPException(status_code=400, detail="lora_name required")
    try:
        r = _get_session().post(f"{ctx.config.VLLM_API_URL.rstrip('/')}/v1/unload_lora_adapter",
                                json={"lora_name": name}, timeout=60)
        return {"ok": r.ok, "status": r.status_code, "output": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Route registration ─────────────────────────────────────────────────

_ROUTES: tuple = (
    # (method, path, handler)
    ("GET",  "/vllm/server/status",  vllm_server_status_endpoint),
    ("POST", "/vllm/server/start",   vllm_server_start_endpoint),
    ("POST", "/vllm/server/stop",    vllm_server_stop_endpoint),
    ("POST", "/vllm/server/restart", vllm_server_restart_endpoint),
    ("GET",  "/vllm/server/svcconfig", vllm_svcconfig_get),
    ("POST", "/vllm/server/svcconfig", vllm_svcconfig_post),
    ("GET",  "/vllm/log/tail",       vllm_log_tail),
    ("GET",  "/vllm/log/stream",     vllm_log_stream),
    ("GET",  "/vllm/models",         vllm_models_endpoint),
    ("POST", "/vllm/openai/chat/completions", vllm_openai_chat),
    ("POST", "/vllm/openai/completions",      vllm_openai_completions),
    ("POST", "/vllm/lora/load",      vllm_lora_load),
    ("POST", "/vllm/lora/unload",    vllm_lora_unload),
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
