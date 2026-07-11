"""vLLM provider — systemd lifecycle, Prometheus metrics, journal log,
svcconfig, OpenAI passthrough, opt-in LoRA."""

from __future__ import annotations

import json
import logging
import math
import queue as _queue_lib
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import requests
from fastapi import Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

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

_vllm_prev_counters: Optional[dict] = None


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
    global _vllm_prev_counters
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
                    pass  # another consumer drained it first
                try:
                    _log_queue.put_nowait(line)
                except _queue_lib.Full:
                    pass  # still full — drop the line (best-effort)
    except Exception as e:
        try:
            _log_queue.put_nowait(f"[log stream error: {e}]")
        except _queue_lib.Full:
            pass  # queue full — drop the error line (best-effort)
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass  # already exited (best-effort cleanup)
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


# ── svcconfig (ExecStart editing via the root-owned baked wrapper) ─────

_VLLM_SVCCONFIG_WRAPPER = "/usr/local/sbin/llm-vllm-svcconfig-apply"


def _vllm_svc_file_path() -> str:
    return f"/etc/systemd/system/{_require_ctx().config.VLLM_SYSTEMD_UNIT}"


def _wrapper_baked_unit_path(wrapper: str) -> str:
    """UNIT_PATH baked into the installed wrapper, or "" if unreadable."""
    try:
        for line in Path(wrapper).read_text().splitlines():
            if line.startswith("UNIT_PATH='") and line.rstrip().endswith("'"):
                return line.rstrip()[len("UNIT_PATH='"):-1]
    except OSError:
        pass  # unreadable/missing wrapper reads as "" (caller skips the check)
    return ""


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
    tokens = shlex.split(head or "")
    for a in args_list:
        tokens.append(a["flag"])
        if not a.get("bool") and a.get("value") not in (None, ""):
            tokens.append(str(a["value"]))
    for t in tokens:
        if not isinstance(t, str) or "\n" in t or "\r" in t:
            return None
    return tokens


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
    payload = ("\n".join(tokens) + "\n").encode()

    baked = _wrapper_baked_unit_path(_VLLM_SVCCONFIG_WRAPPER)
    expected = _vllm_svc_file_path()
    if baked and baked != expected:
        return {"ok": False,
                "error": f"svcconfig helper is baked for {baked} but the configured "
                         f"unit is {expected} — run a root agent install.sh --update "
                         f"to re-bake the helper and sudoers for the renamed unit"}

    try:
        r = subprocess.run(
            ["sudo", "-n", _VLLM_SVCCONFIG_WRAPPER],
            input=payload, capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            return {"ok": False,
                    "error": r.stderr.decode().strip()
                             or "svcconfig apply failed — check sudoers for llm-vllm-svcconfig-apply"}
    except Exception as e:
        return {"ok": False, "error": f"Write failed: {e}"}

    if body.get("restart"):
        try:
            subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart",
                            _require_ctx().config.VLLM_SYSTEMD_UNIT],
                           timeout=60, check=True, capture_output=True)
        except Exception as e:
            return {"ok": False, "error": f"restart failed: {e}"}
    return {"ok": True}


# ── OpenAI passthrough (mirrors providers/llama.py #214) ───────────────

_OPENAI_READ_TIMEOUT_S = 600.0


def _openai_wants_stream(body: bytes) -> bool:
    try:
        return bool((json.loads(body or b"{}") or {}).get("stream"))
    except Exception:
        return False


async def _vllm_openai_forward(sub: str, request: Request,
                               authorization: "Optional[str]"):
    """Narrow OpenAI passthrough to vLLM /v1/<sub>."""
    _require_ctx().check_bearer(authorization)
    _vllm_check_enabled()
    body = await request.body()
    url = f"{_require_ctx().config.VLLM_API_URL.rstrip('/')}/v1/{sub}"
    headers = {"Content-Type": "application/json"}
    # Async handler: blocking requests.post calls run off-loop via run_in_threadpool.
    if _openai_wants_stream(body):
        try:
            upstream = await run_in_threadpool(
                lambda: requests.post(url, data=body, headers=headers,
                                      stream=True,
                                      timeout=(5, _OPENAI_READ_TIMEOUT_S)))
        except requests.exceptions.RequestException as e:
            raise HTTPException(status_code=502, detail=str(e))
        ctype = upstream.headers.get("content-type") or "text/event-stream"
        if "text/event-stream" not in ctype.lower():
            content, status = upstream.content, upstream.status_code
            upstream.close()
            return Response(content=content, status_code=status, media_type=ctype)
        if not stream_pool.POOL.try_acquire():
            upstream.close()
            raise HTTPException(status_code=503,
                                detail="agent at stream capacity; retry shortly")

        def generate() -> "Iterator[bytes]":
            try:
                for chunk in upstream.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return StreamingResponse(stream_pool.guarded_async(generate()),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
    try:
        r = await run_in_threadpool(
            lambda: requests.post(url, data=body, headers=headers,
                                  timeout=(5, _OPENAI_READ_TIMEOUT_S)))
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=str(e))
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


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
