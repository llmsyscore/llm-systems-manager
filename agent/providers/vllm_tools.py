"""vLLM benchmark routes (#894): vllm bench serve and the live speed-bench, on llama.py's shared
bench state so one benchmark or autotune runs per host at a time."""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling at agent root
from fastapi import Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from . import _shared
from . import llama as _ll
from . import llama_bench_live as _bl
from . import lms_timings_shim as _shim
from . import vllm as _v

log = logging.getLogger("llm-systems-agent.providers.vllm_tools")


def _ctx():
    return _v._require_ctx()


def _url() -> str:
    return (_ctx().config.VLLM_API_URL or "").rstrip("/")


# ── Benchmark (vllm bench serve) (#357) ────────────────────────────────

def _bench_resolve_bin() -> "tuple[Optional[str], Optional[str]]":
    """(vllm binary path, None) or (None, error). Order: VLLM_BENCH_BIN
    override → svcconfig ExecStart head binary → PATH lookup."""
    cfg = _ctx().config
    override = (getattr(cfg, "VLLM_BENCH_BIN", "") or "").strip()
    if override:
        if Path(override).exists() or shutil.which(override):
            return override, None
        return None, f"VLLM_BENCH_BIN not found: {override}"
    try:
        head, _ = _v._parse_vllm_execstart(Path(_v._vllm_svc_file_path()).read_text())
        if head:
            cand = shlex.split(head)[0]
            # Interpreter heads (python -m vllm ...) can't run "bench serve".
            if Path(cand).name == "vllm" and Path(cand).exists():
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
           "--disable-tqdm",
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


def _bench_extract_extra(data: dict) -> dict:
    """Scalar fields only — drops per-request arrays from the result JSON."""
    return {k: v for k, v in data.items()
            if isinstance(v, (int, float, str, bool))}


def _bench_summary(extra: dict) -> dict:
    """Ledger throughput fields from a vllm bench serve result (#780)."""
    return {"gen_tps": extra.get("output_throughput"),
            "pg_tps": extra.get("total_token_throughput"),
            "bench_tool": "vllm-bench-serve"}


def _bench_model_done(model, ok, rc, cancelled, error, extra, switches=None) -> None:
    summary = _bench_summary(extra or {})
    _ll._bench_put({"type": "model_done", "ok": ok, "rc": rc, "cancelled": cancelled, "error": error,
                    "model_id": model, "run_id": _ll._bench_replay.run_id, **summary})
    _shared.post_tool_run(_ctx(), "benchmark", "vllm", _ll._bench_replay.run_id, model, ok,
                          {**summary, "switches": _shared.switch_map(switches or [])})


def _killpg(proc) -> None:
    """SIGKILL the bench process group, tolerating an already-gone group."""
    try:
        os.killpg(_ll._bench_pgid or proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception as e:
        log.warning("vllm bench: killpg failed: %s", e)


def _bench_run_one(binpath: str, model: str, switches: list) -> None:
    """Job thread: run vllm bench serve, stream lines, parse the result JSON."""
    ok, rc, error, extra = False, None, None, {}
    tmpdir = tempfile.mkdtemp(prefix="vllm-bench-")
    try:
        cmd = _bench_build_cmd(binpath, _url(), model, switches, tmpdir)
        _ll._bench_put({"type": "model_start", "model": model, "cmd": " ".join(shlex.quote(t) for t in cmd)})
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try: _ll._bench_pgid = os.getpgid(proc.pid)
        except Exception: _ll._bench_pgid = proc.pid
        _ll._bench_proc = proc
        assert proc.stdout is not None
        if _ll._bench_cancel_event.is_set():
            _killpg(proc)
        else:
            for raw in iter(proc.stdout.readline, ""):
                if _ll._bench_cancel_event.is_set():
                    break
                line = _shared.ANSI_RE.sub("", raw).rstrip()
                if line:
                    _ll._bench_put({"type": "line", "text": line})
            if _ll._bench_cancel_event.is_set():
                _killpg(proc)
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            try: rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired: error = "benchmark process did not exit after kill"
        cancelled = _ll._bench_cancel_event.is_set()
        if rc == 0 and not cancelled and error is None:
            try:
                extra = _bench_extract_extra(json.loads(Path(tmpdir, "result.json").read_text()))
                _ll._bench_put({"type": "result", "model_id": model, "extra": extra, "switches": switches,
                                "run_id": _ll._bench_replay.run_id})
                ok = True
            except Exception as e:
                error = f"benchmark finished but result.json unreadable: {e}"
        elif not cancelled and error is None:
            error = f"vllm bench serve exited rc={rc}"
        _bench_model_done(model, ok, rc, cancelled, error, extra, switches)
    except Exception as e:
        _bench_model_done(model, False, rc, _ll._bench_cancel_event.is_set(), str(e), {}, switches)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _ll._bench_put({"type": "done", "ok": ok, "cancelled": _ll._bench_cancel_event.is_set()})
        _ll._bench_proc = None; _ll._bench_pgid = None
        with _ll._bench_lock:
            _ll._bench_active = False


def _busy() -> bool:
    return bool(_ll._bench_active or _ll._autotune_active or _v._at_job.active)


def _claim() -> Optional[dict]:
    """Take the shared bench slot and open a new replay run; the refusal dict when busy."""
    with _ll._bench_lock:
        if _busy():
            return {"ok": False, "error": "Another benchmark or autotune is in progress"}
        _ll._bench_active = True
        with _ll._bench_cond:
            _ll._bench_replay.start_run(uuid.uuid4().hex[:12])
    _ll._bench_cancel_event.clear()
    return None


def _served_ids() -> list:
    try:
        r = _v._get_session().get(f"{_url()}/v1/models", timeout=3)
        if r.ok:
            return [m.get("id") for m in ((r.json() or {}).get("data") or []) if isinstance(m, dict) and m.get("id")]
    except Exception:
        log.debug("vllm /v1/models unreachable", exc_info=True)
    return []


def vllm_bench_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _v._vllm_check_enabled()
    switches = body.get("switches", [])
    if not isinstance(switches, list):
        raise HTTPException(status_code=400, detail="switches must be a list")
    served = _served_ids()
    if not served:
        return {"ok": False, "error": "vLLM server is not running — start it before benchmarking"}
    model = (body.get("model") or "").strip() or served[0]
    binpath, err = _bench_resolve_bin()
    if not binpath:
        return {"ok": False, "error": err}
    refused = _claim()
    if refused:
        return refused
    threading.Thread(target=_bench_run_one, args=(binpath, model, switches), daemon=True).start()
    return {"ok": True, "run_id": _ll._bench_replay.run_id}


def vllm_bench_stream(authorization: Optional[str] = Header(default=None), token: Optional[str] = Query(default=None),
                      last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID")) -> StreamingResponse:
    _ctx().check_stream_auth(authorization, token, "/vllm/bench/stream"); _v._vllm_check_enabled()
    return _shared.bench_replay_sse(_ll._bench_replay, _ll._bench_cond, lambda: _ll._bench_active, last_event_id)


def vllm_bench_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _v._vllm_check_enabled()
    return _ll._bench_cancel_impl([])


def vllm_tools_state(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Whether a bench/autotune job is running, for the manager Tools view."""
    _ctx().check_bearer(authorization)
    return {"ok": True, "bench_active": bool(_ll._bench_active),
            "autotune_active": bool(_v._at_job.active or _ll._autotune_active), "quality_active": False}


# ── live benchmark (speed-bench) ───────────────────────────────────────

DEFAULT_MAX_NUM_SEQS = 256


def _unit_args() -> list:
    """Flag list parsed from the unit's ExecStart; [] when the unit file is absent."""
    try:
        _head, args = _v._parse_vllm_execstart(Path(_v._vllm_svc_file_path()).read_text())
        return args
    except (OSError, ValueError):
        return []


def _arg_value(args: list, flag: str) -> Optional[str]:
    return next((str(a.get("value")) for a in args if a.get("flag") == flag and a.get("value") is not None), None)


def _max_num_seqs(args: list) -> int:
    v = _arg_value(args, "--max-num-seqs")
    try:
        return max(1, int(v)) if v else DEFAULT_MAX_NUM_SEQS
    except ValueError:
        return DEFAULT_MAX_NUM_SEQS


def _spec_of(args: list) -> Optional[dict]:
    raw = _arg_value(args, "--speculative-config") or _arg_value(args, "-sc")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except ValueError:
        return {"method": "custom", "n": None}
    if not isinstance(cfg, dict):
        return None
    n = cfg.get("num_speculative_tokens")
    return {"method": str(cfg.get("method") or ("draft_model" if cfg.get("model") else "unknown")),
            "n": int(n) if isinstance(n, (int, float)) else None}


def _requests_running() -> int:
    with best_effort("vllm tools: metrics", log=log):
        m = _v._get_session().get(f"{_url()}/metrics", timeout=3)
        if m.ok:
            n = _v._fam_sum(_v._parse_prom_families(m.text), "vllm:num_requests_running")
            return int(n) if n is not None else 0
    return 0


def _build() -> str:
    with best_effort("vllm tools: version", log=log):
        r = _v._get_session().get(f"{_url()}/version", timeout=3)
        if r.ok:
            return str((r.json() or {}).get("version") or "")[:64]
    return ""


def _bench_server() -> dict:
    """Running-server snapshot in the live bench's shape: served ids, slots from --max-num-seqs, spec, build."""
    out: dict = {"up": False, "url": _url(), "provider": "vllm", "models": [], "loaded_id": None,
                 "slots_idle": 0, "slots_total": 0, "spec": None, "build": ""}
    ids = _served_ids()
    if not ids:
        return out
    out["up"] = True
    out["models"] = [{"id": i, "status": "loaded"} for i in ids]
    out["loaded_id"] = ids[0]
    args = _unit_args()
    out["slots_total"] = _max_num_seqs(args)
    out["slots_idle"] = max(0, out["slots_total"] - _requests_running())
    out["spec"] = _spec_of(args)
    out["build"] = _build()
    return out


def vllm_bench_live_preflight(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _v._vllm_check_enabled()
    cfg = _ctx().config
    return {"ok": True, "provider": "vllm", "server": _bench_server(), "runtime": _ll._bench_live_runtime(),
            "datasets": _bl.read_marker(cfg.AGENT_INSTALL_DIR), "benches": list(_bl.BENCHES),
            "busy": _busy()}


def vllm_bench_live_setup(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _v._vllm_check_enabled()
    benches = [b for b in (body.get("prefetch") or []) if b in _bl.BENCHES]
    refused = _claim()
    if refused:
        return refused
    threading.Thread(target=_ll._bench_live_setup_job, args=(benches,), daemon=True).start()
    return {"ok": True, "run_id": _ll._bench_replay.run_id}


def vllm_bench_live_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _v._vllm_check_enabled()
    try:
        req = _bl.validate_run_request(body or {})
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if _busy():
        return {"ok": False, "error": "Another benchmark or autotune is in progress"}
    server = _bench_server()
    if not server["up"]:
        return {"ok": False, "error": "vLLM server is not running"}
    if not any(m.get("id") == req["model_id"] for m in server["models"]):
        return {"ok": False, "error": f"vLLM does not serve {req['model_id']}"}
    rt = _ll._bench_live_runtime()
    if not rt["python"] or not rt["script"]:
        return {"ok": False, "error": "speed-bench runtime not installed", "runtime": rt}
    refused = _claim()
    if refused:
        return refused
    threading.Thread(target=_bench_run_shimmed, args=(req, server, rt["python"], rt["script"]), daemon=True).start()
    return {"ok": True, "run_id": _ll._bench_replay.run_id}


def _bench_run_shimmed(req: dict, server: dict, python: str, script: str) -> None:
    """The live run behind the timings shim in streaming mode: vLLM reports no prefill/decode timings."""
    started = False
    try:
        with _shim.Shim(server["url"], native=False, label="vLLM") as sh:
            srv = dict(server, url=sh.url, lms_url=server["url"], timings="shim")
            _ll._bench_put({"type": "line", "model_id": req["model_id"],
                            "text": "[vllm] timings: OpenAI streaming marks (first token / last token)"})
            started = True
            _ll._bench_live_run_all(req, srv, python, script, provider="vllm")
    except Exception as e:
        if started:
            raise
        _bench_start_failed(req["model_id"], e)


def _bench_start_failed(model_id: str, e: Exception) -> None:
    """Close the run and free the bench slot when the shim fails before the run starts."""
    _ll._bench_put({"type": "line", "model_id": model_id, "text": f"error: {e}"})
    _ll._bench_put({"type": "done", "ok": False, "cancelled": False, "count": 0})
    _ll._bench_proc = None; _ll._bench_pgid = None
    with _ll._bench_lock:
        _ll._bench_active = False


_ROUTES: tuple = (
    ("GET",  "/vllm/bench/live/preflight", vllm_bench_live_preflight),
    ("POST", "/vllm/bench/live/setup",     vllm_bench_live_setup),
    ("POST", "/vllm/bench/live/run",       vllm_bench_live_run),
    ("POST", "/vllm/bench/run",    vllm_bench_run),
    ("GET",  "/vllm/bench/stream", vllm_bench_stream),
    ("POST", "/vllm/bench/cancel", vllm_bench_cancel),
    ("GET",  "/vllm/tools/state",  vllm_tools_state),
)


def set_context(ctx) -> None:
    """State lives in vllm.py and llama.py; kept for providers.configure_all symmetry."""
    return None


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
