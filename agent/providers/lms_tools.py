"""LM Studio benchmark + autotune routes (#916): live speed-bench against the LM Studio server and
the load-config tuner. Shares llama.py's bench/autotune run state: one tool per host at a time."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from typing import Any, Optional

import requests
from fastapi import Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling at agent root

from . import _shared
from . import llama as _ll
from . import llama_autotune as _at
from . import llama_bench_live as _bl
from . import lms as _lms
from . import lms_autotune as _lat
from . import lms_timings_shim as _shim

log = logging.getLogger("llm-systems-agent.providers.lms_tools")

INSTANCE_WAIT_S = 45
UNLOAD_WAIT_S = 30
# ~1k tokens of prompt so the warm-up exercises the prompt batch, as serving traffic would.
WARM_PROMPT = ("The quick brown fox jumps over the lazy dog while the sun sets behind the hills. " * 70) + "\nSummarize that in one line."


def _ctx():
    return _lms._require_ctx()


def _url() -> str:
    return (_ctx().config.LMS_API_URL or "").rstrip("/")


def _session() -> requests.Session:
    return _lms._get_session()


def _native_models() -> Optional[list]:
    """Rows of GET /api/v1/models; None when LM Studio is down or has no native API."""
    try:
        r = _session().get(f"{_url()}/api/v1/models", timeout=5)
        if not r.ok:
            return None
        rows = (r.json() or {}).get("models")
    except (requests.RequestException, ValueError):
        return None
    return rows if isinstance(rows, list) else None


def _entry(models: Optional[list], model_id: str) -> Optional[dict]:
    for m in models or []:
        if not isinstance(m, dict):
            continue
        if m.get("key") == model_id:
            return m
        for inst in m.get("loaded_instances") or []:
            if isinstance(inst, dict) and inst.get("id") == model_id:
                return m
    return None


def _instances(models: Optional[list], model_id: str) -> list[dict]:
    e = _entry(models, model_id)
    return [i for i in (e or {}).get("loaded_instances") or [] if isinstance(i, dict) and i.get("id")]


def _all_instances(models: Optional[list]) -> list[tuple[str, dict]]:
    """(model key, instance) for every loaded instance of every model."""
    out = []
    for m in models or []:
        if isinstance(m, dict) and m.get("key"):
            out += [(m["key"], i) for i in m.get("loaded_instances") or [] if isinstance(i, dict) and i.get("id")]
    return out


def _free_mb_under(work, sample=None, sleep=None, every: float = 0.25) -> Optional[int]:
    """Free memory while `work()` runs in a thread: the 10th-percentile reading, so a one-off dip from
    another process does not stand in for what the model costs."""
    sample, sleep = sample or _free_mb, sleep or time.sleep
    th = threading.Thread(target=work, daemon=True)
    th.start()
    seen: list[int] = []
    while th.is_alive():
        cur = sample()
        if cur is not None:
            seen.append(int(cur))
        sleep(every)
    cur = sample()
    if cur is not None:
        seen.append(int(cur))
    if not seen:
        return None
    seen.sort()
    return seen[int(0.1 * (len(seen) - 1))]


_VM_STAT_RE = re.compile(r"^(Pages free|Pages speculative|File-backed pages):\s+(\d+)\.", re.M)


def _darwin_free_mb(vm_stat: str) -> Optional[int]:
    """Activity Monitor's spare memory: physical memory minus Memory Used = free + speculative + cached file pages."""
    m = re.search(r"page size of (\d+) bytes", vm_stat)
    rows = dict(_VM_STAT_RE.findall(vm_stat))
    if not m or len(rows) < 3:
        return None
    pages = sum(int(v) for v in rows.values())
    return int(pages * int(m.group(1)) // (1024 * 1024))


def _ram_free_mb(vm, darwin: bool, vm_stat: Optional[str] = None) -> int:
    """macOS: Activity Monitor's spare memory from vm_stat, else memory not wired. Elsewhere: available."""
    if darwin:
        free = _darwin_free_mb(vm_stat) if vm_stat else None
        if free is not None:
            return free
        if getattr(vm, "wired", None) is not None:
            return int((vm.total - vm.wired) // (1024 * 1024))
    return int(vm.available // (1024 * 1024))


def _vm_stat() -> Optional[str]:
    try:
        return subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _free_mb() -> Optional[int]:
    """Free memory after a load: host RAM, min'd with free VRAM where a discrete GPU reports."""
    free: Optional[int] = None
    with best_effort("lms tools: ram available", log=log):
        import psutil  # type: ignore
        import sys as _sys
        darwin = _sys.platform == "darwin"
        free = _ram_free_mb(psutil.virtual_memory(), darwin, _vm_stat() if darwin else None)
    with best_effort("lms tools: vram free", log=log):
        from collectors.gpu import collect_gpu  # type: ignore
        gpu = collect_gpu() or {}
        total, used = gpu.get("vram_total_bytes"), gpu.get("vram_used_bytes")
        if isinstance(total, (int, float)) and total > 0 and isinstance(used, (int, float)):
            vfree = int((total - used) // (1024 * 1024))
            free = vfree if free is None else min(free, vfree)
    return free


def _model_row(m: dict) -> dict:
    insts = [i for i in m.get("loaded_instances") or [] if isinstance(i, dict)]
    return {"key": m.get("key"), "display_name": m.get("display_name"), "type": m.get("type"),
            "params": m.get("params_string"), "size_bytes": m.get("size_bytes"),
            "max_ctx": m.get("max_context_length"), "quant": (m.get("quantization") or {}).get("name"),
            "loaded": bool(insts), "instances": [i.get("id") for i in insts],
            "config": (insts[0].get("config") if insts else None)}


def _bench_server() -> dict:
    """Running-server snapshot in the live bench's shape: downloaded models, loaded id, slots, spec window."""
    base = _url()
    out: dict = {"up": False, "url": base, "provider": "lms", "models": [], "loaded_id": None,
                 "slots_idle": 0, "slots_total": 0, "spec": None, "build": ""}
    if not base:
        return out
    ids: list = []
    try:
        r = _session().get(f"{base}/v1/models", timeout=3)
        if r.ok:
            ids = [m.get("id") for m in ((r.json() or {}).get("data") or []) if isinstance(m, dict) and m.get("id")]
            out["up"] = True
    except (requests.RequestException, ValueError):
        return out
    native = _native_models() or []
    loaded: dict = {}
    for m in native:
        if not isinstance(m, dict) or not m.get("key"):
            continue
        row = _model_row(m)
        if row["loaded"]:
            loaded[row["key"]] = row
    for mid in ids:
        out["models"].append({"id": mid, "status": "loaded" if mid in loaded else "unloaded",
                              "type": (loaded.get(mid) or {}).get("type")})
    for key, row in loaded.items():
        if (row.get("type") or "llm") != "llm":
            continue
        cfg = row.get("config") or {}
        out["loaded_id"] = row["instances"][0] if row["instances"] else key
        out["slots_total"] = int(cfg.get("parallel") or 1)
        out["slots_idle"] = out["slots_total"]
        if cfg.get("speculative_draft_mtp") or cfg.get("speculative_draft_simple"):
            out["spec"] = {"n_min": cfg.get("speculative_draft_min_tokens"),
                           "n_max": cfg.get("speculative_draft_max_tokens"),
                           "p_min": cfg.get("speculative_draft_min_continue_probability")}
        break
    with best_effort("lms tools: ps busy", log=log):
        ps = _lms.lms_get_ps() or []
        busy = sum(1 for r in ps if any(m in (r.get("status") or "") for m in ("PROMPT", "STREAM", "GENERAT", "PREDICT", "QUEUE")))
        out["slots_idle"] = max(0, out["slots_total"] - busy)
    return out


# ── live benchmark ─────────────────────────────────────────────────────

def lms_bench_live_preflight(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _lms._lms_check_enabled()
    cfg = _ctx().config
    return {"ok": True, "provider": "lms", "server": _bench_server(), "runtime": _ll._bench_live_runtime(),
            "datasets": _bl.read_marker(cfg.AGENT_INSTALL_DIR), "benches": list(_bl.BENCHES),
            "busy": bool(_ll._bench_active or _ll._autotune_active)}


def lms_bench_live_setup(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _lms._lms_check_enabled()
    benches = [b for b in (body.get("prefetch") or []) if b in _bl.BENCHES]
    with _ll._bench_lock:
        if _ll._bench_active or _ll._autotune_active:
            return {"ok": False, "error": "Another benchmark or autotune is in progress"}
        _ll._bench_active = True
        with _ll._bench_cond:
            _ll._bench_replay.start_run(uuid.uuid4().hex[:12])
    _ll._bench_cancel_event.clear()
    threading.Thread(target=_ll._bench_live_setup_job, args=(benches,), daemon=True).start()
    return {"ok": True, "run_id": _ll._bench_replay.run_id}


def lms_bench_live_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _lms._lms_check_enabled()
    try:
        req = _bl.validate_run_request(body or {})
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if _ll._bench_active or _ll._autotune_active:
        return {"ok": False, "error": "Another benchmark or autotune is in progress"}
    server = _bench_server()
    if not server["up"]:
        return {"ok": False, "error": "LM Studio server is not running"}
    row = next((m for m in server["models"] if m.get("id") == req["model_id"]), None)
    if row is None:
        return {"ok": False, "error": f"LM Studio does not list {req['model_id']}"}
    rt = _ll._bench_live_runtime()
    if not rt["python"] or not rt["script"]:
        return {"ok": False, "error": "speed-bench runtime not installed", "runtime": rt}
    with _ll._bench_lock:
        if _ll._bench_active or _ll._autotune_active:
            return {"ok": False, "error": "Another benchmark or autotune is in progress"}
        _ll._bench_active = True
        with _ll._bench_cond:
            _ll._bench_replay.start_run(uuid.uuid4().hex[:12])
    _ll._bench_cancel_event.clear()
    if row.get("status") != "loaded":
        _ll._bench_put({"type": "line", "model_id": req["model_id"],
                        "text": f"{req['model_id']} is not loaded; LM Studio loads it on the first request, which counts against the first level"})
    threading.Thread(target=_bench_run_shimmed, args=(req, server, rt["python"], rt["script"]), daemon=True).start()
    return {"ok": True, "run_id": _ll._bench_replay.run_id}


def _bench_run_shimmed(req: dict, server: dict, python: str, script: str) -> None:
    """The live run behind the timings shim: LM Studio reports no prefill/decode timings itself."""
    with _shim.Shim(server["url"]) as sh:
        srv = dict(server, url=sh.url, lms_url=server["url"], timings="shim")
        _ll._bench_put({"type": "line", "model_id": req["model_id"],
                        "text": "[lms] timings: " + ("native /api/v1/chat stats for single-turn requests, streaming marks otherwise"
                                                     if sh.native_available() else "OpenAI streaming marks (no native API)")})
        _ll._bench_live_run_all(req, srv, python, script, provider="lms")


def lms_bench_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    _ctx().check_stream_auth(authorization, token, "/lms/bench/stream")
    _lms._lms_check_enabled()
    return _shared.bench_replay_sse(_ll._bench_replay, _ll._bench_cond, lambda: _ll._bench_active, last_event_id)


def lms_bench_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _lms._lms_check_enabled()
    return _ll._bench_cancel_impl([])


# ── autotune ───────────────────────────────────────────────────────────

class _LmsBackend:
    """Real backend for lms_autotune._Run: native unload/load, effective config, free memory, the speed-bench stick."""

    def __init__(self, model_id: str, run_id: str, bench_url: Optional[str] = None, orig: Optional[dict] = None):
        self.model_id, self.run_id = model_id, run_id
        self.bench_url = bench_url
        self.orig = orig
        self._n = 0

    def drafts(self) -> list:
        return _native_models() or []

    def current(self) -> dict:
        """The model as the run found it: live instance, else the run's snapshot (earlier models unload everything)."""
        models = _native_models()
        e = _entry(models, self.model_id)
        insts = _instances(models, self.model_id)
        cfg = dict((insts[0].get("config") or {}) if insts else (self.orig or {}))
        return {"loaded": bool(insts) or self.orig is not None, "config": cfg,
                "max_ctx": (e or {}).get("max_context_length"), "size_bytes": (e or {}).get("size_bytes") or 0,
                "free_mb": _free_mb(), "missing": e is None}

    def unload_all(self) -> None:
        """Unloads every instance of every model so a candidate load gets the whole memory budget."""
        ids = [i["id"] for _k, i in _all_instances(_native_models())]
        for iid in ids:
            with best_effort("lms autotune: unload instance", log=log):
                _session().post(f"{_url()}/api/v1/models/unload", json={"instance_id": iid},
                                timeout=_lms._cfg_timeout("LMS_UNLOAD_TIMEOUT_S", _lms._LMS_UNLOAD_TIMEOUT_DEFAULT_S))
        deadline = time.monotonic() + UNLOAD_WAIT_S
        while ids and time.monotonic() < deadline:
            if not _all_instances(_native_models()):
                break
            time.sleep(1)
        with best_effort("lms autotune: reconcile", log=log):
            _lms.reconcile_now()

    def _touch(self, instance_id: str) -> tuple[Optional[str], Optional[int]]:
        """A ~1k-token prompt + 32 tokens against the fresh instance; (error text, lowest free MB seen while it ran).
        Weights, KV and compute buffers are only all resident while a request runs (macOS unwires idle Metal buffers)."""
        out: dict = {}

        def work():
            try:
                out["r"] = _session().post(f"{_url()}/v1/chat/completions",
                                           json={"model": instance_id, "messages": [{"role": "user", "content": WARM_PROMPT}],
                                                 "max_tokens": 32, "temperature": 0, "stream": False},
                                           timeout=_lms._cfg_timeout("LMS_LOAD_TIMEOUT_S", _lms._LMS_LOAD_TIMEOUT_DEFAULT_S))
            except requests.RequestException as e:
                out["e"] = f"first request failed: {e}"[:300]
        low = _free_mb_under(work)
        if out.get("e"):
            return out["e"], None
        r = out.get("r")
        if r is None or not r.ok:
            try:
                err = (r.json() or {}).get("error") or {} if r is not None else {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
            except ValueError:
                msg = r.text[:200]
            return (msg or f"HTTP {getattr(r, 'status_code', '?')}")[:300], None
        return None, low

    def _wait_instance(self) -> Optional[dict]:
        deadline = time.monotonic() + INSTANCE_WAIT_S
        while time.monotonic() < deadline:
            if _ll._autotune_cancel_event.is_set():
                return None
            insts = _instances(_native_models(), self.model_id)
            if insts:
                return insts[0]
            time.sleep(1)
        return None

    def load(self, config: dict, measure: Optional[dict]) -> dict:
        t0 = time.monotonic()
        self.unload_all()
        if _ll._autotune_cancel_event.is_set():
            return {"ok": False, "error": "cancelled"}
        payload = {"model": self.model_id}
        payload.update({k: v for k, v in (config or {}).items() if k in _lat.CONFIG_KEYS})
        _ll._autotune_put({"type": "line", "model_id": self.model_id,
                           "text": "[autotune] load " + json.dumps(payload, sort_keys=True)})
        try:
            resp = _session().post(f"{_url()}/api/v1/models/load", json=payload,
                                   timeout=_lms._cfg_timeout("LMS_LOAD_TIMEOUT_S", _lms._LMS_LOAD_TIMEOUT_DEFAULT_S))
        except requests.RequestException as e:
            return {"ok": False, "error": f"load request failed: {e}"[:300]}
        if not resp.ok:
            try:
                err = (resp.json() or {}).get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
            except ValueError:
                msg = resp.text[:200]
            return {"ok": False, "error": (msg or f"HTTP {resp.status_code}")[:300]}
        inst = self._wait_instance()
        with best_effort("lms autotune: reconcile", log=log):
            _lms.reconcile_now()
        if inst is None:
            return {"ok": False, "error": "cancelled" if _ll._autotune_cancel_event.is_set()
                    else "the model did not appear as loaded"}
        # LM Studio lists the instance while it is still loading; the first answer proves the load is complete.
        err, free = self._touch(str(inst.get("id") or self.model_id))
        if err:
            return {"ok": False, "error": err}
        inst = next(iter(_instances(_native_models(), self.model_id)), inst)
        eff = dict(inst.get("config") or {})
        out = {"ok": True, "error": None, "config": eff, "ctx": eff.get("context_length"),
               "free_mb": free if free is not None else _free_mb(), "load_s": round(time.monotonic() - t0, 1),
               "instance_id": inst.get("id"), "stick": None}
        if measure:
            out["stick"] = self._stick(str(inst.get("id") or self.model_id), measure)
            sf = out["stick"].get("free_mb")
            if sf is not None and (out["free_mb"] is None or sf < out["free_mb"]):
                out["free_mb"] = sf
        return out

    def _stick(self, instance_id: str, measure: dict) -> dict:
        """One short speed-bench run against LM Studio: single-turn 1k prompts, fixed osl."""
        cfg = _ctx().config
        rt = _ll._bench_live_runtime()
        if not rt["python"] or not rt["script"]:
            return {"ok": False, "error": "speed-bench runtime not installed"}
        req = {"model_id": instance_id, "bench": _at.STICK_BENCH, "categories": "all",
               "osl": _at.STICK_OSL, "limit": int(measure.get("limit") or _at.STICK_LIMIT),
               "concurrency": [int(measure.get("concurrency") or 1)], "timeout_s": 300,
               "extra_inputs": {"temperature": 0}, "baseline_run_id": None}
        self._n += 1
        out_dir = _bl.bench_dir(cfg.AGENT_INSTALL_DIR) / "runs" / f"at-{self.run_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        token = re.sub(r"[^A-Za-z0-9_.-]", "_", self.model_id)[:60]
        out_path = out_dir / f"stick-{self._n}-{token}.json"
        out_path.unlink(missing_ok=True)
        benv = dict(os.environ, PYTHONUNBUFFERED="1", HF_HUB_DISABLE_PROGRESS_BARS="1")
        level = req["concurrency"][0]
        t0 = time.monotonic()
        got: dict = {}

        def work():
            got["res"] = _bl.run_level_subprocess(
                _bl.build_cmd(rt["python"], rt["script"], self.bench_url or _url(), req, level, str(out_path)),
                benv, _ll._autotune_put, self.model_id, level, _ll._autotune_cancel_event,
                _ll._autotune_track_aux, _ll._autotune_untrack_aux)
        # Free memory is sampled through the traffic: serving costs more than one warm-up request.
        free = _free_mb_under(work, every=1.0)
        rc, cancelled, elapsed = got.get("res") or (None, False, None)
        wall = elapsed if elapsed is not None else (time.monotonic() - t0)
        if cancelled:
            return {"ok": False, "error": "cancelled"}
        if rc is None:
            return {"ok": False, "error": "speed-bench did not start"}
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "error": f"speed-bench produced no output (rc={rc})"}
        s = _bl.level_summary(payload, wall)["all"]
        return {"ok": rc in (0, 1) and bool(s.get("pred_tps")), "decode_tps": s.get("pred_tps"),
                "prefill_tps": s.get("prompt_tps"), "latency_s": s.get("latency_s"), "agg_tps": s.get("agg_pred_tps"),
                "accept": s.get("accept_rate"), "completion_tokens": s.get("completion_tokens"), "free_mb": free,
                "seconds": round(wall, 1), "error": None if rc in (0, 1) else f"speed-bench rc={rc}"}


def _snapshot() -> list[tuple[str, dict]]:
    """(model key, load config) of every instance loaded when the run starts."""
    return [(k, {c: v for c, v in (i.get("config") or {}).items() if c in _lat.CONFIG_KEYS})
            for k, i in _all_instances(_native_models())]


def _restore(snapshot: list, run_id: str, bench_url: Optional[str] = None) -> None:
    """Puts every instance the run found back with its original config; nothing loaded → everything unloaded."""
    _LmsBackend("", run_id, bench_url).unload_all()
    for key, cfg in snapshot:
        _ll._autotune_put({"type": "line", "model_id": key, "text": f"[autotune] restoring {key} with its original load settings"})
        res = _LmsBackend(key, run_id, bench_url).load(cfg, None)
        if not res.get("ok"):
            _ll._autotune_put({"type": "line", "model_id": key, "text": f"[autotune] restore of {key} failed: {res.get('error')}"})


def _autotune_run_all(req: dict) -> None:
    run_id = _ll._autotune_run_id
    _ll._autotune_cancel_event.clear()
    shim = _shim.Shim(_url()).start()
    try:
        rt = _ll._bench_live_runtime()
        env = {"run_id": run_id, "runtime": bool(rt["python"] and rt["script"]), "provider": "lms"}
        snapshot = _snapshot()
        orig_cfg = dict(snapshot)
        try:
            for mid in req["model_ids"]:
                if _ll._autotune_cancel_event.is_set():
                    break
                backend = _LmsBackend(mid, run_id, bench_url=shim.url, orig=orig_cfg.get(mid))
                if backend.current().get("missing"):
                    _ll._autotune_put({"type": "model_done", "model_id": mid, "run_id": run_id, "provider": "lms", "ok": False,
                                       "cancelled": False, "objective": req["objective"], "mode": req.get("mode") or "tune",
                                       "changes": [], "stages": [], "stop_reason": "LM Studio does not list this model",
                                       "elapsed_s": 0, "loads": 0})
                    continue
                done = _lat.run_model(mid, req, backend, _ll._autotune_put, _ll._autotune_cancel_event.is_set, env)
                _shared.post_tool_run(_ctx(), "autotune", "lms", run_id, mid, done["ok"], _lat.ledger_summary(done))
        finally:
            # Restore even after a cancel: the cancel flag only stops measuring, not the reload.
            was_cancel = _ll._autotune_cancel_event.is_set()
            _ll._autotune_cancel_event.clear()
            with best_effort("lms autotune: restore original loads", log=log):
                _restore(snapshot, run_id, shim.url)
            if was_cancel:
                _ll._autotune_cancel_event.set()
        cancelled = _ll._autotune_cancel_event.is_set()
        _ll._autotune_put({"type": "done", "ok": not cancelled, "cancelled": cancelled, "count": len(req["model_ids"])})
    except Exception as e:
        log.error("lms autotune run error: %s", e, exc_info=True)
        _ll._autotune_put({"type": "done", "ok": False, "error": str(e)})
    finally:
        shim.stop()
        with best_effort("lms autotune: drop run scratch dir", log=log):
            import shutil
            shutil.rmtree(_bl.bench_dir(_ctx().config.AGENT_INSTALL_DIR) / "runs" / f"at-{run_id}", ignore_errors=True)
        _ll._autotune_untrack_aux()
        with _ll._autotune_lock:
            _ll._autotune_active = False
            _ll._autotune_quality = False


def lms_autotune_preflight(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """What the tuner can do on this host: server state, downloaded models with their load config, memory."""
    _ctx().check_bearer(authorization); _lms._lms_check_enabled()
    rt = _ll._bench_live_runtime()
    server = _bench_server()
    rows = [_model_row(m) for m in (_native_models() or []) if isinstance(m, dict) and m.get("key")]
    drafts_for: dict = {}
    for r in rows:
        if (r.get("type") or "llm") != "llm":
            continue
        hit = _lat.find_draft([m for m in rows if (m.get("type") or "llm") == "llm"], r["key"], int(r.get("size_bytes") or 0))
        drafts_for[r["key"]] = hit["key"] if hit else None
    return {"ok": True, "provider": "lms", "busy": bool(_ll._bench_active or _ll._autotune_active),
            "autotune_active": bool(_ll._autotune_active), "quality_active": False,
            "unit_active": False, "server": server, "models": rows, "drafts_for": drafts_for,
            "runtime": {"ok": bool(rt["python"] and rt["script"]), **rt},
            "ram_total_mb": _ll._ram_total_mb(), "free_mb": _free_mb(), "vram_total_mb": None}


def lms_autotune_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _lms._lms_check_enabled()
    try:
        req = _lat.validate_request(body or {})
    except (ValueError, TypeError, AttributeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not _bench_server()["up"]:
        return {"ok": False, "error": "LM Studio server is not running — start it before auto-tune"}
    with _ll._autotune_lock:
        if _ll._autotune_active or _ll._bench_active:
            return {"ok": False, "error": "Another benchmark or auto-tune is in progress"}
        _ll._autotune_active = True
        _ll._autotune_quality = False
        _ll._autotune_run_id = uuid.uuid4().hex[:12]
        with _ll._autotune_cond:
            _ll._autotune_replay.start_run(_ll._autotune_run_id)
    threading.Thread(target=_autotune_run_all, args=(req,), daemon=True).start()
    return {"ok": True, "run_id": _ll._autotune_run_id}


def lms_autotune_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    _ctx().check_stream_auth(authorization, token, "/lms/autotune/stream")
    _lms._lms_check_enabled()
    return _shared.bench_replay_sse(_ll._autotune_replay, _ll._autotune_cond, lambda: _ll._autotune_active, last_event_id)


def lms_autotune_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _ctx().check_bearer(authorization); _lms._lms_check_enabled()
    return _ll._autotune_cancel_impl([])


def lms_tools_state(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Whether a bench/autotune job is running, for the manager Tools view."""
    _ctx().check_bearer(authorization)
    return {"ok": True, "bench_active": bool(_ll._bench_active), "autotune_active": bool(_ll._autotune_active),
            "quality_active": False}


_ROUTES: tuple = (
    ("GET",  "/lms/bench/live/preflight", lms_bench_live_preflight),
    ("POST", "/lms/bench/live/setup",     lms_bench_live_setup),
    ("POST", "/lms/bench/live/run",       lms_bench_live_run),
    ("GET",  "/lms/bench/stream",         lms_bench_stream),
    ("POST", "/lms/bench/cancel",         lms_bench_cancel),
    ("GET",  "/lms/autotune/preflight",   lms_autotune_preflight),
    ("POST", "/lms/autotune/run",         lms_autotune_run),
    ("GET",  "/lms/autotune/stream",      lms_autotune_stream),
    ("POST", "/lms/autotune/cancel",      lms_autotune_cancel),
    ("GET",  "/lms/tools/state",          lms_tools_state),
)


def set_context(ctx) -> None:
    """State lives in lms.py and llama.py; kept for providers.configure_all symmetry."""
    return None


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])
