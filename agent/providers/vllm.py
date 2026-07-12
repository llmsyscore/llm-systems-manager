"""vLLM provider — systemd lifecycle, Prometheus metrics, journal log,
svcconfig, OpenAI passthrough, opt-in LoRA."""

from __future__ import annotations

import logging
import math
import re
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


# ── Autotune: --max-model-len tuner (#356) ─────────────────────────────

_AT_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_AT_KV_SIZE_RE = re.compile(r"GPU KV cache size:\s*([\d,]+)\s*tokens")
_AT_MAX_CONC_RE = re.compile(
    r"Maximum concurrency for\s*([\d,]+)\s*tokens per request:\s*([\d.]+)x")
_AT_EST_MAX_RE = re.compile(r"estimated maximum model length is\s*([\d,]+)")
_AT_KV_CAP_OLD_RE = re.compile(
    r"maximum number of tokens that can be stored in KV cache \(([\d,]+)\)")
_AT_FATAL_RE = re.compile(
    r"EngineCore failed|Engine core initialization failed|ValueError|"
    r"RuntimeError|OutOfMemoryError|CUDA out of memory")


def _at_num(s: str) -> int:
    return int(s.replace(",", ""))


def compute_recommended_max_len(kv_tokens: int, concurrency: float = 1.0,
                                kv_fraction: float = 1.0) -> int:
    """Largest --max-model-len fitting kv_tokens at the target concurrency,
    scaled by kv_fraction, floored to a multiple of 256 (min 256)."""
    conc = max(1.0, float(concurrency))
    frac = min(1.0, max(0.1, float(kv_fraction)))
    raw = int(int(kv_tokens) * frac / conc)
    return max(256, (raw // 256) * 256)


def _at_get_max_len(args: list) -> Optional[int]:
    for a in args:
        if a.get("flag") == "--max-model-len" and a.get("value"):
            try:
                return _at_num(str(a["value"]))
            except ValueError:
                return None
    return None


def _at_args_with_max_len(args: list, value: int) -> list:
    out = [dict(a) for a in args if a.get("flag") != "--max-model-len"]
    out.append({"flag": "--max-model-len", "value": str(value), "bool": False})
    return out


_at_job = _shared.JobRunner("vllm-autotune")


def _at_watch_journal(unit: str, timeout_s: float, step: str) -> dict:
    """Follow the unit journal until a KV-capacity answer, engine failure,
    cancel, or timeout; emits line + loading_progress events while waiting."""
    res: dict[str, Any] = {"outcome": "timeout", "max_conc": None}
    proc = subprocess.Popen(
        ["journalctl", "-u", unit, "-n", "0", "-f", "-o", "cat"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    _at_job.track(proc)
    deadline = time.monotonic() + timeout_s
    grace: Optional[float] = None
    killer = threading.Timer(timeout_s, _at_job.kill_tracked)
    killer.daemon = True
    killer.start()
    hb_stop = threading.Event()

    def _hb():
        start = time.monotonic()
        while not hb_stop.wait(2.0):
            _at_job.put({"type": "loading_progress", "step": step,
                         "elapsed_s": round(time.monotonic() - start, 1),
                         "timeout_s": timeout_s})
            if time.monotonic() - start > 15:
                try:
                    r = subprocess.run(["systemctl", "is-active", unit],
                                       capture_output=True, text=True, timeout=5)
                    state = (r.stdout or "").strip()
                    if state in ("failed", "inactive"):
                        res["unit_state"] = state
                        _at_job.kill_tracked()
                        return
                except Exception:
                    log.debug("is-active poll failed", exc_info=True)

    threading.Thread(target=_hb, daemon=True).start()
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, ""):
            if _at_job.cancel_event.is_set():
                res["outcome"] = "cancelled"
                break
            line = _AT_ANSI_RE.sub("", raw).rstrip()
            if not line:
                continue
            _at_job.put({"type": "line", "text": line})
            m = _AT_MAX_CONC_RE.search(line)
            if m:
                res["max_conc"] = float(m.group(2))
            m = _AT_KV_SIZE_RE.search(line)
            if m:
                res.update(outcome="kv", kv_tokens=_at_num(m.group(1)))
                grace = min(deadline, time.monotonic() + 8)
            m = _AT_EST_MAX_RE.search(line) or _AT_KV_CAP_OLD_RE.search(line)
            if m and res["outcome"] != "kv":
                res.update(outcome="est_max", est_max_len=_at_num(m.group(1)))
                break
            if res["outcome"] != "kv" and _AT_FATAL_RE.search(line):
                res.update(outcome="fatal", fatal_line=line[:300])
                break
            if res["outcome"] == "kv" and (res["max_conc"] is not None
                                           or (grace and time.monotonic() > grace)):
                break
        if _at_job.cancel_event.is_set():
            res["outcome"] = "cancelled"
        elif res["outcome"] == "timeout" and res.get("unit_state"):
            res.update(outcome="fatal",
                       fatal_line=f"unit {unit} is {res['unit_state']} after restart")
    finally:
        hb_stop.set()
        killer.cancel()
        _at_job.kill_tracked()
        _at_job.untrack()
    return res


def _at_wait_ready(timeout_s: float = 60.0) -> Optional[int]:
    """Poll /v1/models until it answers; returns the reported max_model_len."""
    base = _require_ctx().config.VLLM_API_URL.rstrip("/")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not _at_job.cancel_event.is_set():
        try:
            r = _get_session().get(f"{base}/v1/models", timeout=3)
            if r.ok:
                data = r.json().get("data") or []
                return data[0].get("max_model_len") if data else None
        except Exception:
            pass
        time.sleep(2)
    return None


def _at_apply(head_tokens: list, args: list) -> dict:
    """svcconfig write (daemon-reload inside the wrapper), no restart."""
    tokens = _shared.build_svcconfig_tokens(head_tokens, args)
    if not tokens:
        return {"ok": False, "error": "invalid ExecStart token (newline or non-string)"}
    return _shared.svcconfig_apply(
        _VLLM_SVCCONFIG_WRAPPER, _vllm_svc_file_path(), tokens,
        sudoers_hint="llm-vllm-svcconfig-apply", restart_unit=None)


def _at_restart_and_watch(unit: str, timeout_s: float, step: str) -> dict:
    """Start the journal watcher in a thread, then restart the unit."""
    holder: dict[str, Any] = {}
    t = threading.Thread(
        target=lambda: holder.update(_at_watch_journal(unit, timeout_s, step)),
        daemon=True)
    t.start()
    time.sleep(0.5)
    r = _vllm_systemctl("restart", timeout=60)
    if not r["ok"]:
        _at_job.cancel_event.set()
        _at_job.kill_tracked()
        t.join(timeout=10)
        _at_job.cancel_event.clear()
        return {"outcome": "fatal", "max_conc": None,
                "fatal_line": f"systemctl restart failed: {r.get('error')}"}
    t.join(timeout=timeout_s + 15)
    return holder or {"outcome": "timeout", "max_conc": None}


def _at_run(params: dict) -> None:
    """Job thread: probe → compute → apply+verify (or report-only) → rollback."""
    ctx = _require_ctx()
    unit = ctx.config.VLLM_SYSTEMD_UNIT
    ok = False
    mutated = False
    head_tokens: list = []
    orig_args: list = []
    orig_max: Optional[int] = None
    try:
        content = Path(_vllm_svc_file_path()).read_text()
        head, orig_args = _parse_vllm_execstart(content)
        if head is None:
            _at_job.put({"type": "model_done", "ok": False, "applied": False,
                         "error": "ExecStart line not found in service file"})
            return
        head_tokens = shlex.split(head)
        orig_max = _at_get_max_len(orig_args)
        model = head_tokens[2] if len(head_tokens) > 2 else None
        _at_job.put({"type": "model_start", "unit": unit, "model": model,
                     "original_max_len": orig_max})

        _at_job.put({"type": "step_start", "step": "probe",
                     "max_model_len": params["probe_len"]})
        r = _at_apply(head_tokens, _at_args_with_max_len(orig_args, params["probe_len"]))
        if not r["ok"]:
            _at_job.put({"type": "model_done", "ok": False, "applied": False,
                         "error": r.get("error")})
            return
        mutated = True
        watch = _at_restart_and_watch(unit, params["load_timeout_s"], "probe")
        if watch["outcome"] == "cancelled":
            return
        if watch["outcome"] == "kv":
            kv_tokens = watch["kv_tokens"]
        elif watch["outcome"] == "est_max":
            kv_tokens = watch["est_max_len"]
        else:
            _at_job.put({"type": "model_done", "ok": False, "applied": False,
                         "error": watch.get("fatal_line")
                         or f"probe {watch['outcome']} — no KV-capacity answer in journal"})
            return
        _at_job.put({"type": "kv_capacity", "tokens": kv_tokens})

        rec = compute_recommended_max_len(kv_tokens, params["concurrency"],
                                          params["kv_fraction"])
        _at_job.put({"type": "recommendation", "max_model_len": rec,
                     "kv_tokens": kv_tokens, "concurrency": params["concurrency"],
                     "kv_fraction": params["kv_fraction"]})

        if params["report_only"]:
            _at_job.put({"type": "model_done", "ok": True, "applied": False,
                         "report_only": True, "max_model_len": rec,
                         "kv_tokens": kv_tokens, "max_concurrency_x": watch["max_conc"],
                         "original_max_len": orig_max})
            ok = True
            return

        _at_job.put({"type": "step_start", "step": "apply", "max_model_len": rec})
        r = _at_apply(head_tokens, _at_args_with_max_len(orig_args, rec))
        if not r["ok"]:
            _at_job.put({"type": "model_done", "ok": False, "applied": False,
                         "error": r.get("error")})
            return
        watch = _at_restart_and_watch(unit, params["load_timeout_s"], "verify")
        if watch["outcome"] == "cancelled":
            return
        if watch["outcome"] != "kv":
            _at_job.put({"type": "model_done", "ok": False, "applied": False,
                         "error": watch.get("fatal_line")
                         or f"verify {watch['outcome']} at max-model-len={rec}"})
            return
        reported = _at_wait_ready(60)
        if reported is not None and reported != rec:
            _at_job.put({"type": "line",
                         "text": f"note: server reports max_model_len={reported}"})
        mutated = False
        ok = True
        _at_job.put({"type": "model_done", "ok": True, "applied": True,
                     "report_only": False, "max_model_len": rec,
                     "kv_tokens": watch.get("kv_tokens", kv_tokens),
                     "max_concurrency_x": watch["max_conc"],
                     "original_max_len": orig_max})
    except Exception as e:
        _at_job.put({"type": "model_done", "ok": False, "applied": False,
                     "error": str(e)})
    finally:
        cancelled = _at_job.cancel_event.is_set()
        if mutated and head_tokens:
            _at_job.put({"type": "step_start", "step": "rollback",
                         "max_model_len": orig_max})
            rr = _at_apply(head_tokens, orig_args)
            rs = _vllm_systemctl("restart", timeout=60) if rr["ok"] else rr
            if not rs["ok"]:
                _at_job.put({"type": "rollback_failed",
                             "error": rs.get("error") or "rollback restart failed"})
        _at_job.put({"type": "done", "ok": ok and not cancelled,
                     "cancelled": cancelled})


def vllm_autotune_run(body: dict,
                      authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    _vllm_check_enabled()
    try:
        params = {
            "probe_len": int(body.get("probe_len") or 4096),
            "concurrency": float(body.get("concurrency") or 1.0),
            "kv_fraction": float(body.get("kv_fraction") or 1.0),
            "load_timeout_s": int(body.get("load_timeout_s") or 600),
            "report_only": bool(body.get("report_only")),
        }
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid autotune parameters")
    if not 256 <= params["probe_len"] <= 262144:
        raise HTTPException(status_code=400, detail="probe_len out of range (256–262144)")
    if not 1.0 <= params["concurrency"] <= 64.0:
        raise HTTPException(status_code=400, detail="concurrency out of range (1–64)")
    if not 0.1 <= params["kv_fraction"] <= 1.0:
        raise HTTPException(status_code=400, detail="kv_fraction out of range (0.1–1.0)")
    if not 60 <= params["load_timeout_s"] <= 3600:
        raise HTTPException(status_code=400, detail="load_timeout_s out of range (60–3600)")
    if not _at_job.try_start(lambda: _at_run(params)):
        return {"ok": False, "error": "Another auto-tune is in progress"}
    return {"ok": True}


def vllm_autotune_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    _require_ctx().check_stream_auth(authorization, token, "/vllm/autotune/stream")
    _vllm_check_enabled()
    return _at_job.sse_response()


def vllm_autotune_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    _vllm_check_enabled()
    return {"ok": _at_job.cancel(), "active": _at_job.active}


def shutdown_children() -> None:
    """Kill tracked autotune children at agent shutdown."""
    try:
        _at_job.cancel()
    except Exception:
        log.debug("vllm shutdown_children failed", exc_info=True)


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
    ("POST", "/vllm/autotune/run",    vllm_autotune_run),
    ("GET",  "/vllm/autotune/stream", vllm_autotune_stream),
    ("POST", "/vllm/autotune/cancel", vllm_autotune_cancel),
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
