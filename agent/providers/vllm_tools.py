"""vLLM benchmark + autotune routes (#894): vllm bench serve, the live speed-bench and the serve-flag tuner, on llama.py's shared bench/autotune state so one tool runs per host at a time."""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
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
from . import lms_tools as _lt
from . import vllm_autotune as _vat

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
        # Group already gone.
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


_bench_kind: Optional[str] = None   # "live" | "offline" while _bench_active


def _busy() -> bool:
    return bool(_ll._bench_active or _ll._autotune_active)


def _busy_kind() -> Optional[str]:
    """Which run holds the slot: autotune, live, offline, or None."""
    if _ll._autotune_active:
        return "autotune"
    return _bench_kind if _ll._bench_active else None


def _claim(kind: str = "live") -> Optional[dict]:
    """Take the shared bench slot and open a new replay run; the refusal dict when busy."""
    global _bench_kind
    with _ll._bench_lock:
        if _busy():
            return {"ok": False, "error": "Another benchmark or autotune is in progress"}
        _ll._bench_active = True
        _bench_kind = kind
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
    refused = _claim("offline")
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
            "autotune_active": bool(_ll._autotune_active), "quality_active": False}


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
            "busy": _busy(), "busy_kind": _busy_kind()}


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


# ── autotune (#894) ────────────────────────────────────────────────────

_AT_KV_SIZE_RE = re.compile(r"(?:GPU|CPU) KV cache size:\s*([\d,]+)\s*tokens")
_AT_MAX_CONC_RE = re.compile(r"Maximum concurrency for\s*([\d,]+)\s*tokens per request:\s*([\d.]+)x")
_AT_EST_MAX_RE = re.compile(r"estimated maximum model length is\s*([\d,]+)")
_AT_KV_CAP_OLD_RE = re.compile(r"maximum number of tokens that can be stored in KV cache \(([\d,]+)\)")
_AT_DERIVED_MAX_RE = re.compile(r"derived max_model_len \((\d[\d,]*)\)")
_AT_FATAL_RE = re.compile(r"EngineCore failed|Engine core initialization failed|ValueError|"
                          r"RuntimeError|OutOfMemoryError|CUDA out of memory")
_AT_ERR_MSG_RE = re.compile(r"\b[A-Za-z]+Error: \S")
# Grace wait for the max-concurrency line after the KV-size line arrives; lines read after a fatal match.
_AT_CONC_GRACE_S = 8.0
_AT_FATAL_TAIL_LINES = 20
READY_WAIT_S = 60.0
# systemctl restart waits for the stop (TimeoutStopSec, 90 s by default) before the start.
RESTART_TIMEOUT_S = 180
_autotune_thread: Optional[threading.Thread] = None


def _at_num(s: str) -> int:
    return int(s.replace(",", ""))


def _at_watch_journal(unit: str, timeout_s: float, step: str,
                      started: Optional[threading.Event] = None, holder: Optional[dict] = None) -> dict:
    """Follow the unit journal until a KV-capacity answer, engine failure, cancel or timeout;
    emits line + loading_progress events on the shared autotune stream while waiting."""
    res: dict[str, Any] = {"outcome": "timeout"}
    tail = 0
    proc = subprocess.Popen(["journalctl", "-u", unit, "-n", "0", "-f", "-o", "cat"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    _ll._autotune_track_aux(proc)
    if holder is not None:
        holder["proc"] = proc
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
            _ll._autotune_put({"type": "loading_progress", "step": step,
                               "elapsed_s": round(time.monotonic() - start, 1), "timeout_s": timeout_s})
            if time.monotonic() - start > 15 and ticks % 5 == 0:
                try:
                    r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)
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
            if _ll._autotune_cancel_event.is_set():
                res["outcome"] = "cancelled"
                break
            line = _shared.ANSI_RE.sub("", raw).rstrip()
            if not line:
                continue
            _ll._autotune_put({"type": "line", "text": line})
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
            if res["outcome"] == "fatal":
                # A traceback's message line follows its "raise" line; keep the message.
                if _AT_ERR_MSG_RE.search(line):
                    res["fatal_line"] = line[:300]
                    m = _AT_DERIVED_MAX_RE.search(line)
                    if m:
                        res["derived_max"] = _at_num(m.group(1))
                    break
                tail -= 1
                if tail <= 0:
                    break
                continue
            if res["outcome"] != "kv" and _AT_FATAL_RE.search(line):
                res.update(outcome="fatal", fatal_line=line[:300])
                m = _AT_DERIVED_MAX_RE.search(line)
                if m:
                    res["derived_max"] = _at_num(m.group(1))
                if _AT_ERR_MSG_RE.search(line):
                    break
                tail = _AT_FATAL_TAIL_LINES
                killer.cancel()
                killer = threading.Timer(_AT_CONC_GRACE_S, _kill_local)
                killer.daemon = True
                killer.start()
                continue
            if res["outcome"] == "kv" and res.get("max_conc") is not None:
                break
        if _ll._autotune_cancel_event.is_set():
            res["outcome"] = "cancelled"
        elif res["outcome"] == "timeout" and res.get("unit_state"):
            res.update(outcome="fatal", fatal_line=f"unit {unit} is {res['unit_state']} after restart")
    finally:
        hb_stop.set()
        killer.cancel()
        _kill_local()
        hb_thread.join(timeout=6)
        _ll._autotune_untrack_aux()
    return res


def _at_wait_ready(timeout_s: float = READY_WAIT_S) -> Optional[dict]:
    """Poll /v1/models until it lists a model; that row, or None on timeout / cancel."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not _ll._autotune_cancel_event.is_set():
        try:
            r = _v._get_session().get(f"{_url()}/v1/models", timeout=3)
            if r.ok:
                data = (r.json() or {}).get("data") or []
                if data:
                    return data[0]
        except Exception:
            log.debug("vllm readiness poll failed", exc_info=True)
        time.sleep(2)
    return None


def _at_restart_and_watch(unit: str, timeout_s: float, step: str) -> dict:
    """Start the journal watcher in a thread, then restart the unit."""
    holder: dict[str, Any] = {}
    started = threading.Event()
    t = threading.Thread(target=lambda: holder.update(_at_watch_journal(unit, timeout_s, step, started=started, holder=holder)),
                         daemon=True)
    t.start()
    started.wait(timeout=5)
    r = _v._vllm_systemctl("restart", timeout=RESTART_TIMEOUT_S)
    if not r["ok"]:
        proc = holder.get("proc")
        if proc is not None:
            with best_effort("vllm autotune: stop journal watcher", log=log):
                proc.kill()
        t.join(timeout=10)
        return {"outcome": "fatal", "fatal_line": f"systemctl restart failed: {r.get('error')}"}
    t.join(timeout=timeout_s + 15)
    out = {k: v for k, v in holder.items() if k != "proc"}
    return out or {"outcome": "timeout"}


def _head_model(head_tokens: list) -> Optional[str]:
    """The served model from `vllm serve <model> …`."""
    return head_tokens[2] if len(head_tokens) > 2 else None


_TUNED_FLAGS = frozenset(_vat.FLAGS.values()) | frozenset(f for fs in _vat._ALIASES.values() for f in fs)


def _norm(rows: list) -> tuple:
    """Tuned flags as typed config values, the rest as order-free (flag, value) pairs."""
    cfg = {k: _vat.cfg_str(v) for k, v in _vat.config_from_args(rows or []).items()}
    rest = sorted((f, None if v is None else str(v)) for f, v in map(_vat._split_flag, rows or []) if f not in _TUNED_FLAGS)
    return (tuple(sorted(cfg.items())), tuple(rest))


def _first_model_row() -> dict:
    """The first /v1/models row, or {} when the server is unreachable."""
    with best_effort("vllm /v1/models row", log=log):
        r = _v._get_session().get(f"{_url()}/v1/models", timeout=3)
        if r.ok:
            data = (r.json() or {}).get("data") or []
            if data and isinstance(data[0], dict):
                return data[0]
    return {}


def _flag_text(a: dict) -> str:
    return f"{a.get('flag')} {a['value']}" if a.get("value") not in (None, "") else str(a.get("flag"))


class _VllmBackend:
    """Real backend for vllm_autotune._Run: ExecStart edit → restart → journal KV read → /v1/models → speed-bench stick."""

    def __init__(self, model_id: str, run_id: str, unit: str, head_tokens: list, orig_args: list,
                 bench_url: str, load_timeout_s: float):
        self.model_id, self.run_id, self.unit = model_id, run_id, unit
        self.head_tokens, self.orig_args = list(head_tokens), [dict(a) for a in orig_args]
        self.bench_url, self.load_timeout_s = bench_url, float(load_timeout_s)
        self._n = 0
        self.dirty = False

    def drafts(self) -> list:
        return []

    def current(self) -> dict:
        served = _served_ids()
        config = _vat.config_from_args(self.orig_args)
        max_ctx = None
        if "max_model_len" not in config:
            v = _first_model_row().get("max_model_len")
            max_ctx = int(v) if isinstance(v, int) and v > 0 else None
        served_id = self.model_id if self.model_id in served else (served[0] if served else _head_model(self.head_tokens))
        return {"loaded": True, "served": served_id,
                "config": config, "max_ctx": max_ctx}

    def load(self, config: dict, measure: Optional[dict]) -> dict:
        t0 = time.monotonic()
        if _ll._autotune_cancel_event.is_set():
            return {"ok": False, "error": "cancelled"}
        args = _vat.args_with_config(self.orig_args, config)
        _ll._autotune_put({"type": "line", "model_id": self.model_id,
                           "text": "[autotune] restart with " + " ".join(_flag_text(a) for a in args)})
        r = _v._svcconfig_write(self.head_tokens, args)
        if not r.get("ok"):
            return {"ok": False, "error": str(r.get("error") or "svcconfig write failed")[:300]}
        self.dirty = _norm(args) != _norm(self.orig_args)
        watch = _at_restart_and_watch(self.unit, self.load_timeout_s, "load")
        if watch["outcome"] == "cancelled":
            return {"ok": False, "error": "cancelled"}
        if watch["outcome"] == "est_max":
            return {"ok": False, "rejected": True, "est_max_len": watch["est_max_len"],
                    "error": f"engine estimates the maximum model length at {watch['est_max_len']:,}"}
        if watch["outcome"] != "kv":
            err = watch.get("fatal_line") or f"no KV-capacity answer in the journal ({watch['outcome']})"
            out = {"ok": False, "rejected": True, "error": str(err)[:300]}
            if watch.get("derived_max"):
                out["derived_max"] = watch["derived_max"]
            return out
        row = _at_wait_ready(READY_WAIT_S)
        if row is None:
            if _ll._autotune_cancel_event.is_set():
                return {"ok": False, "error": "cancelled"}
            return {"ok": False, "rejected": True, "error": "the server did not answer /v1/models after the restart"}
        out = {"ok": True, "error": None, "config": dict(config),
               "ctx": row.get("max_model_len") or config.get("max_model_len"),
               "kv_tokens": watch.get("kv_tokens"), "max_conc": watch.get("max_conc"),
               "load_s": round(time.monotonic() - t0, 1), "stick": None}
        if measure:
            self._n += 1
            out["stick"] = _lt.stick_run(self.bench_url, self.model_id, self.run_id, self._n, measure)
        return out


def _restore(head_tokens: list, orig_args: list) -> None:
    """Puts the original ExecStart back and restarts the unit."""
    _ll._autotune_put({"type": "line", "text": "[autotune] restoring the original server flags"})
    r = _v._svcconfig_write(head_tokens, orig_args, restart=True, restart_timeout=RESTART_TIMEOUT_S)
    if not r.get("ok"):
        _ll._autotune_put({"type": "rollback_failed", "error": r.get("error") or "rollback restart failed"})


def _autotune_run_all(req: dict, head_tokens: list, orig_args: list) -> None:
    run_id = _ll._autotune_run_id
    unit = _ctx().config.VLLM_SYSTEMD_UNIT
    applied = False
    dirty = False
    try:
        with _shim.Shim(_url(), native=False, label="vLLM") as sh:
            rt = _ll._bench_live_runtime()
            env = {"run_id": run_id, "runtime": bool(rt["python"] and rt["script"]), "provider": "vllm"}
            try:
                for mid in req["model_ids"]:
                    if _ll._autotune_cancel_event.is_set():
                        break
                    backend = _VllmBackend(mid, run_id, unit, head_tokens, orig_args, sh.url, req["load_timeout_s"])
                    done = _vat.run_model(mid, req, backend, _ll._autotune_put, _ll._autotune_cancel_event.is_set, env)
                    applied = applied or bool(done.get("applied"))
                    dirty = dirty or backend.dirty
                    _shared.post_tool_run(_ctx(), "autotune", "vllm", run_id, mid, done["ok"], _vat.ledger_summary(done))
            finally:
                # Restore even after a cancel: the cancel flag only stops measuring, not the rollback.
                was_cancel = _ll._autotune_cancel_event.is_set()
                _ll._autotune_cancel_event.clear()
                if not applied and dirty:
                    with best_effort("vllm autotune: restore ExecStart", log=log):
                        _restore(head_tokens, orig_args)
                if was_cancel:
                    _ll._autotune_cancel_event.set()
        cancelled = _ll._autotune_cancel_event.is_set()
        _ll._autotune_put({"type": "done", "ok": not cancelled, "cancelled": cancelled, "count": len(req["model_ids"])})
    except Exception as e:
        log.error("vllm autotune run error: %s", e, exc_info=True)
        _ll._autotune_put({"type": "done", "ok": False, "error": str(e)})
    finally:
        with best_effort("vllm autotune: drop run scratch dir", log=log):
            shutil.rmtree(_bl.bench_dir(_ctx().config.AGENT_INSTALL_DIR) / "runs" / f"at-{run_id}", ignore_errors=True)
        _ll._autotune_untrack_aux()
        with _ll._autotune_lock:
            _ll._autotune_active = False
            _ll._autotune_quality = False


def _unit_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", _ctx().config.VLLM_SYSTEMD_UNIT],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return (r.stdout or "").strip() == "active"


def vllm_autotune_preflight(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """What the tuner can do on this host: server state, the served model with its serve flags, the bench runtime."""
    _ctx().check_bearer(authorization); _v._vllm_check_enabled()
    rt = _ll._bench_live_runtime()
    server = _bench_server()
    return {"ok": True, "provider": "vllm", "busy": _busy(), "autotune_active": bool(_ll._autotune_active),
            "quality_active": False, "unit_active": _unit_active(), "server": server,
            "models": [{"key": m["id"], "loaded": True, "type": "llm"} for m in server["models"]],
            "config": _vat.config_from_args(_unit_args()), "drafts_for": {},
            "runtime": {"ok": bool(rt["python"] and rt["script"]), **rt},
            "ram_total_mb": _ll._ram_total_mb(), "free_mb": None, "vram_total_mb": None}


def vllm_autotune_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _v._vllm_check_enabled()
    body = body or {}
    served = _served_ids()
    if _vat.is_legacy(body):
        if not served:
            return {"ok": False, "error": "vLLM server is not running — start it before auto-tune"}
        body = _vat.legacy_body(body, served[0])
    try:
        req = _vat.validate_request(body)
    except (ValueError, TypeError, AttributeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not served:
        return {"ok": False, "error": "vLLM server is not running — start it before auto-tune"}
    missing = [m for m in req["model_ids"] if m not in served]
    if missing:
        return {"ok": False, "error": f"vLLM does not serve {missing[0]}"}
    try:
        head, args = _v._parse_vllm_execstart(Path(_v._vllm_svc_file_path()).read_text())
    except OSError as e:
        return {"ok": False, "error": f"service file unreadable: {e}"}
    if head is None:
        return {"ok": False, "error": "ExecStart line not found in service file"}
    with _ll._autotune_lock:
        if _ll._autotune_active or _ll._bench_active:
            return {"ok": False, "error": "Another benchmark or auto-tune is in progress"}
        _ll._autotune_cancel_event.clear()
        _ll._autotune_active = True
        _ll._autotune_quality = False
        _ll._autotune_run_id = uuid.uuid4().hex[:12]
        with _ll._autotune_cond:
            _ll._autotune_replay.start_run(_ll._autotune_run_id)
    global _autotune_thread
    _autotune_thread = threading.Thread(target=_autotune_run_all, args=(req, shlex.split(head), args), daemon=True)
    _autotune_thread.start()
    return {"ok": True, "run_id": _ll._autotune_run_id}


def shutdown_children() -> None:
    """Wait for a running vLLM autotune to finish its ExecStart rollback."""
    t = _autotune_thread
    if t is not None and t.is_alive():
        t.join(timeout=45)


def vllm_autotune_stream(authorization: Optional[str] = Header(default=None), token: Optional[str] = Query(default=None),
                         last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID")) -> StreamingResponse:
    _ctx().check_stream_auth(authorization, token, "/vllm/autotune/stream"); _v._vllm_check_enabled()
    return _shared.bench_replay_sse(_ll._autotune_replay, _ll._autotune_cond, lambda: _ll._autotune_active, last_event_id)


def vllm_autotune_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _v._vllm_check_enabled()
    return _ll._autotune_cancel_impl([])


_ROUTES: tuple = (
    ("GET",  "/vllm/bench/live/preflight", vllm_bench_live_preflight),
    ("POST", "/vllm/bench/live/setup",     vllm_bench_live_setup),
    ("POST", "/vllm/bench/live/run",       vllm_bench_live_run),
    ("POST", "/vllm/bench/run",    vllm_bench_run),
    ("GET",  "/vllm/bench/stream", vllm_bench_stream),
    ("POST", "/vllm/bench/cancel", vllm_bench_cancel),
    ("GET",  "/vllm/autotune/preflight", vllm_autotune_preflight),
    ("POST", "/vllm/autotune/run",       vllm_autotune_run),
    ("GET",  "/vllm/autotune/stream",    vllm_autotune_stream),
    ("POST", "/vllm/autotune/cancel",    vllm_autotune_cancel),
    ("GET",  "/vllm/tools/state",  vllm_tools_state),
)


def set_context(ctx) -> None:
    """State lives in vllm.py and llama.py; kept for providers.configure_all symmetry."""
    return None


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
