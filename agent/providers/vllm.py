"""vLLM provider — systemd lifecycle, Prometheus metrics, journal log,
svcconfig, OpenAI passthrough, opt-in LoRA."""

from __future__ import annotations

import json
import logging
import math
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from _bench_replay import BenchReplayBuffer  # type: ignore[import-not-found]  # sibling at agent root

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


def _svcconfig_write(head_tokens: list, args: list, restart: bool = False) -> dict[str, Any]:
    """Token build + wrapper apply, optionally restarting the unit."""
    tokens = _shared.build_svcconfig_tokens(head_tokens, args)
    if not tokens:
        return {"ok": False, "error": "invalid ExecStart token (newline or non-string)"}
    return _shared.svcconfig_apply(
        _VLLM_SVCCONFIG_WRAPPER, _vllm_svc_file_path(), tokens,
        sudoers_hint="llm-vllm-svcconfig-apply",
        restart_unit=_require_ctx().config.VLLM_SYSTEMD_UNIT if restart else None,
        restart_timeout=60)


def vllm_svcconfig_post(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _vllm_check_enabled()
    return _svcconfig_write(shlex.split(body.get("binary", "") or ""),
                            body.get("args", []), restart=bool(body.get("restart")))


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

_AT_KV_SIZE_RE = re.compile(r"GPU KV cache size:\s*([\d,]+)\s*tokens")
_AT_MAX_CONC_RE = re.compile(
    r"Maximum concurrency for\s*([\d,]+)\s*tokens per request:\s*([\d.]+)x")
_AT_EST_MAX_RE = re.compile(r"estimated maximum model length is\s*([\d,]+)")
_AT_KV_CAP_OLD_RE = re.compile(
    r"maximum number of tokens that can be stored in KV cache \(([\d,]+)\)")
_AT_FATAL_RE = re.compile(
    r"EngineCore failed|Engine core initialization failed|ValueError|"
    r"RuntimeError|OutOfMemoryError|CUDA out of memory")

# Grace wait for the max-concurrency line after the KV-size line arrives.
_AT_CONC_GRACE_S = 8.0


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
    """Current --max-model-len value; handles both '--flag v' and '--flag=v'."""
    for a in args:
        flag = str(a.get("flag") or "")
        raw = None
        if flag == "--max-model-len" and a.get("value"):
            raw = str(a["value"])
        elif flag.startswith("--max-model-len="):
            raw = flag.split("=", 1)[1]
        if raw is not None:
            try:
                return _at_num(raw)
            except ValueError:
                return None
    return None


def _at_args_with_max_len(args: list, value: int) -> list:
    """Args with --max-model-len (either form) replaced by the given value."""
    out = [dict(a) for a in args
           if a.get("flag") != "--max-model-len"
           and not str(a.get("flag") or "").startswith("--max-model-len=")]
    out.append({"flag": "--max-model-len", "value": str(value), "bool": False})
    return out


_at_job = _shared.JobRunner("vllm-autotune")


def _at_watch_journal(unit: str, timeout_s: float, step: str,
                      started: Optional[threading.Event] = None) -> dict:
    """Follow the unit journal until a KV-capacity answer, engine failure,
    cancel, or timeout; emits line + loading_progress events while waiting."""
    res: dict[str, Any] = {"outcome": "timeout"}
    proc = subprocess.Popen(
        ["journalctl", "-u", unit, "-n", "0", "-f", "-o", "cat"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True)
    _at_job.track(proc)
    if started is not None:
        started.set()

    def _kill_local():
        try:
            proc.kill()
        except Exception:
            log.debug("journal watcher kill failed", exc_info=True)

    killer = threading.Timer(timeout_s, _kill_local)
    killer.daemon = True
    killer.start()
    hb_stop = threading.Event()

    def _hb():
        start = time.monotonic()
        ticks = 0
        while not hb_stop.wait(2.0):
            ticks += 1
            _at_job.put({"type": "loading_progress", "step": step,
                         "elapsed_s": round(time.monotonic() - start, 1),
                         "timeout_s": timeout_s})
            if time.monotonic() - start > 15 and ticks % 5 == 0:
                try:
                    r = subprocess.run(["systemctl", "is-active", unit],
                                       capture_output=True, text=True, timeout=5)
                    if (r.stdout or "").strip() == "failed":
                        res["unit_state"] = "failed"
                        _kill_local()
                        return
                except Exception:
                    log.debug("is-active poll failed", exc_info=True)

    hb_thread = threading.Thread(target=_hb, daemon=True)
    hb_thread.start()
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, ""):
            if _at_job.cancel_event.is_set():
                res["outcome"] = "cancelled"
                break
            line = _shared.ANSI_RE.sub("", raw).rstrip()
            if not line:
                continue
            _at_job.put({"type": "line", "text": line})
            m = _AT_MAX_CONC_RE.search(line)
            if m:
                res["max_conc"] = float(m.group(2))
            m = _AT_KV_SIZE_RE.search(line)
            if m and res["outcome"] != "kv":
                res.update(outcome="kv", kv_tokens=_at_num(m.group(1)))
                # Quiet journals block readline; a short timer forces EOF.
                killer.cancel()
                killer = threading.Timer(_AT_CONC_GRACE_S, _kill_local)
                killer.daemon = True
                killer.start()
            m = _AT_EST_MAX_RE.search(line) or _AT_KV_CAP_OLD_RE.search(line)
            if m and res["outcome"] != "kv":
                res.update(outcome="est_max", est_max_len=_at_num(m.group(1)))
                break
            if res["outcome"] != "kv" and _AT_FATAL_RE.search(line):
                res.update(outcome="fatal", fatal_line=line[:300])
                break
            if res["outcome"] == "kv" and res.get("max_conc") is not None:
                break
        if _at_job.cancel_event.is_set():
            res["outcome"] = "cancelled"
        elif res["outcome"] == "timeout" and res.get("unit_state"):
            res.update(outcome="fatal",
                       fatal_line=f"unit {unit} is {res['unit_state']} after restart")
    finally:
        hb_stop.set()
        killer.cancel()
        _kill_local()
        hb_thread.join(timeout=6)
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
            log.debug("vllm readiness poll failed", exc_info=True)
        time.sleep(2)
    return None


def _at_apply(head_tokens: list, args: list) -> dict:
    """svcconfig write (daemon-reload inside the wrapper), no restart."""
    return _svcconfig_write(head_tokens, args)


def _at_restart_and_watch(unit: str, timeout_s: float, step: str) -> dict:
    """Start the journal watcher in a thread, then restart the unit."""
    holder: dict[str, Any] = {}
    started = threading.Event()
    t = threading.Thread(
        target=lambda: holder.update(_at_watch_journal(unit, timeout_s, step,
                                                       started=started)),
        daemon=True)
    t.start()
    started.wait(timeout=5)
    r = _vllm_systemctl("restart", timeout=60)
    if not r["ok"]:
        _at_job.kill_tracked()
        t.join(timeout=10)
        return {"outcome": "fatal",
                "fatal_line": f"systemctl restart failed: {r.get('error')}"}
    t.join(timeout=timeout_s + 15)
    return holder or {"outcome": "timeout"}


def _at_run(params: dict) -> None:
    """Job thread: probe → compute → apply+verify (or report-only) → rollback."""
    ctx = _require_ctx()
    unit = ctx.config.VLLM_SYSTEMD_UNIT
    ok = False
    mutated = False
    head_tokens: list = []
    orig_args: list = []
    orig_max: Optional[int] = None

    def _fail(error: Optional[str]) -> None:
        _at_job.put({"type": "model_done", "ok": False, "applied": False,
                     "error": error})

    try:
        content = Path(_vllm_svc_file_path()).read_text()
        head, orig_args = _parse_vllm_execstart(content)
        if head is None:
            _fail("ExecStart line not found in service file")
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
            _fail(r.get("error"))
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
            _fail(watch.get("fatal_line")
                  or f"probe {watch['outcome']} — no KV-capacity answer in journal")
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
                         "kv_tokens": kv_tokens,
                         "max_concurrency_x": watch.get("max_conc"),
                         "original_max_len": orig_max})
            ok = True
            return

        _at_job.put({"type": "step_start", "step": "apply", "max_model_len": rec})
        r = _at_apply(head_tokens, _at_args_with_max_len(orig_args, rec))
        if not r["ok"]:
            _fail(r.get("error"))
            return
        watch = _at_restart_and_watch(unit, params["load_timeout_s"], "verify")
        if watch["outcome"] == "cancelled":
            return
        if watch["outcome"] != "kv":
            _fail(watch.get("fatal_line")
                  or f"verify {watch['outcome']} at max-model-len={rec}")
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
                     "max_concurrency_x": watch.get("max_conc"),
                     "original_max_len": orig_max})
    except Exception as e:
        _fail(str(e))
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
    def _num(key: str, default, cast):
        v = body.get(key)
        return cast(default if v is None else v)

    try:
        params = {
            "probe_len": _num("probe_len", 4096, int),
            "concurrency": _num("concurrency", 1.0, float),
            "kv_fraction": _num("kv_fraction", 1.0, float),
            "load_timeout_s": _num("load_timeout_s", 600, int),
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
    _at_job.cancel()
    return {"ok": True, "active": _at_job.active}


def shutdown_children() -> None:
    """Cancel running bench/autotune jobs; wait for autotune's ExecStart rollback."""
    try:
        _bench_job.cancel()
    except Exception:
        log.debug("vllm bench shutdown cancel failed", exc_info=True)
    try:
        _at_job.cancel()
        _at_job.join(timeout=45)
    except Exception:
        log.debug("vllm shutdown_children failed", exc_info=True)


# ── Benchmark (vllm bench serve) (#357) ────────────────────────────────

_BENCH_METRIC_KEYS = (
    "request_throughput", "output_throughput", "total_token_throughput",
    "mean_ttft_ms", "median_ttft_ms", "p99_ttft_ms",
    "mean_tpot_ms", "median_tpot_ms", "p99_tpot_ms",
    "mean_itl_ms", "median_itl_ms", "p99_itl_ms",
    "completed", "duration", "total_input_tokens", "total_output_tokens",
)


def _bench_resolve_bin() -> "tuple[Optional[str], Optional[str]]":
    """(vllm binary path, None) or (None, error). Order: VLLM_BENCH_BIN
    override → svcconfig ExecStart head binary → PATH lookup."""
    cfg = _require_ctx().config
    override = (getattr(cfg, "VLLM_BENCH_BIN", "") or "").strip()
    if override:
        if Path(override).exists() or shutil.which(override):
            return override, None
        return None, f"VLLM_BENCH_BIN not found: {override}"
    try:
        head, _ = _parse_vllm_execstart(Path(_vllm_svc_file_path()).read_text())
        if head:
            cand = shlex.split(head)[0]
            if Path(cand).exists():
                return cand, None
    except Exception:
        log.debug("bench bin: svcconfig head unavailable", exc_info=True)
    w = shutil.which("vllm")
    if w:
        return w, None
    return None, ("vllm binary not found — set VLLM_BENCH_BIN in the agent "
                  "config or pip install vllm[bench] on this host")


def _bench_build_cmd(binpath: str, api_url: str, model: str,
                     switches: list, result_dir: str) -> list:
    cmd = [binpath, "bench", "serve",
           "--base-url", api_url, "--model", model,
           "--save-result", "--result-dir", result_dir,
           "--result-filename", "result.json"]
    for s in switches:
        flag = str(s.get("flag") or "").strip()
        if not flag:
            continue
        cmd.append(flag)
        value = s.get("value")
        if value not in (None, ""):
            cmd.append(str(value))
    return cmd


def _bench_extract_metrics(data: dict) -> dict:
    return {k: data[k] for k in _BENCH_METRIC_KEYS
            if isinstance(data.get(k), (int, float))
            and not isinstance(data.get(k), bool)}


def _bench_extract_extra(data: dict) -> dict:
    return {k: v for k, v in data.items()
            if isinstance(v, (int, float, str, bool))}


_bench_job = _shared.JobRunner("vllm-bench")
_bench_replay = BenchReplayBuffer(maxlen=5000)
_bench_cond = threading.Condition()


def _bench_put(event: dict) -> None:
    with _bench_cond:
        _bench_replay.append(event)
        _bench_cond.notify_all()


def _bench_run_one(binpath: str, model: str, switches: list) -> None:
    """Job thread: run vllm bench serve, stream lines, parse the result JSON."""
    ok = False
    rc: Optional[int] = None
    error: Optional[str] = None
    tmpdir = tempfile.mkdtemp(prefix="vllm-bench-")
    try:
        cmd = _bench_build_cmd(binpath, _require_ctx().config.VLLM_API_URL.rstrip("/"),
                               model, switches, tmpdir)
        _bench_put({"type": "model_start", "model": model,
                    "cmd": " ".join(shlex.quote(t) for t in cmd)})
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        _bench_job.track(proc)
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, ""):
            if _bench_job.cancel_event.is_set():
                break
            line = _shared.ANSI_RE.sub("", raw).rstrip()
            if line:
                _bench_put({"type": "line", "text": line})
        rc = proc.wait(timeout=30)
        cancelled = _bench_job.cancel_event.is_set()
        if rc == 0 and not cancelled:
            try:
                data = json.loads(Path(tmpdir, "result.json").read_text())
                _bench_put({"type": "result", "model_id": model,
                            "metrics": _bench_extract_metrics(data),
                            "extra": _bench_extract_extra(data),
                            "switches": switches})
                ok = True
            except Exception as e:
                error = f"benchmark finished but result.json unreadable: {e}"
        elif not cancelled:
            error = f"vllm bench serve exited rc={rc}"
        _bench_put({"type": "model_done", "ok": ok, "rc": rc,
                    "cancelled": cancelled, "error": error})
    except Exception as e:
        _bench_put({"type": "model_done", "ok": False, "rc": rc,
                    "cancelled": _bench_job.cancel_event.is_set(),
                    "error": str(e)})
    finally:
        _bench_job.untrack()
        shutil.rmtree(tmpdir, ignore_errors=True)
        _bench_put({"type": "done", "ok": ok,
                    "cancelled": _bench_job.cancel_event.is_set()})


def vllm_bench_run(body: dict,
                   authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    ctx = _require_ctx()
    ctx.check_bearer(authorization)
    _vllm_check_enabled()
    switches = body.get("switches", [])
    if not isinstance(switches, list):
        raise HTTPException(status_code=400, detail="switches must be a list")
    base = ctx.config.VLLM_API_URL.rstrip("/")
    served: list = []
    try:
        r = _get_session().get(f"{base}/v1/models", timeout=3)
        if r.ok:
            served = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    except Exception:
        pass
    if not served:
        return {"ok": False,
                "error": "vLLM server is not running — start it before benchmarking"}
    model = (body.get("model") or "").strip() or served[0]
    binpath, err = _bench_resolve_bin()
    if not binpath:
        return {"ok": False, "error": err}
    if _bench_job.active:
        return {"ok": False, "error": "Another benchmark is in progress"}
    with _bench_cond:
        _bench_replay.start_run(uuid.uuid4().hex[:12])
    if not _bench_job.try_start(lambda: _bench_run_one(binpath, model, switches)):
        return {"ok": False, "error": "Another benchmark is in progress"}
    return {"ok": True}


def vllm_bench_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    _require_ctx().check_stream_auth(authorization, token, "/vllm/bench/stream")
    _vllm_check_enabled()

    def generate():
        with _bench_cond:
            cur_run = _bench_replay.run_id
            last_seq = _bench_replay.seq_for(last_event_id)
        while True:
            with _bench_cond:
                if cur_run and _bench_replay.run_id != cur_run:
                    return
                cur_run = _bench_replay.run_id
                new = _bench_replay.records_after_seq(last_seq)
                if not new:
                    _bench_cond.wait(timeout=10)
                    if cur_run and _bench_replay.run_id != cur_run:
                        return
                    new = _bench_replay.records_after_seq(last_seq)
            if not new:
                if not _bench_job.active:
                    yield (b'data: {"type":"done","ok":false,'
                           b'"error":"no active job"}\n\n')
                    return
                yield b'data: {"type":"keepalive"}\n\n'
                continue
            for rec in new:
                yield f"id: {rec['id']}\ndata: {json.dumps(rec['event'])}\n\n".encode()
                last_seq = rec["seq"]
                if rec["event"].get("type") == "done":
                    return

    return _shared.pool_guarded_sse(generate())


def vllm_bench_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    _vllm_check_enabled()
    _bench_job.cancel()
    return {"ok": True, "active": _bench_job.active}


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
    ("POST", "/vllm/bench/run",       vllm_bench_run),
    ("GET",  "/vllm/bench/stream",    vllm_bench_stream),
    ("POST", "/vllm/bench/cancel",    vllm_bench_cancel),
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
