"""llama.cpp provider — 32 routes + collector + perf-controller background."""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import math
import os
import pwd
import queue as _queue_lib
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

import requests
from fastapi import Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
import stream_pool  # type: ignore[import-not-found]  # sibling at agent root
from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling at agent root
from _bench_replay import BenchReplayBuffer  # type: ignore[import-not-found]  # sibling at agent root

from collectors.gpu import collect_gpu  # type: ignore
from . import _shared
from . import llama_install
from . import llama_sse
from . import llama_upgrade
from . import llama_bench_live as _bl
from . import llama_autotune as _at

# PR2: minimal spec the agent's heartbeat body emits so the manager can
# discover what this agent serves. Manager-side providers/llama.py owns the
# fleet aggregator + UI metadata; this is the agent-side counterpart.
PROVIDER_SPEC = {
    "name": "llama",
    "capability_key": "llama",
    "push_endpoint": "/api/remote/provider-state",
}

log = logging.getLogger("llm-systems-agent.providers.llama")

_ctx = None


def set_context(ctx) -> None:
    global _ctx
    _ctx = ctx


def _require_ctx():
    if _ctx is None:
        raise RuntimeError("providers.llama.set_context() not called")
    return _ctx


# ── Module state ───────────────────────────────────────────────────────

_llama_api_probe_cache: dict[str, Any] = {"ts": 0.0, "result": "unknown"}
_LLAMA_METRICS_IDLE_THRESHOLD_S = 60
_LLAMA_FAIL_THRESHOLD = 10
# Cap on the /props chat_template text carried in each heartbeat sample.
_LLAMA_CHAT_TEMPLATE_MAX_CHARS = 2000

_llama_info_cache: dict[str, Any] = {}
_llama_info_last_poll: float = 0.0
_llama_info_last_active_ts: float = 0.0
_llama_info_last_tokens_total: "int | None" = None
_llama_info_last_loaded_model: "str | None" = None
_llama_info_conn_fail_count: int = 0
_llama_info_idle_logged: bool = False
_llama_build_last: str = ""

# /models/sse listener (router mode); authoritative for llama_state when connected.
_llama_sse_listener: "Optional[llama_sse.LlamaSseListener]" = None
_llama_sse_thread: "Optional[threading.Thread]" = None
_llama_sse_session: "Optional[requests.Session]" = None

_HF_REPO_RE = re.compile(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$')
_SVCCONFIG_WRAPPER = "/usr/local/sbin/llm-svcconfig-apply"
_LLAMA_LOG_IGNORE = (
    "GET /v1/models",
    "GET /metrics",
    "update_slots: all slots are idle",
)

# Download-console PTY geometry and the emit floor for progress frames.
_DL_PTY_ROWS, _DL_PTY_COLS = 32, 140
_DL_FRAME_INTERVAL_S = 1.0
# Progress-bar shaped line: "NN%|", tqdm block glyphs, or "| <size>B" — multi-
# bar redraws arrive \n-separated (cursor-up codes stripped), not just \r.
_DL_FRAME_RE = re.compile(r"\d+%\||[▏▎▍▌▋▊▉█]|\|\s*\d+(?:\.\d+)?\s*[KMGT]?i?B\b")

_dl_queue: "_queue_lib.Queue[dict[str, Any]]" = _queue_lib.Queue(maxsize=2000)
_dl_lock = threading.Lock()
_dl_active = False
_dl_proc: "Optional[subprocess.Popen]" = None
_dl_cancelled = False

_log_state = _shared.LogStream()

_LLAMA_VALUE_FLAGS = {
    "--threads", "--timeout", "--log-file", "--sleep-idle-seconds",
    "--host", "--port", "--parallel", "--models-max",
    "--models-preset", "--model",
    "--api-key", "--keep", "--ctx-size", "--batch-size",
    "--gpu-layers", "--tensor-parallel", "-t", "-c", "-ngl",
    "--mmap", "--no-mmap", "--load-mode", "-lm", "--log-disable", "--cont-batch",
    "--embedding", "--no-display", "--simple-io",
    "--chat-template", "--cors-origins", "--tools",
}

_build_queue: "_queue_lib.Queue[dict[str, Any]]" = _queue_lib.Queue(maxsize=4000)
_build_lock = threading.Lock()
_build_running = False

_bench_replay = BenchReplayBuffer(maxlen=5000)
_bench_cond = threading.Condition()
_bench_lock = threading.Lock()
_bench_active = False
_bench_proc: "Optional[subprocess.Popen]" = None
_bench_pgid: "Optional[int]" = None
_bench_cancel_event = threading.Event()
_BENCH_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_autotune_replay = BenchReplayBuffer(maxlen=5000)
_autotune_cond = threading.Condition()
_autotune_lock = threading.Lock()
_autotune_active = False
_autotune_run_id = ""
_autotune_proc: "Optional[subprocess.Popen]" = None
_autotune_pgid: "Optional[int]" = None
_autotune_aux_proc: "Optional[subprocess.Popen]" = None
_autotune_aux_pgid: "Optional[int]" = None
_autotune_cancel_event = threading.Event()

def shutdown_children() -> None:
    """SIGTERM (then SIGKILL) any tracked bench/autotune process group so an
    agent restart doesn't orphan children holding VRAM or the server port."""
    _bench_cancel_event.set()
    _autotune_cancel_event.set()
    for what, proc, pgid in (("bench", _bench_proc, _bench_pgid),
                             ("autotune", _autotune_proc, _autotune_pgid),
                             ("autotune-aux", _autotune_aux_proc, _autotune_aux_pgid)):
        if proc is None or proc.poll() is not None:
            continue
        try:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    continue
            else:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    log.warning("shutdown: %s process %s survived SIGKILL", what, proc.pid)
                    continue
            log.info("shutdown: terminated %s process group (pid %s)", what, proc.pid)
        except Exception as e:
            log.warning("shutdown: %s terminate failed: %s", what, e)


_AT_NCTX_RE = re.compile(r"n_ctx_seq\s*\(\s*(\d+)\s*\)")
_AT_NCTX_FALLBACK_RE = re.compile(r"\bn_ctx\b\s*=\s*(\d+)")
_AT_MEM_RE = re.compile(
    r"common_memory_breakdown_print:\s*\|\s*-\s*(?P<label>[^|]*?)\|\s*(?P<total>\d+)\s*=\s*(?P<free>\d+)\s*\+"
)
_AT_GPU_HINT_RE = re.compile(r"(?i)vulkan|rocm|cuda|hip|metal")
_AT_MODEL_LOADED_RE = re.compile(r"(?:^|\s)(?:\w+\s*:\s*)?model loaded\b", re.IGNORECASE)
# The alloc alternative allows only a size/unit run between "alloc…" and "failed".
_AT_OOM_RE = re.compile(
    r"out of memory|failed to allocate|cudaMalloc failed|OutOfDeviceMemory|not enough (?:memory|space)"
    r"|alloc\w*\s+(?:\d[\w.]*\s+(?:\w+\s+){0,4})?failed", re.IGNORECASE)
_AT_MEM_TOTAL_SANE_MAX = 200_000


# ── Moved helpers + routes (verbatim from llm-systems-agent.py with ctx-routing) ──

def llama_read_state_file() -> str:
    try:
        with open(_require_ctx().config.LLAMA_STATE_FILE) as f:
            v = f.read().strip().lower()
            return v if v in ("awake", "sleeping") else "unknown"
    except FileNotFoundError:
        return "unknown"
    except Exception as e:
        log.debug("read llama state file failed: %s", e)
        return "unknown"


def _llama_port_open() -> bool:
    """Bare TCP connect — invisible to llama-server's sleep idle timer."""
    try:
        from urllib.parse import urlparse
        p = urlparse(_require_ctx().config.LLAMA_API_URL)
        host = p.hostname or "127.0.0.1"
        port = p.port or 8080
        import socket as _s
        with _s.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def llama_get_state() -> str:
    """Best-effort state: SSE (when authoritative), state-file+TCP-probe, then /v1/models, else unknown.

    State file alone lies after `systemctl stop` (perf controller doesn't
    clear it), so verify with a non-disturbing TCP connect.
    """
    if _llama_sse_authoritative():
        sse_state = _require_ctx().state.get("llama_state")
        if sse_state in ("awake", "sleeping"):
            return sse_state
    file_state = llama_read_state_file()
    if file_state in ("awake", "sleeping"):
        if _llama_port_open():
            return file_state
        return "unknown"

    now = time.time()
    if now - _llama_api_probe_cache["ts"] < 5.0:
        return _llama_api_probe_cache["result"]

    ok, _msg = _require_ctx().probe_http(f"{_require_ctx().config.LLAMA_API_URL.rstrip('/')}/v1/models", timeout=1.5)
    result = "awake" if ok else "unknown"
    _llama_api_probe_cache["ts"] = now
    _llama_api_probe_cache["result"] = result
    return result


def _llama_metric_val(line: str) -> "float | None":
    # Prometheus emits +Inf/NaN for some rates; treat non-finite as no value.
    try:
        v = float(line.split()[-1])
        return v if math.isfinite(v) else None
    except Exception:
        return None


def llama_api_port(url: "str | None") -> "int | None":
    """TCP port of a llama API base URL, or None when unparseable."""
    from urllib.parse import urlsplit
    try:
        return urlsplit(str(url or "")).port
    except ValueError:
        return None


def collect_llama_for_metrics() -> dict[str, Any]:
    """Agent-side rich llama snapshot; skips /metrics + /slots when sleeping or token-idle."""
    if not _require_ctx().config.LLAMA_ENABLED:
        return {}

    global _llama_info_cache, _llama_info_last_poll
    global _llama_info_last_active_ts, _llama_info_last_tokens_total
    global _llama_info_last_loaded_model, _llama_info_conn_fail_count
    global _llama_info_idle_logged, _llama_build_last

    now = time.time()
    interval = max(2.0, _require_ctx().config.POLL_INTERVAL_S)

    if now - _llama_info_last_poll < interval:
        return dict(_llama_info_cache) if _llama_info_cache else {}
    _llama_info_last_poll = now

    state = llama_get_state()
    api_base = _require_ctx().config.LLAMA_API_URL.rstrip("/")

    llama: dict[str, Any] = {
        "state": state,
        "port": llama_api_port(api_base),
        "model": None,
        "sleeping": False,
        "tokens_per_second": None,
        "prompt_tokens_per_second": None,
        "total_tokens_generated": None,
        "total_tokens_prompted": None,
        "requests_processing": None,
        "requests_deferred": None,
        "active_slots": None,
        "n_decode_total": None,
        "n_busy_slots_per_decode": None,
        "n_tokens_max": None,
        "kv_cache_usage_ratio": None,
        "kv_cache_tokens": None,
        "n_remain": None,
        "sse_status": None,
        "sse_connected": False,
        "download_progress": None,
        "chat_template": None,
        "chat_template_len": None,
        "modalities": None,
        "total_slots": None,
        "is_sleeping": None,
        "n_ctx": None,
    }

    sse_snap = dict(_require_ctx().state.get("llama_sse") or {})
    if sse_snap:
        llama["sse_status"] = sse_snap.get("status")
        llama["download_progress"] = sse_snap.get("download_progress")
    llama["sse_connected"] = _llama_sse_authoritative()

    loaded_id: "str | None" = None
    model_api_sleeping = False

    # /v1/models is safe in all states; doesn't reset llama-server's sleep timer.
    try:
        resp = requests.get(f"{api_base}/v1/models", timeout=2)
        if resp.ok:
            _llama_info_conn_fail_count = 0
            models = (resp.json() or {}).get("data", []) or []
            for m in models:
                st = m.get("status", {})
                sv = st.get("value") if isinstance(st, dict) else None
                if sv in ("loaded", "sleeping"):
                    loaded_id = m.get("id")
                    model_api_sleeping = (sv == "sleeping")
                    llama["model"] = loaded_id
                    if _llama_info_last_loaded_model != loaded_id:
                        log.info("llama model: %s → %s%s",
                                    _llama_info_last_loaded_model or "(none)",
                                    loaded_id,
                                    " (sleeping)" if model_api_sleeping else "")
                    _llama_info_last_loaded_model = loaded_id
                    break
            if not loaded_id:
                if _llama_info_last_loaded_model is not None:
                    log.info("llama model unloaded: %s", _llama_info_last_loaded_model)
                    _llama_info_last_tokens_total = None
                    _llama_info_last_active_ts = now
                _llama_info_last_loaded_model = None
                if models:
                    llama["model"] = (models[0].get("id") or "") + " (unloaded)"
        else:
            _llama_info_conn_fail_count += 1
    except Exception as e:
        _llama_info_conn_fail_count += 1
        if _llama_info_conn_fail_count == 1:
            log.warning("llama /v1/models unreachable: %s", e)
        if _llama_info_conn_fail_count >= _LLAMA_FAIL_THRESHOLD:
            log.warning(
                "llama /v1/models unreachable for %s cycles — marking server down",
                _LLAMA_FAIL_THRESHOLD,
            )
            _llama_info_last_loaded_model = None
            _llama_info_conn_fail_count = 0
            llama["state"] = "unknown"
        _llama_info_cache = llama
        return llama

    # /props is idle-timer-exempt; router mode returns the per-model fields
    # only with ?model=, so it's skipped entirely when nothing is loaded.
    if loaded_id:
        try:
            presp = requests.get(
                f"{api_base}/props",
                timeout=2,
                headers={"Authorization": "Bearer no-key"},
                params={"model": loaded_id},
            )
            if presp.ok:
                props = presp.json() or {}
                tmpl = props.get("chat_template")
                if isinstance(tmpl, str) and tmpl:
                    llama["chat_template"] = tmpl[:_LLAMA_CHAT_TEMPLATE_MAX_CHARS]
                    llama["chat_template_len"] = len(tmpl)
                mods = props.get("modalities")
                if isinstance(mods, dict):
                    llama["modalities"] = {k: bool(v) for k, v in mods.items()}
                n_slots = props.get("total_slots")
                if isinstance(n_slots, int):
                    llama["total_slots"] = n_slots
                dgs = props.get("default_generation_settings") or {}
                n_ctx = dgs.get("n_ctx")
                if isinstance(n_ctx, (int, float)):
                    llama["n_ctx"] = int(n_ctx)
                if isinstance(props.get("is_sleeping"), bool):
                    llama["is_sleeping"] = props["is_sleeping"]
                    # Direct API sleep signal corroborates the state-file value.
                    if props["is_sleeping"]:
                        llama["sleeping"] = True
                bi = props.get("build_info")
                if isinstance(bi, str) and bi.strip():
                    _llama_build_last = bi.strip()[:64]
        except Exception as e:
            log.debug("llama /props: %s", e)

    if _llama_build_last:
        llama["build"] = _llama_build_last

    # /metrics + /slots reset llama-server's sleep timer; skip while sleeping.
    if state == "sleeping":
        llama["sleeping"] = True
        if llama["model"] is None and _llama_info_last_loaded_model:
            llama["model"] = f"{_llama_info_last_loaded_model} (sleeping)"
        # Emit 0 for rates so charts stay continuous; leave cumulative counters None.
        for k in (
            "tokens_per_second", "prompt_tokens_per_second",
            "requests_processing", "requests_deferred", "active_slots",
            "n_busy_slots_per_decode", "kv_cache_usage_ratio",
            "kv_cache_tokens", "n_remain",
        ):
            if llama.get(k) is None:
                llama[k] = 0
        _llama_info_cache = llama
        return llama

    if _llama_info_cache.get("sleeping"):
        _llama_info_last_active_ts = now

    if loaded_id and not model_api_sleeping:
        idle_secs = now - _llama_info_last_active_ts
        if idle_secs < _LLAMA_METRICS_IDLE_THRESHOLD_S:
            try:
                resp = requests.get(
                    f"{api_base}/metrics",
                    timeout=2,
                    headers={"Authorization": "Bearer no-key"},
                    params={"model": loaded_id},
                )
                if resp.ok:
                    if _llama_info_idle_logged:
                        log.info("llama /metrics polling resumed — token activity detected")
                        _llama_info_idle_logged = False
                    for line in resp.text.splitlines():
                        if line.startswith("#"):
                            continue
                        if line.startswith("llamacpp:predicted_tokens_seconds"):
                            llama["tokens_per_second"] = _llama_metric_val(line)
                        elif line.startswith("llamacpp:prompt_tokens_seconds"):
                            llama["prompt_tokens_per_second"] = _llama_metric_val(line)
                        elif line.startswith("llamacpp:tokens_predicted_total"):
                            v = _llama_metric_val(line)
                            new_total = int(v) if v is not None else None
                            llama["total_tokens_generated"] = new_total
                            if new_total is not None:
                                if (_llama_info_last_tokens_total is None
                                        or new_total > _llama_info_last_tokens_total):
                                    _llama_info_last_active_ts = now
                                    _llama_info_last_tokens_total = new_total
                                elif _llama_info_last_tokens_total - new_total > 1000:
                                    log.info(
                                        "llama.cpp token counter reset detected — resuming /metrics"
                                    )
                                    _llama_info_last_tokens_total = new_total
                                    _llama_info_last_active_ts = now
                                else:
                                    _llama_info_last_tokens_total = new_total
                        elif line.startswith("llamacpp:prompt_tokens_total"):
                            v = _llama_metric_val(line)
                            llama["total_tokens_prompted"] = int(v) if v is not None else None
                        elif line.startswith("llamacpp:requests_processing "):
                            v = _llama_metric_val(line)
                            llama["requests_processing"] = int(v) if v is not None else None
                            llama["active_slots"] = int(v) if v is not None else None
                            if v and int(v) > 0:
                                _llama_info_last_active_ts = now
                        elif line.startswith("llamacpp:requests_deferred"):
                            v = _llama_metric_val(line)
                            llama["requests_deferred"] = int(v) if v is not None else None
                        elif line.startswith("llamacpp:n_decode_total"):
                            v = _llama_metric_val(line)
                            llama["n_decode_total"] = int(v) if v is not None else None
                        elif line.startswith("llamacpp:n_busy_slots_per_decode"):
                            llama["n_busy_slots_per_decode"] = _llama_metric_val(line)
                        elif line.startswith("llamacpp:n_tokens_max"):
                            v = _llama_metric_val(line)
                            llama["n_tokens_max"] = int(v) if v is not None else None
                        elif line.startswith("llamacpp:kv_cache_usage_ratio"):
                            llama["kv_cache_usage_ratio"] = _llama_metric_val(line)
                        elif line.startswith("llamacpp:kv_cache_tokens"):
                            v = _llama_metric_val(line)
                            llama["kv_cache_tokens"] = int(v) if v is not None else None
            except Exception as e:
                log.debug("llama /metrics: %s", e)
        else:
            if _llama_info_cache:
                for k in _llama_info_cache:
                    if llama.get(k) is None:
                        llama[k] = _llama_info_cache.get(k)
            if not _llama_info_idle_logged:
                log.info(
                    "llama /metrics polling paused — no token activity for %.0fs (threshold %ds)",
                    idle_secs, _LLAMA_METRICS_IDLE_THRESHOLD_S,
                )
                _llama_info_idle_logged = True
            # Busy GPU implies inference; resume /metrics polling.
            gpu_util = (collect_gpu() or {}).get("gpu_util_percent")
            if gpu_util is not None and gpu_util > 10:
                log.info(
                    "GPU %.0f%% during llama idle — resuming /metrics polling",
                    gpu_util,
                )
                _llama_info_last_active_ts = now
                _llama_info_idle_logged = False

    if (loaded_id and not model_api_sleeping
            and (now - _llama_info_last_active_ts) < _LLAMA_METRICS_IDLE_THRESHOLD_S):
        try:
            slots_resp = requests.get(
                f"{api_base}/slots",
                timeout=2,
                headers={"Authorization": "Bearer no-key"},
                params={"model": loaded_id},
            )
            if slots_resp.ok:
                slots_data = slots_resp.json() or []
                if slots_data:
                    n_ctx_slot = slots_data[0].get("n_ctx")
                    if n_ctx_slot and llama.get("n_tokens_max") is not None:
                        if llama["kv_cache_tokens"] is None:
                            llama["kv_cache_tokens"] = llama["n_tokens_max"]
                        if llama["kv_cache_usage_ratio"] is None and n_ctx_slot:
                            llama["kv_cache_usage_ratio"] = llama["kv_cache_tokens"] / n_ctx_slot
                    total_remain: "int | None" = None
                    for s in slots_data:
                        nt = s.get("next_token")
                        if isinstance(nt, list) and nt:
                            r = nt[0].get("n_remain")
                        elif isinstance(nt, dict):
                            r = nt.get("n_remain")
                        else:
                            r = None
                        if r is not None:
                            total_remain = (total_remain or 0) + r
                    llama["n_remain"] = total_remain
        except Exception as e:
            log.debug("llama /slots: %s", e)

    llama["build_method"] = getattr(_require_ctx().config, "LLAMA_BUILD_METHOD", "") or "custom_script"
    _llama_info_cache = llama
    return llama


def llama_write_state_file(state: str) -> None:
    """Atomic write so concurrent readers never see a partial file."""
    target = _require_ctx().config.LLAMA_STATE_FILE
    tmp = f"{target}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            f.write(state + "\n")
        os.replace(tmp, target)
    except PermissionError as e:
        # Likely legacy: root-owned file from the old bash daemon + /tmp sticky bit.
        try:
            stat = os.stat(target) if os.path.exists(target) else None
        except Exception:
            stat = None
        owner = ""
        if stat is not None:
            try:
                owner = pwd.getpwuid(stat.st_uid).pw_name
            except KeyError:
                owner = f"uid={stat.st_uid}"
        log.warning(
            "write llama state file failed: %s (target=%s owner=%s; "
            "probable bash-daemon legacy file). Recover with: "
            "sudo chown %s %s",
            e, target, owner or "<unknown>",
            _require_ctx().config.AGENT_USER or "<agent_user>", target,
        )
        with best_effort("state file: unlink temp", log=log):
            os.unlink(tmp)
    except Exception as e:
        log.warning("write llama state file failed: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Performance controller (replaces the bash daemon)
# ---------------------------------------------------------------------------

async def perf_controller_loop() -> None:
    """Tail LLAMA_LOG_FILE and switch CPU/fan profiles on sleep/wake markers."""
    if not _require_ctx().config.PERF_CONTROLLER_ENABLED:
        log.info("perf controller disabled by config")
        return
    if _require_ctx().config.AGENT_OS != "linux":
        log.warning("perf controller only supported on Linux; ignoring")
        return

    log_file = _require_ctx().config.LLAMA_LOG_FILE
    log.info("perf controller starting; tailing %s", log_file)

    # Pre-flight: if the state file exists but isn't owned by us, every
    # subsequent transition will fail with EPERM at os.replace() time
    # (sticky bit on /tmp blocks renames over a file you don't own).
    # Detect now and log a single loud actionable line, instead of
    # silently spamming a warning per transition.
    sf = _require_ctx().config.LLAMA_STATE_FILE
    if os.path.exists(sf):
        try:
            sf_uid = os.stat(sf).st_uid
            if sf_uid != os.geteuid():
                try:
                    sf_owner = pwd.getpwuid(sf_uid).pw_name
                except KeyError:
                    sf_owner = f"uid={sf_uid}"
                log.error(
                    "STATE FILE NOT WRITABLE: %s is owned by '%s' but agent runs as "
                    "'%s'. Transitions will fail until you run: sudo chown %s %s "
                    "(usually a leftover from the legacy bash perf-controller daemon)",
                    sf, sf_owner, _require_ctx().config.AGENT_USER or "<agent_user>",
                    _require_ctx().config.AGENT_USER or "<agent_user>", sf,
                )
        except OSError as e:
            log.warning("could not stat %s: %s", sf, e)

    if not os.path.exists(_require_ctx().config.LLAMA_STATE_FILE):
        llama_write_state_file("sleeping")
        log.info("initialized %s = sleeping", _require_ctx().config.LLAMA_STATE_FILE)

    backoff = 1.0
    while not _require_ctx().state.get("restart_pending"):
        if not os.path.exists(log_file):
            log.warning("llama log file not found: %s; retry in %.0fs", log_file, backoff)
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)
            continue
        backoff = 1.0
        proc = await asyncio.create_subprocess_exec(
            "tail", "-F", "-n", "0", log_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        try:
            while not _require_ctx().state.get("restart_pending"):
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")
                await _perf_process_line(line)
        finally:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                with best_effort("perf: kill controller proc", log=log):
                    proc.kill()


async def _perf_process_line(line: str) -> None:
    cur = llama_read_state_file()
    matched_sleep = next((m for m in _require_ctx().config.PERF_SLEEP_MARKERS if m in line), None)
    matched_wake = next((m for m in _require_ctx().config.PERF_WAKE_MARKERS if m in line), None)

    # When True, SSE owns llama_state; skip the state set + manager push below.
    sse_auth = _llama_sse_authoritative()

    if matched_sleep and cur != "sleeping":
        log.info("perf transition wake->sleep (matched %r); switching to %s",
                    matched_sleep, _require_ctx().config.PERF_TARGET_SLEEP)
        await _perf_switch(_require_ctx().config.PERF_TARGET_SLEEP)
        llama_write_state_file("sleeping")
        with _require_ctx().runtime_lock:
            if not sse_auth:
                _require_ctx().state["llama_state"] = "sleeping"
            _require_ctx().state["perf_sleep_count"] += 1
            _require_ctx().state["perf_last_transition"] = {"to": "sleeping", "ts": _require_ctx().now_iso(), "marker": matched_sleep}
        if not sse_auth:
            _push_llama_state_to_manager("sleeping")
        return

    if matched_wake and cur != "awake":
        log.info("perf transition sleep->wake (matched %r); switching to %s",
                    matched_wake, _require_ctx().config.PERF_TARGET_AWAKE)
        await _perf_switch(_require_ctx().config.PERF_TARGET_AWAKE)
        llama_write_state_file("awake")
        with _require_ctx().runtime_lock:
            if not sse_auth:
                _require_ctx().state["llama_state"] = "awake"
            _require_ctx().state["perf_wake_count"] += 1
            _require_ctx().state["perf_last_transition"] = {"to": "awake", "ts": _require_ctx().now_iso(), "marker": matched_wake}
        if not sse_auth:
            _push_llama_state_to_manager("awake")


def _push_llama_state_to_manager(state: str) -> None:
    """Fire-and-forget llama-state push so the dashboard flips before the next heartbeat."""
    with _require_ctx().runtime_lock:
        tok = _require_ctx().state.get("token")
        aid = _require_ctx().state.get("agent_id")
    if not (tok and aid):
        return
    try:
        url = f"{_require_ctx().config.MANAGER_URL.rstrip('/')}/api/agents/{aid}/llama-state"
        r = _require_ctx().post_session.post(
            url,
            json={"state": state},
            headers={"Authorization": f"Bearer {tok}"},
            timeout=5,
        )
        if r.ok:
            log.info("pushed llama-state=%s to manager (applied=%s)",
                        state, (r.json() or {}).get("applied"))
        else:
            log.debug("manager rejected llama-state push: %s %s",
                         r.status_code, r.text[:160])
    except Exception as e:
        log.debug("llama-state push failed (heartbeat will backstop): %s", e)


async def _perf_switch(target_unit: str) -> None:
    cmd = ["sudo", "-n", "/usr/bin/systemctl", "reload-or-restart", target_unit]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            log.warning("perf switch %s failed (rc=%s): %s",
                           target_unit, proc.returncode,
                           (err or b"").decode("utf-8", errors="replace").strip())
    except Exception as e:
        log.warning("perf switch %s exception: %s", target_unit, e, exc_info=True)


# ---------------------------------------------------------------------------
# /models/sse push-state consumer (router mode).
# ---------------------------------------------------------------------------

def _llama_router_mode() -> bool:
    """True when the llama systemd unit launches in multi-model router mode."""
    try:
        content = Path(_llama_svc_file_path()).read_text()
    except FileNotFoundError:
        log.debug("router-mode detect: service file absent; SSE off")
        return False
    except OSError as e:
        log.warning("router-mode detect: cannot read service file (%s); SSE off", e)
        return False
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("ExecStart="):
            flags = llama_sse.parse_exec_flags(s[len("ExecStart="):].strip())
            return llama_sse.router_mode_from_flags(flags)
    return False


def _llama_sse_authoritative() -> bool:
    """True when the SSE listener is connected and owns llama_state reporting."""
    lis = _llama_sse_listener
    return lis is not None and lis.connected


def _llama_sse_update_snapshot(**fields: Any) -> None:
    ctx = _require_ctx()
    with ctx.runtime_lock:
        snap = dict(ctx.state.get("llama_sse") or {})
        snap.update(fields)
        snap["connected"] = True
        snap["ts"] = ctx.now_iso()
        ctx.state["llama_sse"] = snap


def _llama_sse_apply_status(data: dict[str, Any]) -> None:
    status = llama_sse.status_value(data)
    model_id = llama_sse.model_id(data)
    new_state = llama_sse.sse_status_to_state(status)
    ctx = _require_ctx()
    changed = False
    with ctx.runtime_lock:
        snap = dict(ctx.state.get("llama_sse") or {})
        snap.update({
            "status": (status or "").lower() or None,
            "model": model_id, "connected": True, "ts": ctx.now_iso(),
        })
        ctx.state["llama_sse"] = snap
        if new_state in ("awake", "sleeping") and ctx.state.get("llama_state") != new_state:
            ctx.state["llama_state"] = new_state
            changed = True
    if changed:
        log.info("llama /models/sse: status=%s -> llama_state=%s (model=%s)",
                    status, new_state, model_id)
        _push_llama_state_to_manager(new_state)


def _llama_sse_on_event(sse_event: str, data: dict[str, Any]) -> None:
    kind = llama_sse.event_kind(sse_event, data)
    if kind in ("status_change", "model_status"):
        _llama_sse_apply_status(data)
    elif kind == "download_progress":
        _llama_sse_update_snapshot(download_progress=llama_sse.progress_value(data))
    elif kind in ("download_finished", "download_failed"):
        _llama_sse_update_snapshot(download_progress=None)


def _llama_sse_on_disconnect() -> None:
    """Drop SSE-owned llama_state so the heartbeat re-derives via fallback."""
    ctx = _require_ctx()
    with ctx.runtime_lock:
        ctx.state["llama_state"] = "unknown"
        snap = dict(ctx.state.get("llama_sse") or {})
        if snap:
            snap["connected"] = False
            ctx.state["llama_sse"] = snap


def _maybe_start_sse_listener() -> None:
    """Start the router-mode /models/sse listener thread when enabled."""
    global _llama_sse_listener, _llama_sse_thread, _llama_sse_session
    ctx = _require_ctx()
    if not ctx.config.LLAMA_ENABLED:
        return
    mode = (getattr(ctx.config, "LLAMA_SSE_ENABLED", "auto") or "auto").lower()
    if mode == "off":
        log.info("llama /models/sse listener disabled by config")
        return
    if mode != "on" and not _llama_router_mode():
        log.info("llama /models/sse: single-model mode; SSE listener not started")
        return
    url = f"{ctx.config.LLAMA_API_URL.rstrip('/')}/models/sse"
    _llama_sse_session = requests.Session()
    sess = _llama_sse_session
    _llama_sse_listener = llama_sse.LlamaSseListener(
        connect=lambda: llama_sse.requests_sse_lines(url, session=sess),
        on_event=_llama_sse_on_event,
        should_stop=lambda: bool(ctx.state.get("restart_pending")),
        on_disconnect=_llama_sse_on_disconnect,
        logger=log,
    )
    _llama_sse_thread = threading.Thread(
        target=_llama_sse_listener.run, name="llama-models-sse", daemon=True)
    _llama_sse_thread.start()
    log.info("llama /models/sse listener started (router mode): %s", url)


def llama_state_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    if not _require_ctx().config.LLAMA_ENABLED:
        raise HTTPException(status_code=503, detail="llama not enabled on this agent")
    return {
        "state": llama_get_state(),
        "port": llama_api_port(_require_ctx().config.LLAMA_API_URL),
        "perf_controller_enabled": _require_ctx().config.PERF_CONTROLLER_ENABLED,
        "last_transition": _require_ctx().state.get("perf_last_transition"),
        "sse_connected": _llama_sse_authoritative(),
        "sse": _require_ctx().state.get("llama_sse"),
    }


def _llama_check_enabled() -> None:
    """Raise 503 when LLAMA isn't enabled."""
    if not _require_ctx().config.LLAMA_ENABLED:
        raise HTTPException(status_code=503, detail="llama not enabled on this agent")


def _llama_log_should_keep(line: str) -> bool:
    return not any(p in line for p in _LLAMA_LOG_IGNORE)


def _hf_repo_valid(repo: str) -> bool:
    return bool(_HF_REPO_RE.match(repo or ""))


def _hf_cache_root() -> Path:
    """~/.cache/huggingface/hub for AGENT_USER (not euid); falls back to Path.home()."""
    if _require_ctx().config.AGENT_USER:
        try:
            home = pwd.getpwnam(_require_ctx().config.AGENT_USER).pw_dir
            return Path(home) / ".cache" / "huggingface" / "hub"
        except KeyError:
            log.debug("HF cache: AGENT_USER %r not in passwd db; using current home", _require_ctx().config.AGENT_USER)
    return Path.home() / ".cache" / "huggingface" / "hub"


def _list_cache_ggufs(root: Path) -> list[dict]:
    """Every .gguf under <root>/models--*/snapshots/*, mmproj projectors excluded."""
    rows: list[dict] = []
    if not root.is_dir():
        return rows
    for repo_dir in root.glob("models--*"):
        repo = repo_dir.name[len("models--"):].replace("--", "/", 1)
        snaps = repo_dir / "snapshots"
        if not snaps.is_dir():
            continue
        for snap in snaps.iterdir():
            if not snap.is_dir():
                continue
            for p in snap.iterdir():
                if p.suffix != ".gguf" or p.name.lower().startswith("mmproj-"):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                rows.append({"repo": repo, "file": p.name, "path": str(p), "size": size})
    rows.sort(key=lambda r: (r["repo"], r["file"]))
    return rows


def _llama_read_ini() -> configparser.ConfigParser:
    cp = configparser.ConfigParser(default_section="__DEFAULTS__", interpolation=None)
    cp.optionxform = str
    cp.read(_require_ctx().config.LLAMA_CONFIG_INI)
    return cp


def _llama_ini_to_dict(cp: configparser.ConfigParser) -> dict[str, dict[str, str]]:
    return {s: dict(cp[s]) for s in cp.sections()}


def _llama_write_ini(sections: dict[str, dict[str, Any]]) -> None:
    cp = configparser.ConfigParser(default_section="__DEFAULTS__", interpolation=None)
    cp.optionxform = str
    for section, values in sections.items():
        cp.add_section(section)
        for k, v in values.items():
            # hf-repo is derived from the section name; never persist it.
            if k == "hf-repo":
                continue
            if v not in (None, ""):
                cp.set(section, k, str(v))
    with open(_require_ctx().config.LLAMA_CONFIG_INI, "w") as f:
        cp.write(f)


def _locate_quant_files(model_id: str) -> "tuple[list[Path], Optional[str]]":
    """Resolve model_id -> its .gguf snapshot symlink(s) in the HF cache,
    validated against path traversal. Shared by delete + size lookup."""
    repo = None
    quant = None
    try:
        cp = _llama_read_ini()
        if cp.has_section(model_id):
            sec = cp[model_id]
            repo = sec.get("hf-repo") or sec.get("--hf-repo")
            quant = sec.get("hf-file") or sec.get("--hf-file")
    except Exception as e:
        log.warning("hf-cache lookup: read ini failed: %s", e, exc_info=True)

    if not repo and ":" in model_id and "/" in model_id:
        repo, quant = model_id.rsplit(":", 1)
    elif not repo:
        return [], f"Could not derive repo from model_id={model_id!r}"

    if not repo or not _hf_repo_valid(repo):
        return [], f"Repo missing or malformed: {repo!r}"
    if not quant:
        return [], "Quant identifier missing — refusing to wildcard-match the whole repo"
    # quant becomes a glob pattern under a snapshot dir; a single path component
    # only. Reject separators/parent refs so the pattern can't escape the cache.
    if "/" in quant or "\\" in quant or ".." in quant or "\x00" in quant:
        return [], f"Quant identifier rejected (path traversal): {quant!r}"

    cache_root = _hf_cache_root()
    repo_dir = cache_root / f"models--{repo.replace('/', '--')}"
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return [], f"Snapshots dir not found: {snapshots}"

    # Case-insensitive: model_id's quant token (e.g. "Q4_K_M") doesn't always
    # match the on-disk filename's casing (some repos ship it lowercase).
    quant_lower = quant.lower()
    matches: list[Path] = []
    for snap in snapshots.iterdir():
        if not snap.is_dir():
            continue
        gguf_files = [p for p in snap.iterdir() if p.suffix == ".gguf"]
        if quant.endswith(".gguf"):
            matches.extend(sorted(p for p in gguf_files
                                  if p.name.lower() == quant_lower))
        else:
            matches.extend(
                p for p in gguf_files
                if quant_lower in p.name.lower()
                and not p.name.lower().startswith("mmproj-")
            )
    if not matches:
        return [], f"No quant files matched {quant!r} under {snapshots}"
    return matches, None


def _delete_quant_from_hf_cache(model_id: str) -> "tuple[list[str], Optional[str]]":
    """Unlink the specific quant's .gguf from the HF cache. Returns (deleted_paths, error_or_none)."""
    candidates, err = _locate_quant_files(model_id)
    if err:
        return [], err

    deleted: list[str] = []
    for symlink in candidates:
        try:
            target = symlink.resolve() if symlink.is_symlink() else None
            symlink.unlink()
            deleted.append(str(symlink))
            if target and target.exists() and target.is_file():
                with best_effort("hf cache: unlink quant target", log=log):
                    target.unlink()
                    deleted.append(str(target))
        except Exception as e:
            return deleted, f"Failed to unlink {symlink}: {e}"

    return deleted, None


def _model_gguf_size_bytes(model_id: str) -> "Optional[int]":
    """Sum on-disk bytes of model_id's resolved .gguf snapshot file(s)."""
    matches, err = _locate_quant_files(model_id)
    if err:
        return None
    total = 0
    for p in matches:
        try:
            target = p.resolve() if p.is_symlink() else p
            total += target.stat().st_size
        except OSError:
            continue
    return total or None


_NGL_RE = re.compile(r"(?<![\w-])(?:--n-gpu-layers|-ngl)[=\s]+(\d+)")


def _extract_gpu_layers(node, _depth: int = 0) -> "Optional[int]":
    """Best-effort --n-gpu-layers scan of a /v1/models entry's status.args
    or preset text (any nested string/list/dict) — None if absent."""
    if _depth > 4:
        return None
    if isinstance(node, str):
        m = _NGL_RE.search(node)
        return int(m.group(1)) if m else None
    if isinstance(node, list):
        joined = " ".join(str(x) for x in node if isinstance(x, (str, int, float)))
        found = _extract_gpu_layers(joined, _depth + 1)
        if found is not None:
            return found
        for item in node:
            found = _extract_gpu_layers(item, _depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, dict):
        for v in node.values():
            found = _extract_gpu_layers(v, _depth + 1)
            if found is not None:
                return found
        return None
    return None


def _llama_fetch_model_entries() -> "dict[str, dict]":
    """id -> /v1/models entry (llama-swap router shape); {} on any failure —
    gpu_layers just comes back unknown for every model."""
    try:
        api = _require_ctx().config.LLAMA_API_URL.rstrip("/")
        resp = requests.get(f"{api}/v1/models", timeout=5)
        data = (resp.json() or {}).get("data") or []
    except Exception as e:
        log.debug("gpu-layers lookup: /v1/models unreachable: %s", e)
        return {}
    return {e["id"]: e for e in data if isinstance(e, dict) and e.get("id")}


_model_sizes_cache: "dict[str, Any]" = {"mtime": None, "sizes": {}, "layers": {}}


def _llama_catalog_sweep() -> "tuple[dict[str, int], dict[str, Optional[int]]]":
    """(sizes_bytes, gpu_layers) for every config.ini model section, cached
    together until LLAMA_CONFIG_INI's mtime changes."""
    ini_path = _require_ctx().config.LLAMA_CONFIG_INI
    try:
        mtime = os.path.getmtime(ini_path)
    except OSError:
        mtime = None
    if mtime is not None and _model_sizes_cache["mtime"] == mtime:
        return _model_sizes_cache["sizes"], _model_sizes_cache["layers"]

    entries = _llama_fetch_model_entries()
    sizes: dict[str, int] = {}
    layers: "dict[str, Optional[int]]" = {}
    for section in _llama_read_ini().sections():
        if section in ("*", "__DEFAULTS__"):
            continue
        size = _model_gguf_size_bytes(section)
        if size:
            sizes[section] = size
        layers[section] = _extract_gpu_layers(entries.get(section) or {})
    _model_sizes_cache["mtime"] = mtime
    _model_sizes_cache["sizes"] = sizes
    _model_sizes_cache["layers"] = layers
    return sizes, layers


def _llama_all_model_sizes() -> "dict[str, int]":
    """model_id -> gguf size bytes for every config.ini model section."""
    sizes, _layers = _llama_catalog_sweep()
    return sizes


def _llama_all_model_gpu_layers() -> "dict[str, Optional[int]]":
    """model_id -> --n-gpu-layers (None when absent/unknown)."""
    _sizes, layers = _llama_catalog_sweep()
    return layers


def _dl_put(msg: dict[str, Any]) -> None:
    """Bounded enqueue; drops oldest on overflow."""
    try:
        _dl_queue.put_nowait(msg)
    except _queue_lib.Full:
        try: _dl_queue.get_nowait()
        except _queue_lib.Empty: pass
        try: _dl_queue.put_nowait(msg)
        except _queue_lib.Full: pass


def _llama_run_command(cmd: list, stdin_input: "Optional[bytes]" = None,
                       dry_run: bool = False) -> None:
    """Streaming command runner; PTY for progress bars, pipe mode when stdin_input is needed."""
    global _dl_active, _dl_proc, _dl_cancelled
    _dl_cancelled = False
    try:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        if dry_run:
            env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

        _dl_put({"type": "start", "cmd": " ".join(str(c) for c in cmd)})

        if stdin_input:
            env["FORCE_COLOR"] = "0"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                close_fds=True,
                env=env,
            )
            assert proc.stdin is not None and proc.stdout is not None
            _dl_proc = proc
            proc.stdin.write(stdin_input)
            proc.stdin.close()
            for raw in iter(proc.stdout.readline, b""):
                line = llama_install.strip_ansi(raw.decode("utf-8", errors="replace")).strip()
                if line:
                    _dl_put({"type": "line", "text": line})
            proc.wait()
        else:
            import fcntl
            import pty
            import select as _select
            import struct
            import termios

            env["TERM"] = "xterm-256color"
            env["COLUMNS"], env["LINES"] = str(_DL_PTY_COLS), str(_DL_PTY_ROWS)
            master_fd, slave_fd = pty.openpty()
            with best_effort("download: set pty winsize", log=log):
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", _DL_PTY_ROWS, _DL_PTY_COLS, 0, 0))
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    env=env,
                )
                _dl_proc = proc
                os.close(slave_fd)
                slave_fd = -1

                buf = ""
                last_line = ""
                frames: "dict[str, str]" = {}  # newest \r frame per bar, held by the throttle
                frame_ts = 0.0

                sent_frames: "dict[str, str]" = {}

                def _flush_frames() -> None:
                    # Emit held-back frames tagged progress=True so the console
                    # can update each bar's line in place; unchanged frames for
                    # a bar are not re-sent.
                    nonlocal last_line
                    for k, f in frames.items():
                        if f != last_line and sent_frames.get(k) != f:
                            _dl_put({"type": "line", "text": f, "progress": True})
                            last_line = f
                            sent_frames[k] = f
                    frames.clear()

                while True:
                    try:
                        r, _, _ = _select.select([master_fd], [], [], 0.5)
                        now = time.monotonic()
                        if frames and now - frame_ts >= _DL_FRAME_INTERVAL_S:
                            _flush_frames()
                            frame_ts = now
                        if not r:
                            if proc.poll() is not None:
                                break
                            continue
                        try:
                            data = os.read(master_fd, 2048)
                        except OSError:
                            break
                        if not data:
                            break
                        text = data.decode("utf-8", errors="replace")
                        text = llama_install.strip_ansi(text)
                        buf = (buf + text).replace("\r\n", "\n")
                        parts = re.split(r"([\r\n])", buf)
                        buf = parts[-1]
                        # Plain lines emit as-is; frame-shaped lines are keyed by
                        # bar prefix, flushed at most once per _DL_FRAME_INTERVAL_S.
                        for seg, sep in zip(parts[0::2], parts[1::2]):
                            line = seg.strip()
                            if not line or line == last_line:
                                continue
                            if sep == "\r" or _DL_FRAME_RE.search(line):
                                frames[line.split(":", 1)[0]] = line
                                if now - frame_ts >= _DL_FRAME_INTERVAL_S:
                                    _flush_frames()
                                    frame_ts = now
                                continue
                            _flush_frames()
                            _dl_put({"type": "line", "text": line})
                            last_line = line
                    except (OSError,):
                        break

                _flush_frames()
                tail = buf.strip()
                if tail and tail != last_line:
                    _dl_put({"type": "line", "text": tail})
            finally:
                if proc is not None:
                    try: proc.wait(timeout=5)
                    except Exception:
                        with best_effort("download: terminate proc", log=log):
                            proc.terminate()
                        try: proc.wait(timeout=3)
                        except Exception:
                            with best_effort("download: kill proc", log=log):
                                proc.kill()
                # close pty fds; ignore if already closed
                if slave_fd != -1:
                    try: os.close(slave_fd)
                    except OSError: pass
                try: os.close(master_fd)
                except OSError: pass

        rc = getattr(proc, "returncode", 1)
        if _dl_cancelled:
            _dl_put({"type": "line", "text": "[cancelled by operator]"})
            _dl_put({"type": "done", "ok": False, "cancelled": True, "rc": rc, "dry_run": dry_run})
        else:
            _dl_put({"type": "done", "ok": rc == 0, "rc": rc, "dry_run": dry_run})
    except Exception as e:
        log.error("_llama_run_command error: %s", e, exc_info=True)
        _dl_put({"type": "done", "ok": False, "error": str(e), "dry_run": dry_run})
    finally:
        _dl_proc = None
        with _dl_lock:
            _dl_active = False


def _llama_queue_command(cmd: list, stdin_input: "Optional[bytes]" = None,
                         dry_run: bool = False) -> bool:
    """Start the run in a thread; False if one is already in flight."""
    global _dl_active
    with _dl_lock:
        if _dl_active:
            return False
        _dl_active = True
    while not _dl_queue.empty():
        try: _dl_queue.get_nowait()
        except Exception: break
    threading.Thread(target=_llama_run_command,
                     args=(cmd, stdin_input, dry_run),
                     daemon=True).start()
    return True


def _llama_log_streamer() -> None:
    """tail -F llama-server.log from now → subscribers, dropping idle/noisy lines."""
    _log_state.pump(
        ["tail", "-n", "0", "-F", _require_ctx().config.LLAMA_LOG_FILE],
        should_keep=_llama_log_should_keep,
    )


def llama_server_status_endpoint(
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    _llama_check_enabled()
    try:
        r = subprocess.run(
            ["systemctl", "status", _require_ctx().config.LLAMA_SYSTEMD_UNIT, "--no-pager", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        return {"ok": True, "output": r.stdout + r.stderr}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _llama_systemctl(action: str, timeout: int = 30) -> dict[str, Any]:
    try:
        r = subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", action, _require_ctx().config.LLAMA_SYSTEMD_UNIT],
            capture_output=True, text=True, timeout=timeout,
        )
        log.info("llama-server %s: rc=%s %s", action, r.returncode, r.stderr.strip())
        return {"ok": r.returncode == 0, "error": r.stderr.strip() or None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def llama_server_start_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    return _llama_systemctl("start")


def llama_server_stop_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    return _llama_systemctl("stop")


def llama_server_restart_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    return _llama_systemctl("restart", timeout=60)


def llama_server_wake_endpoint(body: Optional[dict] = None,
                               authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    # GET /v1/models, target the requested model else the sleeping/loaded one, then warm it.
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    base = _require_ctx().config.LLAMA_API_URL.rstrip("/")
    requested = (body or {}).get("model") or (body or {}).get("model_id")
    model_id = None
    try:
        mr = _require_ctx().post_session.get(f"{base}/v1/models", timeout=5)
        if not mr.ok:
            return {"ok": False, "status": mr.status_code,
                    "error": f"GET /v1/models returned HTTP {mr.status_code}: {mr.text[:200]}"}
        data = mr.json() or {}
        model_id = llama_sse.select_wake_target(data.get("data") or [], requested)
    except Exception as e:
        return {"ok": False, "error": f"GET /v1/models failed: {e}"}
    if not model_id:
        return {"ok": False, "error": "no model is loaded on the llama-server "
                                       "— wake has nothing to warm. Load one first."}
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "."}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        r = _require_ctx().post_session.post(f"{base}/v1/chat/completions",
                               json=payload, timeout=60)
        if not r.ok:
            return {"ok": False, "status": r.status_code,
                    "model": model_id, "error": r.text[:300]}
        return {"ok": True, "status": r.status_code, "model": model_id}
    except Exception as e:
        return {"ok": False, "model": model_id, "error": str(e)}


def _llama_svc_file_path() -> str:
    return f"/etc/systemd/system/{_require_ctx().config.LLAMA_SYSTEMD_UNIT}"


def _svcconfig_wrapper_baked_unit_path() -> str:
    """UNIT_PATH baked into the installed wrapper, or "" if unreadable."""
    return _shared.wrapper_baked_unit_path(_SVCCONFIG_WRAPPER)


def llama_svcconfig_get(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    try:
        content = Path(_llama_svc_file_path()).read_text()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("ExecStart="):
            exec_line = stripped[len("ExecStart="):].strip()
            parts = shlex.split(exec_line)
            binary = parts[0]
            args: list[dict[str, Any]] = []
            i = 1
            while i < len(parts):
                p = parts[i]
                if p.startswith("-"):
                    nxt = parts[i + 1] if i + 1 < len(parts) else None
                    expects_value = (p in _LLAMA_VALUE_FLAGS
                                     or (nxt is not None and not nxt.startswith("-")))
                    if expects_value and nxt is not None:
                        args.append({"flag": p, "value": nxt, "bool": False})
                        i += 2
                    else:
                        args.append({"flag": p, "value": None, "bool": True})
                        i += 1
                else:
                    i += 1
            return {"ok": True, "binary": binary, "args": args}
    return {"ok": False, "error": "ExecStart line not found in service file"}


def llama_svcconfig_post(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    do_restart = bool(body.get("restart", False))
    # stdin tokens: line 1 = binary, then one ExecStart token per line. The
    # root-owned wrapper rewrites only the unit's ExecStart line.
    tokens = _shared.build_svcconfig_tokens([body.get("binary", "")],
                                            body.get("args", []))
    if tokens is None:
        return {"ok": False, "error": "invalid ExecStart token (newline or non-string)"}
    return _shared.svcconfig_apply(
        _SVCCONFIG_WRAPPER, _llama_svc_file_path(), tokens,
        sudoers_hint="llm-svcconfig-apply",
        restart_unit=_require_ctx().config.LLAMA_SYSTEMD_UNIT if do_restart else None,
        restart_timeout=30)


def llama_log_tail(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Last ~50 filtered lines of the llama log (no streaming).
    Reads only the last 128 KB to cap memory; uses a bounded deque."""
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    TAIL_BYTES = 128 * 1024
    path = _require_ctx().config.LLAMA_LOG_FILE
    try:
        size = os.path.getsize(path)
        offset = max(0, size - TAIL_BYTES)
        with open(path, "rb") as f:
            if offset:
                f.seek(offset)
                f.readline()  # discard partial first line
            data = f.read()
        out: deque = deque(maxlen=100)
        for raw in data.splitlines():
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line and _llama_log_should_keep(line):
                out.append(line)
        return {"ok": True, "lines": list(out)}
    except FileNotFoundError:
        return {"ok": True, "lines": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def llama_log_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    """SSE stream of llama-server log lines.

    Auth: either the long-lived Authorization: Bearer header (used by
    server-to-server calls + the manager's two-hop proxy) OR a
    short-lived ?token= stream token (used by the browser when going
    direct to the agent — EventSource can't set custom headers).
    """
    _require_ctx().check_stream_auth(authorization, token, "/llama/log/stream")
    _llama_check_enabled()
    return _log_state.sse_response(_llama_log_streamer)


def llama_models_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    try:
        resp = requests.get(f"{_require_ctx().config.LLAMA_API_URL.rstrip('/')}/v1/models", timeout=5)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


def llama_model_sizes_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """sizes (bytes) + meta.gpu_layers per model, for autopilot's offload-
    aware fit check (#472/#474/#475); meta is additive, old readers unaffected."""
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    sizes, layers = _llama_catalog_sweep()
    return {"ok": True, "sizes": sizes,
            "meta": {mid: {"gpu_layers": gl} for mid, gl in layers.items()}}


async def _llama_openai_forward(sub: str, request: Request,
                                authorization: "Optional[str]"):
    """Narrow OpenAI passthrough to llama-server /v1/<sub> (#214)."""
    ctx = _require_ctx()
    ctx.check_bearer(authorization)
    _llama_check_enabled()
    return await _shared.openai_forward(sub, request, ctx.config.LLAMA_API_URL)


async def llama_openai_chat(request: Request,
                            authorization: Optional[str] = Header(default=None)):
    return await _llama_openai_forward("chat/completions", request, authorization)


async def llama_openai_completions(request: Request,
                                   authorization: Optional[str] = Header(default=None)):
    return await _llama_openai_forward("completions", request, authorization)


_LLAMA_UNLOAD_SETTLE_S = 30.0
# Router statuses that hold the single model slot.
_LLAMA_BUSY_STATUSES = ("loaded", "loading", "sleeping")


def _llama_wait_unloaded(api: str, timeout_s: float = _LLAMA_UNLOAD_SETTLE_S,
                         poll_s: float = 0.5) -> bool:
    """Poll /v1/models until no instance holds the slot; a failed poll counts
    as busy. False once timeout_s elapses."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        try:
            mr = requests.get(f"{api}/v1/models", timeout=max(0.5, min(5.0, remaining)))
            if not mr.ok:
                raise RuntimeError(f"HTTP {mr.status_code}")
            busy = [m.get("id") for m in (mr.json() or {}).get("data", [])
                    if isinstance(m.get("status"), dict)
                    and m["status"].get("value") in _LLAMA_BUSY_STATUSES]
        except Exception as e:
            busy = [f"(poll failed: {e})"]
        if not busy:
            return True
        if time.monotonic() >= deadline:
            log.warning("llama unload did not settle within %.0fs: %s", timeout_s, busy)
            return False
        time.sleep(poll_s)


def _json_or_raw(resp) -> Any:
    """Parsed JSON body, else {"raw": <first 500 chars of text>}."""
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:500]}


def llama_load_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")
    api = _require_ctx().config.LLAMA_API_URL.rstrip("/")
    try:
        mr = requests.get(f"{api}/v1/models", timeout=5)
        for m in mr.json().get("data", []):
            st = m.get("status", {})
            if isinstance(st, dict) and st.get("value") in _LLAMA_BUSY_STATUSES:
                log.info("Unloading %s before loading %s", m["id"], model_id)
                ur = requests.post(f"{api}/models/unload",
                                   json={"model": m["id"]}, timeout=30)
                log.info("Unload response: %s %s", ur.status_code, ur.text[:200])

        if not _llama_wait_unloaded(api):
            return {"ok": False,
                    "error": "previous model instance did not unload in time"}
        log.info("Loading model: %s", model_id)
        lr = requests.post(f"{api}/models/load", json={"model": model_id}, timeout=120)
        log.info("Load response: %s %s", lr.status_code, lr.text[:200])

        if lr.status_code == 404:
            raise HTTPException(status_code=404,
                                detail=f"Model not found in llama-server (404). "
                                       f"Verify '{model_id}' matches a registered model ID.")
        body_resp = _json_or_raw(lr)
        if not lr.ok:
            return {"ok": False,
                    "error": f"llama-server returned HTTP {lr.status_code}",
                    "response": body_resp}
        return {"ok": True, "response": body_resp}
    except HTTPException:
        raise
    except Exception as e:
        log.error("llama_load_endpoint error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _llama_model_idle(model_id: str) -> bool:
    """True when /v1/models lists model_id with an explicit status outside
    loaded/loading/sleeping."""
    st = (_llama_fetch_model_entries().get(model_id) or {}).get("status")
    return isinstance(st, dict) and st.get("value") not in _LLAMA_BUSY_STATUSES


def llama_unload_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")
    api = _require_ctx().config.LLAMA_API_URL.rstrip("/")
    try:
        resp = requests.post(f"{api}/models/unload", json={"model": model_id}, timeout=15)
        body_resp = _json_or_raw(resp)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not resp.ok:
        if _llama_model_idle(model_id):
            return {"ok": True, "already_unloaded": True, "response": body_resp}
        return {"ok": False,
                "error": f"llama-server returned HTTP {resp.status_code}",
                "response": body_resp}
    if not _llama_wait_unloaded(api):
        return {"ok": False, "error": "model instance did not unload in time",
                "response": body_resp}
    return {"ok": True, "response": body_resp}


def llama_config_get(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    try:
        return _llama_ini_to_dict(_llama_read_ini())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def llama_config_post(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    try:
        _llama_write_ini(body)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def llama_config_delete(
    model_id: str,
    delete_cache: bool = Query(default=False),
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    try:
        deleted_files: list[str] = []
        cache_error: "Optional[str]" = None
        if delete_cache:
            try:
                deleted_files, cache_error = _delete_quant_from_hf_cache(model_id)
            except Exception as e:
                cache_error = str(e)
        cp = _llama_read_ini()
        if cp.has_section(model_id):
            cp.remove_section(model_id)
            with open(_require_ctx().config.LLAMA_CONFIG_INI, "w") as f:
                cp.write(f)
        return {"ok": True, "deleted_files": deleted_files, "cache_error": cache_error}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _hf_cli_path() -> str:
    return _require_ctx().config.HF_CLI_PATH or shutil.which("hf") or os.path.expanduser("~/.local/bin/hf")


def llama_download_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    repo = (body.get("repo") or "").strip()
    patterns = body.get("patterns") or []
    include = (body.get("include") or "").strip()
    dry_run = bool(body.get("dry_run", False))
    if not repo:
        raise HTTPException(status_code=400, detail="repo required")
    if not _hf_repo_valid(repo):
        raise HTTPException(status_code=400, detail="invalid repo format — expected owner/repo")
    cmd = [_hf_cli_path(), "download", repo]
    all_patterns = list(patterns)
    if include and f"*{include}*" not in all_patterns:
        all_patterns.append(f"*{include}*")
    for p in all_patterns:
        cmd += ["--include", p]
    if dry_run:
        cmd.append("--dry-run")
        cmd += ["--format", "json"]
    else:
        # Pin 'human' so hf keeps progress bars on (default 'auto' disables them here).
        cmd += ["--format", "human"]
    if not _llama_queue_command(cmd, dry_run=dry_run):
        raise HTTPException(status_code=409, detail="Another operation is in progress")
    return {"ok": True}


def llama_download_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """SIGTERM the active hf download/cache process, SIGKILL after grace window."""
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    global _dl_cancelled
    proc = _dl_proc
    if proc is None or proc.poll() is not None:
        return {"ok": False, "error": "no active download"}
    _dl_cancelled = True
    try:
        proc.terminate()
    except Exception as e:
        return {"ok": False, "error": f"terminate failed: {e}"}
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with best_effort("download cancel: kill proc", log=log):
            proc.kill()
        with best_effort("download cancel: reap proc", log=log):
            proc.wait(timeout=2)
    return {"ok": True, "pid": proc.pid, "rc": proc.returncode}


def llama_download_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    _require_ctx().check_stream_auth(authorization, token, "/llama/download/stream")
    _llama_check_enabled()

    def generate() -> Iterator[bytes]:
        while True:
            try:
                msg = _dl_queue.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n".encode()
                if msg.get("type") == "done":
                    break
            except _queue_lib.Empty:
                yield b'data: {"type":"keepalive"}\n\n'

    if not stream_pool.POOL.try_acquire():
        raise HTTPException(status_code=503, detail="agent at stream capacity; retry shortly")
    return StreamingResponse(
        stream_pool.guarded_async(generate()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _build_put(msg: dict[str, Any]) -> None:
    """Bounded enqueue; drops oldest on overflow."""
    try:
        _build_queue.put_nowait(msg)
    except _queue_lib.Full:
        try: _build_queue.get_nowait()
        except _queue_lib.Empty: pass
        try: _build_queue.put_nowait(msg)
        except _queue_lib.Full: pass


def _llama_build_worker() -> None:
    global _build_running
    rc = 1
    resolved = None
    cfg = _require_ctx().config
    method = (getattr(cfg, "LLAMA_BUILD_METHOD", "") or "custom_script")
    opts = getattr(cfg, "LLAMA_BUILD_OPTS", None) or {}
    try:
        try:
            iplan = llama_install.plan(method, opts, cfg)
        except llama_install.InstallError as e:
            _build_put({"type": "line", "data": f"[error] {e}", "text": f"[error] {e}"})
            rc = 2
            return
        joined = " && ".join(" ".join(s) for s in iplan.steps)
        _build_put({"type": "start", "cmd": joined, "method": iplan.label})
        emit = lambda line: _build_put({"type": "line", "data": line, "text": line})
        rc, resolved = llama_install.run_install(iplan, emit=emit)
        if rc == 0:
            bin_cfg = getattr(cfg, "LLAMA_BIN", "") or ""
            if llama_upgrade.should_upgrade_in_place(method, opts) and bin_cfg and resolved:
                try:
                    br = str(llama_install._build_root(cfg))
                except Exception as e:
                    emit(f"[warn] could not resolve build root: {e}; tarball cleanup skipped")
                    br = None
                try:
                    retain = int(opts.get("backup_retain", 2))
                except (TypeError, ValueError):
                    emit(f"[warn] backup_retain {opts.get('backup_retain')!r} is not an integer; using 2")
                    retain = 2
                res = llama_upgrade.upgrade_in_place(
                    resolved, bin_cfg, build_root=br,
                    unit=getattr(cfg, "LLAMA_SYSTEMD_UNIT", "") or "llama_server.service",
                    agent_user=getattr(cfg, "AGENT_USER", "") or "",
                    retain=retain, emit=emit,
                )
                if res.ok:
                    resolved = res.target or bin_cfg
                    # Clean build artifacts after a real swap, or after a
                    # release_binary no-op (its re-extracted dir is disposable).
                    if not res.skipped or method == "release_binary":
                        try:
                            llama_install.cleanup_after_inplace(cfg, method, emit=emit)
                        except Exception as e:
                            emit(f"[warn] post-upgrade cleanup failed: {e}")
                else:
                    rc = 3
            elif resolved and bin_cfg and resolved != bin_cfg:
                warn = (f"[warn] llama-server installed at {resolved}; configured "
                        f"LLAMA_BIN={bin_cfg} — update LLAMA_BIN and restart "
                        f"{getattr(cfg, 'LLAMA_SYSTEMD_UNIT', 'the llama unit')} to run it")
                _build_put({"type": "line", "data": warn, "text": warn})
    except FileNotFoundError as e:
        rc = 127
        _build_put({"type": "line", "data": f"[error] {e}", "text": f"[error] {e}"})
    except Exception as e:
        rc = rc or 1
        log.error("_llama_build_worker error: %s", e, exc_info=True)
        _build_put({"type": "line", "data": f"[error] {e}", "text": f"[error] {e}"})
    finally:
        _build_put({"type": "done", "ok": rc == 0, "rc": rc, "method": method, "path": resolved})
        with _build_lock:
            _build_running = False


def llama_build(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    global _build_running
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    with _build_lock:
        if _build_running:
            raise HTTPException(status_code=409, detail="A build is already running")
        while not _build_queue.empty():
            try: _build_queue.get_nowait()
            except _queue_lib.Empty: break
        _build_running = True
    threading.Thread(target=_llama_build_worker, daemon=True).start()
    return {"ok": True}


def llama_build_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    _require_ctx().check_stream_auth(authorization, token, "/llama/build/stream")
    _llama_check_enabled()

    def generate() -> Iterator[bytes]:
        while True:
            try:
                msg = _build_queue.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n".encode()
                if msg.get("type") == "done":
                    break
            except _queue_lib.Empty:
                yield b'data: {"type":"keepalive"}\n\n'

    if not stream_pool.POOL.try_acquire():
        raise HTTPException(status_code=503, detail="agent at stream capacity; retry shortly")
    return StreamingResponse(
        stream_pool.guarded_async(generate()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def llama_cache_list(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    env = dict(os.environ)
    env["FORCE_COLOR"] = "0"
    try:
        out = subprocess.check_output(
            [_hf_cli_path(), "cache", "list", "--format", "json"],
            text=True, timeout=30, close_fds=True,
            stderr=subprocess.STDOUT, env=env,
        )
        try:
            data = json.loads(out)
        except Exception:
            return {"ok": True, "data": [], "raw": out}
        return {"ok": True, "data": data if isinstance(data, list) else [data]}
    except subprocess.CalledProcessError as e:
        return {"ok": True, "data": [], "raw": getattr(e, "output", str(e))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def llama_cache_gguf(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    try:
        return {"ok": True, "data": _list_cache_ggufs(_hf_cache_root())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def llama_cache_prune(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    if not _llama_queue_command([_hf_cli_path(), "cache", "prune"], stdin_input=b"y\n"):
        raise HTTPException(status_code=409, detail="Another operation is in progress")
    return {"ok": True}


def llama_cache_rm(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    repo_id = (body.get("repo") or "").strip()
    if not repo_id:
        raise HTTPException(status_code=400, detail="repo required")
    if not _hf_repo_valid(repo_id):
        raise HTTPException(status_code=400, detail="invalid repo format — expected owner/repo")
    if not _llama_queue_command(
        [_hf_cli_path(), "cache", "rm", "model/" + repo_id],
        stdin_input=b"y\n",
    ):
        raise HTTPException(status_code=409, detail="Another operation is in progress")
    return {"ok": True}


def llama_hf_trending(
    authorization: Optional[str] = Header(default=None),
    limit: int = 10,
    min_b: str = "27B",
    max_b: str = "35B",
    sort: str = "trending",
) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    limit = max(1, min(50, int(limit)))
    if not re.fullmatch(r"\d{1,4}B", str(min_b) or ""):
        min_b = "27B"
    if not re.fullmatch(r"\d{1,4}B", str(max_b) or ""):
        max_b = "35B"
    if sort not in ("trending", "downloads", "newest"):
        sort = "trending"
    # The Hub API sorts "downloads" by the 30-day count and "createdAt" by raw
    # creation time (all just-pushed empty repos), so: downloads re-ranks the
    # fetched rows by all-time count, and newest sorts a trending pool by
    # creation date and slices to the requested limit.
    pool = 100 if sort == "newest" else limit
    sort_key = "downloads" if sort == "downloads" else "trending_score"
    env = dict(os.environ)
    env["FORCE_COLOR"] = "0"
    try:
        out = subprocess.check_output(
            [_hf_cli_path(), "models", "ls",
             "--sort", sort_key,
             "--limit", str(pool),
             "--format", "json",
             "--expand", "author,downloadsAllTime,trendingScore,createdAt,lastModified",
             "--num-parameters", f"min:{min_b},max:{max_b}"],
            text=True, timeout=30, close_fds=True,
            stderr=subprocess.DEVNULL, env=env,
        )
        try:
            data = json.loads(out)
        except Exception:
            return {"ok": False, "error": "Failed to parse JSON", "raw": out}
        data = data if isinstance(data, list) else [data]
        if sort == "downloads":
            data.sort(key=lambda m: (m or {}).get("downloads_all_time") or 0, reverse=True)
        elif sort == "newest":
            data.sort(key=lambda m: str((m or {}).get("created_at") or ""), reverse=True)
            data = data[:limit]
        return {"ok": True, "data": data}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": getattr(e, "output", str(e))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _bench_put(msg: dict) -> None:
    """Append to the per-run replay buffer and wake any waiting streams."""
    with _bench_cond:
        _bench_replay.append(msg)
        _bench_cond.notify_all()


def _bench_get_hf_arg(model_id: str) -> "Optional[str]":
    try:
        cp = _llama_read_ini()
        if cp.has_section(model_id):
            sec = cp[model_id]
            repo = sec.get("hf-repo") or sec.get("--hf-repo")
            quant = sec.get("hf-file") or sec.get("--hf-file")
            if repo:
                return f"{repo}:{quant}" if quant else repo
    except Exception as e:
        log.warning("bench hf arg lookup failed: %s", e)
    if "/" in model_id:
        return model_id
    return None


def _bench_parse_row(row: dict, tool: str):
    """(gen_tps, ppt_tps, pg_tps) for one JSONL row; all None when not a result row."""
    if not isinstance(row, dict) or tool != "llama-bench":
        return (None, None, None)
    ts_val = row.get("avg_ts") or row.get("t_s")
    if ts_val is None:
        return (None, None, None)
    n_p = int(row.get("n_prompt", 0) or 0)
    n_g = int(row.get("n_gen", 0) or 0)
    if n_p > 0 and n_g == 0: return (None, float(ts_val), None)
    if n_g > 0 and n_p == 0: return (float(ts_val), None, None)
    if n_p > 0 and n_g > 0:  return (None, None, float(ts_val))
    return (None, None, None)


def _bench_tool_path(tool: str) -> "tuple[str, bool]":
    """Resolve a benchmark tool: prefer the copy beside LLAMA_BIN, else fall back
    to PATH. Returns (path, found); path is the sibling location when not found."""
    if not _require_ctx().config.LLAMA_BIN:
        raise RuntimeError("LLAMA_BIN not configured")
    sibling = Path(_require_ctx().config.LLAMA_BIN).parent / tool
    if sibling.exists():
        return str(sibling), True
    found = shutil.which(tool)
    if found:
        return found, True
    return str(sibling), False


def _bench_run_one(model_id: str, tool: str, switches: list, env: dict) -> None:
    global _bench_proc, _bench_pgid
    try:
        tool_path, found = _bench_tool_path(tool)
    except Exception as e:
        _bench_put({"type": "model_done", "model_id": model_id, "ok": False, "error": str(e)})
        return
    if not found:
        _bench_put({"type": "model_done", "model_id": model_id, "ok": False,
                    "error": f"{tool} not found beside LLAMA_BIN ({tool_path}) or on PATH"})
        return
    hf_arg = _bench_get_hf_arg(model_id)
    if not hf_arg:
        _bench_put({"type": "model_done", "model_id": model_id, "ok": False,
                    "error": f"no HF reference found for {model_id}"})
        return
    jsonl_flags = ["-o", "jsonl"]
    energy = _bl.PowerIntegrator(_live_power_w)
    tokens_total = 0
    cmd = [tool_path]
    for sw in switches or []:
        flag = (sw.get("flag") or "").strip()
        if not flag:
            continue
        cmd.append(flag)
        val = sw.get("value")
        if val is not None and str(val).strip() != "":
            cmd.append(str(val))
    cmd += ["-hf", hf_arg]
    cmd += jsonl_flags
    _bench_put({"type": "model_start", "model_id": model_id,
                "cmd": " ".join(str(c) for c in cmd)})
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, text=True, bufsize=1,
        close_fds=True, env=env, start_new_session=True,
    )
    _bench_proc = proc
    try: _bench_pgid = os.getpgid(proc.pid)
    except Exception: _bench_pgid = None
    stopped = False

    try:
        energy.start()
        latest_gen = None
        latest_ppt = None
        latest_pg = None
        result_rows: list = []

        def _drain_stderr():
            with best_effort("bench: drain subprocess stderr", log=log):
                for line in iter(proc.stderr.readline, ""):
                    if not line: break
                    txt = _BENCH_ANSI_RE.sub("", line.rstrip("\n"))
                    if txt:
                        _bench_put({"type": "line", "model_id": model_id, "text": txt})
        threading.Thread(target=_drain_stderr, daemon=True).start()

        for raw in iter(proc.stdout.readline, ""):
            if _bench_cancel_event.is_set():
                break
            if not raw:
                break
            line = _BENCH_ANSI_RE.sub("", raw.rstrip("\n"))
            if not line:
                continue
            _bench_put({"type": "line", "model_id": model_id, "text": line})
            try:
                row = json.loads(line)
            except Exception:
                continue
            try:
                gen_tps, ppt_tps, pg_tps = _bench_parse_row(row, tool)
                if gen_tps is None and ppt_tps is None and pg_tps is None:
                    continue
                result_row = {
                    "n_prompt": int(row.get("n_prompt", 0) or 0),
                    "n_gen":    int(row.get("n_gen", 0) or 0),
                    "n_depth":  int(row.get("n_depth", 0) or 0),
                    "n_batch":  int(row.get("n_batch", 0) or 0),
                    "n_ubatch": int(row.get("n_ubatch", 0) or 0),
                    "avg_ts":   float(row.get("avg_ts", 0) or 0),
                    "type_k":   str(row.get("type_k") or ""),
                    "type_v":   str(row.get("type_v") or ""),
                }
                s = row.get("samples_ns")
                reps = len(s) if isinstance(s, list) and s else 1
            except (TypeError, ValueError):
                continue
            if gen_tps is not None: latest_gen = gen_tps
            if ppt_tps is not None: latest_ppt = ppt_tps
            if pg_tps is not None: latest_pg = pg_tps
            result_rows.append(result_row)
            tokens_total += (result_row["n_prompt"] + result_row["n_gen"]) * reps
            _bench_put({"type": "result", "model_id": model_id,
                        "gen_tps": gen_tps, "ppt_tps": ppt_tps, "pg_tps": pg_tps,
                        **result_row})

        proc.wait()
        _bench_proc = None
        wh, src = energy.stop()
        stopped = True
        wh_per_ktok = (wh / (tokens_total / 1000.0)) if wh is not None and tokens_total > 0 else None
        cancelled = _bench_cancel_event.is_set()
        mx = _shared.bench_maxes(result_rows)
        measured = any(v is not None for v in mx.values())
        _bench_put({"type": "model_done", "model_id": model_id,
                    "ok": (not cancelled) and proc.returncode == 0,
                    "rc": proc.returncode, "cancelled": cancelled,
                    "last_gen_tps": latest_gen, "last_ppt_tps": latest_ppt,
                    "last_pg_tps": latest_pg, "results": result_rows,
                    "max_gen_tps": mx["gen"], "max_ppt_tps": mx["ppt"],
                    "max_pg_tps": mx["pg"], "run_id": _bench_replay.run_id,
                    "energy_wh": wh, "energy_source": src,
                    "wh_per_ktok": wh_per_ktok, "tokens": tokens_total})
        _shared.post_tool_run(
            _require_ctx(), "benchmark", "llama", _bench_replay.run_id, model_id,
            measured, {"gen_tps": mx["gen"], "ppt_tps": mx["ppt"],
                       "pg_tps": mx["pg"], "bench_tool": tool,
                       "wh_per_ktok": wh_per_ktok})
    finally:
        if not stopped:
            energy.stop()


def _bench_run_all(model_ids: list, tool: str, switches: list):
    global _bench_active, _bench_proc
    _bench_cancel_event.clear()
    try:
        env = os.environ.copy()
        parent = str(Path(_require_ctx().config.LLAMA_BIN).parent) if _require_ctx().config.LLAMA_BIN else ""
        existing = env.get("LD_LIBRARY_PATH", "")
        if parent:
            env["LD_LIBRARY_PATH"] = f"{parent}:{existing}" if existing else parent
        for mid in model_ids:
            if _bench_cancel_event.is_set():
                break
            _bench_run_one(mid, tool, switches, env)
        cancelled = _bench_cancel_event.is_set()
        _bench_put({"type": "done", "ok": not cancelled, "cancelled": cancelled,
                    "count": len(model_ids)})
    except Exception as e:
        log.error("bench run error: %s", e, exc_info=True)
        _bench_put({"type": "done", "ok": False, "error": str(e)})
    finally:
        _bench_proc = None
        with _bench_lock:
            _bench_active = False


def llama_bench_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    global _bench_active
    model_ids = body.get("model_ids") or []
    if isinstance(model_ids, str):
        model_ids = [model_ids]
    model_ids = [str(m).strip() for m in model_ids if str(m).strip()]
    if not model_ids:
        raise HTTPException(status_code=400, detail="model_ids required")
    tool = (body.get("tool") or "").strip()
    if tool != "llama-bench":
        raise HTTPException(status_code=400, detail="invalid tool")
    switches = body.get("switches") or []
    if not isinstance(switches, list):
        raise HTTPException(status_code=400, detail="switches must be a list")
    with _bench_lock:
        if _bench_active:
            return {"ok": False, "error": "Another benchmark is in progress"}
        _bench_active = True
        # Reset the buffer before the lock drops: a stream landing between
        # active=True and start_run would replay the prior run's stale done.
        with _bench_cond:
            _bench_replay.start_run(uuid.uuid4().hex[:12])
    threading.Thread(target=_bench_run_all,
                     args=(model_ids, tool, switches), daemon=True).start()
    return {"ok": True}


def llama_bench_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    _require_ctx().check_stream_auth(authorization, token, "/llama/bench/stream")
    _llama_check_enabled()
    return _shared.bench_replay_sse(
        _bench_replay, _bench_cond, lambda: _bench_active, last_event_id)


def _pkill_strays(patterns: list[str], what: str) -> dict[str, Any]:
    """pkill -9 each pattern, reporting honestly: rc 0/1 ok, rc >=2 or exception is a failure."""
    failures: list[str] = []
    for pat in patterns:
        try:
            rc = subprocess.run(['pkill', '-9', '-f', pat], capture_output=True, timeout=3).returncode
        except Exception as e:
            log.warning("%s: pkill -f %s failed to run: %s", what, pat, e)
            failures.append(f"{pat}: {e}")
            continue
        if rc >= 2:
            log.warning("%s: pkill -f %s exited %d", what, pat, rc)
            failures.append(f"{pat}: rc={rc}")
    if failures:
        return {"ok": False, "error": "pkill failed: " + "; ".join(failures)}
    return {"ok": True}


def llama_bench_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    _bench_cancel_event.set()
    proc, pgid = _bench_proc, _bench_pgid
    if proc is None:
        res = _pkill_strays(['llama-bench'], "bench cancel")
        if res["ok"]:
            res["msg"] = "no tracked benchmark process"
        return res
    try:
        if pgid is not None:
            # tolerate the group already being gone
            try: os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError: pass
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                try:
                    # force-kill; tolerate the group already being gone
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    log.warning("bench cancel: killpg SIGKILL failed: %s", e)
        with best_effort("bench cancel: pkill HIP/ROCm child procs", log=log):
            subprocess.run(['pkill', '-9', '-f', 'llama-bench'], capture_output=True, timeout=3)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.warning("bench cancel: process %s survived SIGKILL", proc.pid)
            return {"ok": False, "error": "benchmark process did not terminate"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def llama_bench_perf_mode(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    mode = (body.get("mode") or "").strip()
    if mode not in ("performance", "powersave"):
        return {"ok": False, "error": "mode must be 'performance' or 'powersave'"}
    try:
        # sudoers permits `reload-or-restart` only (LSA_PERF alias in the tmpl).
        proc = subprocess.run(
            ["sudo", "-n", "systemctl", "reload-or-restart", mode],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return {"ok": False,
                    "error": (proc.stderr or proc.stdout or "").strip()[:300] or f"rc={proc.returncode}"}
        return {"ok": True, "mode": mode}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Live benchmark (speed-bench, #879) ────────────────────────────────────

def _live_power_w() -> "tuple[Optional[float], Optional[str]]":
    """(watts, source): PSU wall power from liquidctl, else GPU power."""
    with best_effort("live power: liquidctl psu", log=log):
        from collectors.liquidctl import get_liquidctl_cached  # type: ignore
        psu = (get_liquidctl_cached() or {}).get("psu") or {}
        est = psu.get("Estimated input power")
        if isinstance(est, dict) and isinstance(est.get("value"), (int, float)):
            return float(est["value"]), "psu"
    with best_effort("live power: gpu", log=log):
        gpu = collect_gpu() or {}
        v = gpu.get("power_watts")
        if isinstance(v, (int, float)):
            return float(v), "gpu"
    return None, None


def _bench_live_root() -> Path:
    """Root holding the vendored bench assets: the configured install dir."""
    inst = (getattr(_require_ctx().config, "AGENT_INSTALL_DIR", "") or "").strip()
    return Path(inst) if inst else Path(__file__).resolve().parents[1]


def _bench_live_server() -> dict:
    """Running-server snapshot for the live bench: models, loaded id, slots, spec settings."""
    base = (_require_ctx().config.LLAMA_API_URL or "").rstrip("/")
    out: dict = {"up": False, "url": base, "models": [], "loaded_id": None,
                 "slots_idle": 0, "slots_total": 0, "spec": None}
    if not base:
        return out
    try:
        resp = requests.get(f"{base}/v1/models", timeout=2)
    except Exception:
        return out
    if not resp.ok:
        return out
    out["up"] = True
    for m in (resp.json() or {}).get("data", []) or []:
        st = m.get("status", {})
        sv = st.get("value") if isinstance(st, dict) else None
        out["models"].append({"id": m.get("id"), "status": sv or "unknown"})
        if sv in ("loaded", "sleeping") and not out["loaded_id"]:
            out["loaded_id"] = m.get("id")
    with best_effort("live bench: /slots probe", log=log):
        slots = requests.get(f"{base}/slots", timeout=2).json()
        if isinstance(slots, list):
            out["slots_total"] = len(slots)
            out["slots_idle"] = sum(1 for s in slots if not s.get("is_processing"))
    with best_effort("live bench: /props probe", log=log):
        props = requests.get(f"{base}/props", timeout=2).json() or {}
        gs = props.get("default_generation_settings") or {}
        spec = gs.get("speculative") or {}
        if isinstance(spec, dict) and spec:
            out["spec"] = {"n_min": spec.get("n_min"), "n_max": spec.get("n_max"), "p_min": spec.get("p_min")}
    return out


def _bench_live_runtime() -> dict:
    cfg = _require_ctx().config
    py, src = _bl.runtime_python(cfg.AGENT_INSTALL_DIR, getattr(cfg, "SPEED_BENCH_PYTHON", "") or "")
    script, status = _bl.script_path(cfg.AGENT_INSTALL_DIR, _bench_live_root())
    return {"python": py, "source": src, "script": str(script) if script else None,
            "script_status": status, "commit": _bl.SCRIPT_COMMIT}


def llama_bench_live_preflight(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    cfg = _require_ctx().config
    return {"ok": True, "server": _bench_live_server(), "runtime": _bench_live_runtime(),
            "datasets": _bl.read_marker(cfg.AGENT_INSTALL_DIR), "benches": list(_bl.BENCHES),
            "busy": bool(_bench_active or _autotune_active)}


def _bench_live_setup_job(benches: list) -> None:
    """venv + pip install + optional dataset prefetch, streamed on the bench replay."""
    global _bench_active, _bench_proc, _bench_pgid
    cfg = _require_ctx().config
    root = _bl.bench_dir(cfg.AGENT_INSTALL_DIR)
    ok = True
    try:
        root.mkdir(parents=True, exist_ok=True)
        script, status = _bl.script_path(cfg.AGENT_INSTALL_DIR, _bench_live_root())
        if script is None:
            _bench_put({"type": "setup_step", "step": "script", "text": f"downloading speed_bench.py @ {_bl.SCRIPT_COMMIT[:12]}"})
            resp = requests.get(_bl.SCRIPT_URL, timeout=30)
            resp.raise_for_status()
            body = resp.content
            if len(body) > 512 * 1024:
                raise RuntimeError("downloaded speed_bench.py is unexpectedly large")
            import hashlib
            if hashlib.sha256(body).hexdigest() != _bl.SCRIPT_SHA256:
                raise RuntimeError("downloaded speed_bench.py does not match the pinned hash")
            (root / "speed_bench.py").write_bytes(body)
        py, src = _bl.runtime_python(cfg.AGENT_INSTALL_DIR, getattr(cfg, "SPEED_BENCH_PYTHON", "") or "")
        steps: list = []
        if py is None:
            steps.append(("venv", ["python3", "-m", "venv", str(root / "venv")]))
            py = str(root / "venv" / "bin" / "python")
        req = _bench_live_root() / "bench" / "requirements-bench.txt"
        pkgs = ["-r", str(req)] if req.is_file() else list(_bl.REQUIREMENTS)
        steps.append(("pip", [py, "-m", "pip", "install", "--quiet", "--no-input", *pkgs]))
        for b in benches:
            steps.append((f"prefetch:{b}", [py, "-c",
                          "import sys; from datasets import load_dataset; "
                          f"d = load_dataset('nvidia/SPEED-Bench', name={b!r}, split='test'); "
                          "print('categories:' + ','.join(sorted(set(str(c) for c in d['category']))))"]))
        for name, cmd in steps:
            if _bench_cancel_event.is_set():
                ok = False
                _bench_put({"type": "setup_done", "ok": False, "error": "cancelled"})
                break
            _bench_put({"type": "setup_step", "step": name, "text": " ".join(cmd[:4])})
            env = dict(os.environ, HF_HUB_DISABLE_PROGRESS_BARS="1", PYTHONUNBUFFERED="1")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                    close_fds=True, env=env, start_new_session=True)
            _bench_proc = proc
            try: _bench_pgid = os.getpgid(proc.pid)
            except Exception: _bench_pgid = None
            for raw in iter(proc.stdout.readline, ""):
                txt = _BENCH_ANSI_RE.sub("", raw.rstrip("\n"))
                if txt.startswith("categories:"):
                    _bl.write_marker(cfg.AGENT_INSTALL_DIR, name.split(":", 1)[1],
                                     [c for c in txt[len("categories:"):].split(",") if c])
                elif txt:
                    _bench_put({"type": "line", "model_id": "", "text": txt})
            proc.wait()
            _bench_proc = None
            if proc.returncode != 0:
                ok = False
                _bench_put({"type": "setup_done", "ok": False, "error": f"{name} failed (rc={proc.returncode})"})
                break
        if ok:
            _bench_put({"type": "setup_done", "ok": True, "runtime": _bench_live_runtime()})
    except Exception as e:
        log.error("bench live setup error: %s", e, exc_info=True)
        _bench_put({"type": "setup_done", "ok": False, "error": str(e)})
    finally:
        _bench_proc = None
        with _bench_lock:
            _bench_active = False


def llama_bench_live_setup(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    global _bench_active
    benches = [b for b in (body.get("prefetch") or []) if b in _bl.BENCHES]
    with _bench_lock:
        if _bench_active or _autotune_active:
            return {"ok": False, "error": "Another benchmark or autotune is in progress"}
        _bench_active = True
        with _bench_cond:
            _bench_replay.start_run(uuid.uuid4().hex[:12])
    _bench_cancel_event.clear()
    threading.Thread(target=_bench_live_setup_job, args=(benches,), daemon=True).start()
    return {"ok": True, "run_id": _bench_replay.run_id}


def _bench_live_store(doc: dict) -> None:
    """POST the full run to the manager's live-bench history; best effort."""
    ctx = _require_ctx()
    base = (getattr(ctx.config, "MANAGER_URL", "") or "").rstrip("/")
    token = (getattr(ctx, "state", None) or {}).get("token") or ""
    if not base or not token:
        return
    try:
        ctx.post_session.post(f"{base}/api/benchmark/live/store", json=doc, timeout=15,
                              headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        log.debug("live bench store failed: %s", e)


def _bench_live_run_all(req: dict, server: dict, python: str, script: str) -> None:
    global _bench_active, _bench_proc, _bench_pgid
    cfg = _require_ctx().config
    run_id = _bench_replay.run_id
    run_dir = _bl.bench_dir(cfg.AGENT_INSTALL_DIR) / "runs" / run_id
    model_id = req["model_id"]
    levels: list = []
    energy = _bl.PowerIntegrator(_live_power_w)
    ok = True
    started = time.time()
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONUNBUFFERED="1", HF_HUB_DISABLE_PROGRESS_BARS="1")
        _bench_put({"type": "model_start", "model_id": model_id, "run_id": run_id,
                    "bench": req["bench"], "levels": req["concurrency"], "matrix": req.get("matrix"),
                    "cmd": " ".join(_bl.build_cmd(python, script, server["url"], req, req["concurrency"][0], "<run>/level-N.json"))})
        energy.start()

        def _track(p):
            global _bench_proc, _bench_pgid
            _bench_proc = p
            try: _bench_pgid = os.getpgid(p.pid)
            except Exception: _bench_pgid = None

        def _untrack():
            global _bench_proc, _bench_pgid
            _bench_proc = None
            _bench_pgid = None

        stop = False
        for cell in req["cells"]:
            creq = {**req, "bench": cell["bench"], "osl": cell["osl"]}
            for level in req["concurrency"]:
                if _bench_cancel_event.is_set():
                    ok = False; stop = True
                    break
                out_path = run_dir / f"level-{cell['bench']}-{cell['osl']}-{level}.json"
                _bench_put({"type": "level_start", "model_id": model_id, "level": level,
                            "concurrency": level, "samples": None,
                            "bench": cell["bench"], "osl": cell["osl"]})
                t0 = time.monotonic()
                rc, cancelled, elapsed = _bl.run_level_subprocess(
                    _bl.build_cmd(python, script, server["url"], creq, level, str(out_path)),
                    env, _bench_put, model_id, level, _bench_cancel_event, _track, _untrack)
                # The script's own timer excludes dataset load; agent wall is the fallback.
                wall = elapsed if elapsed is not None else (time.monotonic() - t0)
                if cancelled:
                    ok = False; stop = True
                    break
                try:
                    payload = json.loads(out_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    ok = False; stop = True
                    _bench_put({"type": "line", "model_id": model_id, "text": f"level {level}: no output (rc={rc})"})
                    break
                summ = _bl.level_summary(payload, wall)
                row = {"level": level, "concurrency": level, "bench": cell["bench"], "osl": cell["osl"],
                       "wall_s": round(wall, 3), "rc": rc, **summ}
                levels.append(row)
                if req["categories"] == "all":
                    _bl.write_marker(cfg.AGENT_INSTALL_DIR, cell["bench"], [r.get("category") for r in summ["rows"] if r.get("category")])
                _bench_put({"type": "level_result", "model_id": model_id, **row})
                if rc not in (0, 1):
                    ok = False; stop = True
                    break
            if stop:
                break
    except Exception as e:
        log.error("live bench error: %s", e, exc_info=True)
        ok = False
        _bench_put({"type": "line", "model_id": model_id, "text": f"error: {e}"})
    finally:
        wh, src = energy.stop()
        tokens = sum(int((lv.get("all") or {}).get("completion_tokens") or 0) for lv in levels)
        wh_per_ktok = (wh / (tokens / 1000.0)) if wh is not None and tokens > 0 else None
        cancelled = _bench_cancel_event.is_set()
        first = (levels[0]["all"] if levels else {})
        doc = {"type": "model_done", "model_id": model_id, "run_id": run_id, "ok": ok and not cancelled,
               "cancelled": cancelled, "bench": req["bench"], "config": req, "levels": levels,
               "energy_wh": wh, "energy_source": src, "wh_per_ktok": wh_per_ktok,
               "spec": server.get("spec"), "server_url": server.get("url"),
               "elapsed_s": round(time.time() - started, 1), "baseline_run_id": req.get("baseline_run_id")}
        _bench_put(doc)
        if levels:
            _bench_live_store(doc)
            _shared.post_tool_run(_require_ctx(), "benchmark", "llama", run_id, model_id, ok and not cancelled,
                                  {"bench_tool": "speed-bench", "gen_tps": first.get("pred_tps"),
                                   "ppt_tps": first.get("prompt_tps"), "latency_s": first.get("latency_s"),
                                   "accept_rate": first.get("accept_rate"), "levels": len(levels),
                                   "wh_per_ktok": wh_per_ktok, "bench": req["bench"]})
        _bench_put({"type": "done", "ok": ok and not cancelled, "cancelled": cancelled, "count": 1})
        _bench_proc = None
        _bench_pgid = None
        with _bench_lock:
            _bench_active = False


def llama_bench_live_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    global _bench_active
    try:
        req = _bl.validate_run_request(body or {})
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Probe outside the lock: the HTTP calls would hold it for seconds.
    if _bench_active or _autotune_active:
        return {"ok": False, "error": "Another benchmark or autotune is in progress"}
    server = _bench_live_server()
    if not server["up"]:
        return {"ok": False, "error": "llama-server is not running"}
    if req["model_id"] not in [m.get("id") for m in server["models"]]:
        return {"ok": False, "error": f"server does not list {req['model_id']}"}
    rt = _bench_live_runtime()
    if not rt["python"] or not rt["script"]:
        return {"ok": False, "error": "speed-bench runtime not installed", "runtime": rt}
    with _bench_lock:
        if _bench_active or _autotune_active:
            return {"ok": False, "error": "Another benchmark or autotune is in progress"}
        _bench_active = True
        with _bench_cond:
            _bench_replay.start_run(uuid.uuid4().hex[:12])
    _bench_cancel_event.clear()
    threading.Thread(target=_bench_live_run_all, args=(req, server, rt["python"], rt["script"]), daemon=True).start()
    return {"ok": True, "run_id": _bench_replay.run_id}


def _autotune_port() -> int:
    """Port the tuning spawns bind on: the one in LLAMA_API_URL, else 8080."""
    try:
        return int(urlparse(_require_ctx().config.LLAMA_API_URL or "").port or 8080)
    except ValueError:
        return 8080


def _llama_unit_active() -> bool:
    """True when the llama-server systemd unit reports active."""
    active = False
    with best_effort("autotune: probe llama unit is-active", log=log):
        st = subprocess.run(["systemctl", "is-active", _require_ctx().config.LLAMA_SYSTEMD_UNIT],
                            capture_output=True, text=True, timeout=5)
        active = (st.stdout or "").strip() == "active"
    return active


def _autotune_kl_text() -> Path:
    return _bench_live_root() / "bench" / "kl_text.txt"


def _autotune_perplexity_bin() -> "Optional[Path]":
    """llama-perplexity beside the configured llama-server binary, else None."""
    b = _require_ctx().config.LLAMA_BIN
    if not b:
        return None
    p = Path(b).parent / "llama-perplexity"
    return p if p.exists() else None


_autotune_ppl_probe_cache: dict[str, dict] = {}
_AUTOTUNE_PROBE_TIMEOUT_S = 5


def _autotune_probe_arg(ppl: str, arg: str, env: dict) -> dict:
    """Runs one flag against the binary; {"rc": None, "timeout": True} on a hang, else the rc."""
    try:
        r = subprocess.run([ppl, arg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=_AUTOTUNE_PROBE_TIMEOUT_S, env=env)
        return {"rc": r.returncode, "timeout": False}
    except subprocess.TimeoutExpired:
        return {"rc": None, "timeout": True}
    except OSError:
        return {"rc": None, "timeout": False}


def _autotune_probe_perplexity(ppl: Path) -> dict:
    """Runs llama-perplexity --version (falling back to --help) with the tuning LD_LIBRARY_PATH."""
    try:
        st = ppl.stat()
        key = f"{ppl}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return {"runnable": None, "rc": None, "reason": None}
    cached = _autotune_ppl_probe_cache.get(key)
    if cached is not None:
        return cached
    env = os.environ.copy()
    parent = str(ppl.parent)
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{parent}:{existing}" if existing else parent
    res = _autotune_probe_arg(str(ppl), "--version", env)
    # A hang will hang again on --help too; only retry a clean-but-unrecognised exit.
    if not res["timeout"] and res["rc"] is not None and res["rc"] > 1:
        res = _autotune_probe_arg(str(ppl), "--help", env)
    if res["timeout"]:
        out = {"runnable": False, "rc": None, "reason": "timeout"}
    elif res["rc"] is None:
        out = {"runnable": False, "rc": None, "reason": "exec_failed"}
    elif res["rc"] < 0:
        out = {"runnable": False, "rc": res["rc"], "reason": "signal"}
    else:
        out = {"runnable": True, "rc": res["rc"], "reason": None}
    _autotune_ppl_probe_cache[key] = out
    return out


def _autotune_perplexity_status() -> dict:
    """present/kl_text/runnable for the quality guard; runnable is only probed once both exist."""
    ppl = _autotune_perplexity_bin()
    text = _autotune_kl_text()
    present, kl_text = ppl is not None, text.is_file()
    runnable, rc, hint = None, None, None
    if not present:
        hint = "llama-perplexity is not installed beside llama-server."
    elif not kl_text:
        hint = "the KL reference text is missing from the agent's bench directory."
    else:
        probe = _autotune_probe_perplexity(ppl)
        runnable, rc = probe["runnable"], probe["rc"]
        if runnable is False:
            reason = probe.get("reason")
            if reason == "signal":
                hint = (f"llama-perplexity crashed on startup (signal {-rc}) — it looks stale relative to "
                        "the installed llama.cpp libraries; reinstall the llama.cpp tools from the same build.")
            elif reason == "timeout":
                hint = "llama-perplexity did not respond to a version check — it may be hung or unusable."
            else:
                hint = "llama-perplexity could not be executed — check that it is installed and executable."
    ok = present and kl_text and runnable is not False
    return {"ok": ok, "present": present, "kl_text": kl_text, "runnable": runnable, "rc": rc, "hint": hint}


def _autotune_track_aux(p: "subprocess.Popen") -> None:
    global _autotune_aux_proc, _autotune_aux_pgid
    _autotune_aux_proc = p
    try: _autotune_aux_pgid = os.getpgid(p.pid)
    except Exception: _autotune_aux_pgid = None


def _autotune_untrack_aux() -> None:
    global _autotune_aux_proc, _autotune_aux_pgid
    _autotune_aux_proc = None
    _autotune_aux_pgid = None


def _read_cpuinfo() -> str:
    try:
        return Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _ram_total_mb() -> "Optional[int]":
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return None


def _autotune_put(msg: dict) -> None:
    """Append to the per-run replay buffer and wake any waiting streams."""
    with _autotune_cond:
        _autotune_replay.append(msg)
        _autotune_cond.notify_all()


def _autotune_build_optional_args(params: dict) -> list:
    """Translate optional-params dict into llama-server args; omits flags the user didn't enable."""
    out: list = []
    if not isinstance(params, dict):
        return out
    p = params
    if p.get("load_mode"):
        out += ["--load-mode", str(p["load_mode"])]
    if p.get("mlock"):
        out.append("--mlock")
    if p.get("no_mmap"):
        out.append("--no-mmap")
    if p.get("kv_unified"):
        out.append("--kv-unified")
    if p.get("parallel") not in (None, "", 0):
        out += ["--parallel", str(int(p["parallel"]))]
    # 0 is meaningful for cache_ram: disables the host RAM cache.
    if p.get("cache_ram") not in (None, ""):
        out += ["--cache-ram", str(int(p["cache_ram"]))]
    if p.get("b") not in (None, "", 0):
        out += ["-b", str(int(p["b"]))]
    if p.get("ub") not in (None, "", 0):
        out += ["-ub", str(int(p["ub"]))]
    if p.get("ngl") not in (None, ""):
        out += ["-ngl", str(int(p["ngl"]))]
    if p.get("ctk"):
        out += ["-ctk", str(p["ctk"]).strip()]
    if p.get("ctv"):
        out += ["-ctv", str(p["ctv"]).strip()]
    custom = p.get("custom_args")
    if isinstance(custom, list):
        for tok in custom:
            if isinstance(tok, str) and tok.strip():
                out.append(tok.strip())
    return out


def _autotune_parse_shutdown_mem(lines: list) -> dict:
    """Parse the LAST GPU row from common_memory_breakdown_print (the shutdown one is authoritative)."""
    raw_total = None
    raw_free = None
    for line in lines:
        if "common_memory_breakdown_print" not in line:
            continue
        m = _AT_MEM_RE.search(line)
        if not m or not _AT_GPU_HINT_RE.search(m.group("label")):
            continue
        try:
            raw_total = int(m.group("total"))
            raw_free  = int(m.group("free"))
        except Exception:
            continue
    if raw_total is None or raw_free is None:
        return {"ok": False, "total_mb": None, "free_mb": None,
                "raw_total": raw_total, "raw_free": raw_free,
                "reason": "no GPU breakdown row found"}
    sane = (0 < raw_total <= _AT_MEM_TOTAL_SANE_MAX and 0 <= raw_free <= raw_total)
    return {"ok": sane, "total_mb": raw_total if sane else None,
            "free_mb": raw_free if sane else None,
            "raw_total": raw_total, "raw_free": raw_free,
            "reason": None if sane else "values out of bounds"}


def _autotune_run_iter(model_id: str, fitt_mb: "Optional[int]", extra_args: list, env: dict, iter_idx: int,
                       *, ctx: "Optional[int]" = None, hold=None) -> dict:
    """Run llama-server once (-fitt, an explicit ctx with fit off, or neither), wait for
    model-loaded, optionally call hold() while it is up, SIGTERM, parse output."""
    global _autotune_proc, _autotune_pgid
    if _autotune_cancel_event.is_set():
        return {"ok": False, "error": "cancelled"}
    if not _require_ctx().config.LLAMA_BIN:
        return {"ok": False, "error": "LLAMA_BIN not configured"}
    bin_path = _require_ctx().config.LLAMA_BIN
    if not Path(bin_path).exists():
        return {"ok": False, "error": f"llama-server not found at {bin_path}"}
    hf_arg = _bench_get_hf_arg(model_id)
    if not hf_arg:
        return {"ok": False, "error": f"no HF reference found for {model_id}"}

    cmd = _at.spawn_cmd(bin_path, hf_arg, _autotune_port(), fitt_mb, ctx, extra_args)

    _autotune_put({
        "type": "iter_start", "model_id": model_id, "iter": iter_idx,
        "fitt": fitt_mb, "ctx": ctx, "cmd": " ".join(str(c) for c in cmd),
    })

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, bufsize=1,
        # llama-server occasionally emits non-UTF-8; errors='replace' keeps the loop alive.
        encoding="utf-8", errors="replace",
        close_fds=True, env=env, start_new_session=True,
    )
    _autotune_proc = proc
    try: _autotune_pgid = os.getpgid(proc.pid)
    except Exception: _autotune_pgid = None

    model_loaded = False
    ctx_seq: Optional[int] = None
    # Used only when computed ctx == requested ctx (parenthesized form omitted).
    ctx_fallback: Optional[int] = None
    # True=fit reduced ctx, False="no changes needed", None=neither line seen.
    # Drives plateau detection in the picker.
    fit_applied: Optional[bool] = None
    facts_lines: list = []
    oom = False
    loaded_at: Optional[float] = None
    shutdown_buf: deque = deque(maxlen=4000)
    terminating = threading.Event()
    start_ts = time.time()
    LOAD_TIMEOUT = 300
    last_progress = start_ts

    _hb_stop = threading.Event()
    def _hb_loop():
        while not _hb_stop.wait(2.0):
            elapsed = time.time() - start_ts
            _autotune_put({
                "type": "loading_progress", "model_id": model_id,
                "iter": iter_idx, "fitt": fitt_mb,
                "elapsed_s": int(elapsed), "timeout_s": int(LOAD_TIMEOUT),
            })
    _hb_thread = threading.Thread(target=_hb_loop, daemon=True)
    _hb_thread.start()

    # Authoritative free-VRAM comes from the SHUTDOWN breakdown, not startup.
    try:
        for raw in iter(proc.stdout.readline, ""):
            if _autotune_cancel_event.is_set():
                break
            if not raw:
                break
            line = _BENCH_ANSI_RE.sub("", raw.rstrip("\n"))
            if line:
                _autotune_put({"type": "line", "model_id": model_id, "text": line})
                last_progress = time.time()
                # Take the LAST parenthesized n_ctx_seq value — earlier lines
                # echo the requested default before -fitt has adjusted it.
                mm = _AT_NCTX_RE.search(line)
                if mm:
                    try: ctx_seq = int(mm.group(1))
                    except ValueError: pass  # unparseable counter, keep the prior value
                else:
                    mm2 = _AT_NCTX_FALLBACK_RE.search(line)
                    if mm2:
                        try: ctx_fallback = int(mm2.group(1))
                        except ValueError: pass  # unparseable counter, keep the prior value
                # Detect whether llama-server's auto-fit actually trimmed
                # the ctx. Later occurrences overwrite earlier ones — the
                # final fit decision is what we want.
                if "common_params_fit_impl" in line:
                    if "no changes needed" in line:
                        fit_applied = False
                    elif "context size reduced from" in line:
                        fit_applied = True
                if "print_info:" in line:
                    facts_lines.append(line)
                if _AT_OOM_RE.search(line):
                    oom = True
            if _AT_MODEL_LOADED_RE.search(line):
                model_loaded = True
                loaded_at = time.time()
                break
            if time.time() - last_progress > LOAD_TIMEOUT:
                _autotune_put({"type": "line", "model_id": model_id,
                               "text": f"[autotune] no progress for {LOAD_TIMEOUT}s — aborting iteration"})
                break
    except Exception as e:
        _autotune_put({"type": "line", "model_id": model_id,
                       "text": f"[autotune] read error: {e}"})
    finally:
        _hb_stop.set()

    # Reader-thread handoff: only the thread touches these, the main thread reads after the join.
    drain_ctx: dict = {}

    def _drain_stdout() -> None:
        with best_effort("autotune: drain shutdown stdout", log=log):
            for raw in iter(proc.stdout.readline, ""):
                if not raw:
                    break
                line = _BENCH_ANSI_RE.sub("", raw.rstrip("\n"))
                if not line:
                    continue
                shutdown_buf.append(line)
                # Lines logged while the stick runs would evict stage events from the replay.
                if terminating.is_set():
                    _autotune_put({"type": "line", "model_id": model_id, "text": line})
                mm = _AT_NCTX_RE.search(line)
                if mm:
                    try: drain_ctx["seq"] = int(mm.group(1))
                    except ValueError: pass  # unparseable counter, keep the prior value
                else:
                    mm2 = _AT_NCTX_FALLBACK_RE.search(line)
                    if mm2:
                        try: drain_ctx["fallback"] = int(mm2.group(1))
                        except ValueError: pass  # unparseable counter, keep the prior value

    # A held server keeps logging: drain stdout for the whole hold.
    reader = None
    if model_loaded and hold is not None:
        reader = threading.Thread(target=_drain_stdout, daemon=True)
        reader.start()

    # Measurement runs against the live server before it is torn down.
    hold_res = None
    if model_loaded and hold is not None and not _autotune_cancel_event.is_set():
        try:
            hold_res = hold()
        except Exception as e:
            log.warning("autotune hold failed: %s", e)
            hold_res = {"ok": False, "error": str(e)[:200]}

    # SIGTERM triggers llama-server's clean shutdown + post-load memory breakdown.
    terminating.set()
    pgid = _autotune_pgid
    if pgid is not None:
        # tolerate the group already being gone
        try: os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError: pass
        except Exception as e:
            _autotune_put({"type": "line", "model_id": model_id,
                           "text": f"[autotune] SIGTERM error: {e}"})

    # Last GPU breakdown row found during shutdown is authoritative for free/total.
    # Force-kill the group after a 30s deadline.
    _drain_watchdog = None
    if pgid is not None:
        def _drain_kill(_pgid: int = pgid) -> None:
            try:
                os.killpg(_pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # group already exited
            except Exception as e:
                log.warning("autotune drain watchdog: killpg failed: %s", e)
        _drain_watchdog = threading.Timer(30.0, _drain_kill)
        _drain_watchdog.daemon = True
        _drain_watchdog.start()
    if reader is not None:
        reader.join(timeout=40)
        if reader.is_alive():
            log.warning("autotune iter: stdout reader still alive after 40s")
    else:
        _drain_stdout()
    if _drain_watchdog is not None:
        _drain_watchdog.cancel()
    if "seq" in drain_ctx:
        ctx_seq = drain_ctx["seq"]
    if "fallback" in drain_ctx:
        ctx_fallback = drain_ctx["fallback"]

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if pgid is not None:
            try:
                # force-kill; tolerate the group already being gone
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception as e:
                log.warning("autotune iter: killpg SIGKILL failed: %s", e)
        with best_effort("autotune iter: reap proc", log=log):
            proc.wait(timeout=5)

    _autotune_proc = None
    _autotune_pgid = None

    # Bare `n_ctx = N` is authoritative when computed ctx == requested.
    if ctx_seq is None and ctx_fallback is not None:
        ctx_seq = ctx_fallback

    meta = {"model_loaded": model_loaded, "oom": oom, "facts_lines": facts_lines, "hold": hold_res,
            "load_s": (loaded_at - start_ts) if loaded_at else None}

    if not model_loaded:
        return {**meta, "ok": False,
                "error": "OOM at load" if oom else "model never reached 'main: model loaded'",
                "ctx_seq": ctx_seq, "actual_free_mb": None, "total_vram_mb": None}

    mem = _autotune_parse_shutdown_mem(shutdown_buf)
    if mem["ok"]:
        if ctx_seq is None:
            return {**meta, "ok": False, "error": "got memory breakdown but no n_ctx_seq",
                    "ctx_seq": None,
                    "actual_free_mb": mem["free_mb"], "total_vram_mb": mem["total_mb"],
                    "fit_applied": fit_applied}
        return {**meta, "ok": True, "ctx_seq": ctx_seq,
                "actual_free_mb": mem["free_mb"], "total_vram_mb": mem["total_mb"],
                "fit_applied": fit_applied}
    # Sentinel/missing — return raw numbers so the caller can retry with doubled -fitt.
    return {**meta, "ok": False, "sentinel": True,
            "raw_free_mb": mem.get("raw_free"),
            "raw_total_mb": mem.get("raw_total"),
            "reason": mem.get("reason"),
            "ctx_seq": ctx_seq, "actual_free_mb": None, "total_vram_mb": None}


def _autotune_converge(model_id: str, target_mb: int, extra_args: list, env: dict,
                       tolerance_mb: int = 50, start_fitt: "Optional[int]" = None,
                       max_iters: int = 10) -> dict:
    """Converge actual_free_mb on target_mb (±tolerance_mb) with -fitt; returns the best sample."""
    TOL = max(1, int(tolerance_mb))
    MAX_ITERS = max(1, int(max_iters))
    MAX_STEP = 1024
    MIN_STEP = 128
    # Must be < TOL so refinement can hit the requested precision.
    DEDUP_MB = max(5, min(25, TOL // 2 if TOL > 1 else 5))

    fitt = max(0, int(start_fitt if start_fitt is not None else target_mb))
    facts: dict = {}
    load_s: "Optional[float]" = None
    history: list = []
    tried: dict = {}
    plateau_ceiling = -1
    stop_reason: Optional[str] = None
    # Buckets that returned bogus shutdown memory; remembered across iters.
    sentinel_seen: set = set()
    best: Optional[dict] = None
    converged = False
    iters_done = 0

    def _key(f: int) -> int:
        return int(round(f / DEDUP_MB)) * DEDUP_MB

    def _update_best(rec: dict) -> None:
        """Closest absolute-error wins; ties prefer overshoot (target is a floor)."""
        nonlocal best
        if best is None:
            best = rec
            return
        rec_diff = rec["actual_free_mb"] - target_mb
        best_diff = best["actual_free_mb"] - target_mb
        rec_abs = abs(rec_diff)
        best_abs = abs(best_diff)
        if rec_abs < best_abs:
            best = rec
            return
        if rec_abs > best_abs:
            return
        if rec_diff >= 0 and best_diff < 0:
            best = rec

    def _largest_unexplored_gap(lo_floor: int, hi_ceil: int) -> Optional[int]:
        """Midpoint of the largest gap between obstacle buckets; None if all gaps < DEDUP_MB."""
        obstacles = set(tried.keys()) | sentinel_seen
        keys = sorted({k for k in obstacles
                       if lo_floor < k <= hi_ceil} | {lo_floor, hi_ceil})
        best_gap = 0
        best_mid: Optional[int] = None
        for a, b in zip(keys, keys[1:]):
            gap = b - a
            if gap > best_gap and gap > DEDUP_MB:
                best_gap = gap
                best_mid = (a + b) // 2
        return best_mid

    def _pick_next(prev: dict) -> Optional[int]:
        """Propose the next fitt; None when stuck (sets enclosing `stop_reason`/`converged`)."""
        nonlocal stop_reason, converged
        below = [h for h in history if h["actual_free_mb"] < target_mb]
        at_or_above = [h for h in history if h["actual_free_mb"] >= target_mb]
        lo = max((h["fitt"] for h in below), default=None)
        hi = min((h["fitt"] for h in at_or_above), default=None)

        # MAX_STEP cap is skipped inside a known bracket; bracket already bounds the candidate.
        bracketed_branch = False
        if lo is not None and hi is not None and hi > lo:
            bracketed_branch = True
            if hi - lo <= DEDUP_MB:
                if best is not None and abs(best["actual_free_mb"] - target_mb) <= TOL:
                    converged_flag = True
                    stop_reason = ("converged_at_precision_limit: best sample "
                                   "{f} MB free is within tolerance {t} MB").format(
                                       f=best["actual_free_mb"], t=TOL)
                else:
                    converged_flag = False
                    best_diff = (best["actual_free_mb"] - target_mb) if best else None
                    stop_reason = ("bracket_precision: bracket [{lo}, {hi}] "
                                   "narrowed below {d} MB — best sample is "
                                   "{f} MB free ({sign}{diff} MB vs target), "
                                   "outside TOL {t} MB; no smaller refinement "
                                   "possible").format(
                                       lo=lo, hi=hi, d=DEDUP_MB,
                                       f=(best or {}).get("actual_free_mb", "—"),
                                       sign=("+" if (best_diff or 0) > 0 else ""),
                                       diff=best_diff, t=TOL)
                _autotune_put({
                    "type": "bracket_precision_reached", "model_id": model_id,
                    "bracket_lo": lo, "bracket_hi": hi, "dedup_mb": DEDUP_MB,
                    "tol_mb": TOL, "converged": converged_flag,
                    "reason": stop_reason,
                })
                converged = converged_flag
                return None
            # Regula falsi between bracket samples; fall back to midpoint if degenerate.
            lo_rec = max((h for h in history if h["actual_free_mb"] < target_mb),
                         key=lambda h: h["fitt"])
            hi_rec = min((h for h in history if h["actual_free_mb"] >= target_mb),
                         key=lambda h: h["fitt"])
            f1, a1 = lo_rec["fitt"], lo_rec["actual_free_mb"]
            f2, a2 = hi_rec["fitt"], hi_rec["actual_free_mb"]
            cand = (lo + hi) // 2
            if a2 > a1 and (f2 - f1) > 0:
                interp = f1 + int(round((target_mb - a1) * (f2 - f1) / (a2 - a1)))
                if lo < interp < hi:
                    cand = interp
        elif hi is None:
            # Detect a non-monotonic peak before pushing fitt up further.
            if len(history) >= 2:
                peak = max(history, key=lambda h: h["actual_free_mb"])
                higher = [h for h in history if h["fitt"] > peak["fitt"]]
                if higher and all(h["actual_free_mb"] < peak["actual_free_mb"] for h in higher):
                    stop_reason = ("non_monotonic_peak: free declines past "
                                   "-fitt={f} MB (peak free={p} MB); target "
                                   "{t} MB is above the achievable peak on "
                                   "this model/hardware").format(
                                       f=peak["fitt"],
                                       p=peak["actual_free_mb"],
                                       t=target_mb)
                    _autotune_put({
                        "type": "non_monotonic_detected", "model_id": model_id,
                        "peak_fitt": peak["fitt"],
                        "peak_free_mb": peak["actual_free_mb"],
                        "target_mb": target_mb,
                        "reason": stop_reason,
                    })
                    return None
            top = max((h["fitt"] for h in history), default=prev["fitt"])
            cand = top + MAX_STEP
        else:
            # All samples ≥ target; pull fitt down, but stay above the plateau.
            bot = min((h["fitt"] for h in history), default=prev["fitt"])
            cand = max(plateau_ceiling + MIN_STEP, bot - MAX_STEP)

        if cand <= plateau_ceiling:
            cand = plateau_ceiling + max(MIN_STEP, DEDUP_MB)

        if not bracketed_branch:
            delta = cand - prev["fitt"]
            if delta >  MAX_STEP: cand = prev["fitt"] + MAX_STEP
            if delta < -MAX_STEP: cand = prev["fitt"] - MAX_STEP
        cand = max(0, cand)

        if _key(cand) in tried or _key(cand) in sentinel_seen:
            obstacles_top = max((set(tried.keys()) | sentinel_seen)) if (tried or sentinel_seen) else 0
            escape = _largest_unexplored_gap(plateau_ceiling, obstacles_top + MAX_STEP)
            if escape is None or _key(escape) in tried or _key(escape) in sentinel_seen:
                stop_reason = ("cycle: every fitt in the safe search range "
                               "has already been sampled (every gap is "
                               "smaller than the {d} MB dedup threshold)").format(d=DEDUP_MB)
                _autotune_put({
                    "type": "cycle_detected", "model_id": model_id,
                    "proposed_fitt": cand, "tried_fitt": sorted(tried.keys()),
                    "reason": stop_reason,
                })
                return None
            _autotune_put({
                "type": "cycle_detected", "model_id": model_id,
                "proposed_fitt": cand, "escape_to": escape,
                "tried_fitt": sorted(tried.keys()),
            })
            cand = escape

        return cand

    MAX_SENTINEL_RETRIES = 6
    SANE_FITT_MAX        = 200_000

    for i in range(1, MAX_ITERS + 1):
        if _autotune_cancel_event.is_set():
            break
        iters_done = i

        # Sentinel-recovery sub-loop: double -fitt while shutdown values are bogus.
        # Save the picker's proposal before the retry loop mutates `fitt`.
        requested_fitt = fitt
        sentinel_attempts = 0
        res = None
        while True:
            res = _autotune_run_iter(model_id, fitt, extra_args, env, i)
            if res.get("facts_lines") and not facts.get("n_layer"):
                facts = _at.parse_facts(res["facts_lines"])
            if load_s is None and res.get("load_s"):
                load_s = res["load_s"]
            if res.get("ok") or not res.get("sentinel"):
                break
            # A cancelled run yields a bogus breakdown; never respawn for it.
            if _autotune_cancel_event.is_set():
                res = {"ok": False}
                break
            raw_free = res.get("raw_free_mb")
            raw_total = res.get("raw_total_mb")
            if sentinel_attempts >= MAX_SENTINEL_RETRIES:
                _autotune_put({"type": "iter_failed", "model_id": model_id,
                               "iter": i, "fitt": fitt,
                               "error": f"shutdown memory breakdown stayed out-of-bounds "
                                        f"after {sentinel_attempts} retries (last raw free={raw_free})"})
                res = {"ok": False}
                break
            new_fitt = fitt * 2 if fitt > 0 else 1024
            if new_fitt > SANE_FITT_MAX:
                new_fitt = SANE_FITT_MAX
            if new_fitt == fitt:
                _autotune_put({"type": "iter_failed", "model_id": model_id,
                               "iter": i, "fitt": fitt,
                               "error": "cannot raise -fitt further to escape sentinel"})
                res = {"ok": False}
                break
            sentinel_attempts += 1
            _autotune_put({
                "type": "sentinel_retry", "model_id": model_id, "iter": i,
                "attempt": sentinel_attempts, "max_attempts": MAX_SENTINEL_RETRIES,
                "old_fitt": fitt, "new_fitt": new_fitt,
                "raw_free_mb": raw_free, "raw_total_mb": raw_total,
                "reason": res.get("reason") or "out-of-bounds memory reading",
            })
            fitt = new_fitt

        if not res.get("ok"):
            break
        # If sentinel-retry had to double fitt to get a sane reading,
        # record every failed value (the picker's original proposal and
        # any intermediate doublings) as sentinel-prone buckets. The
        # picker will avoid these exact buckets on subsequent iters but
        # remains free to probe nearby values — sentinel is transient
        # and a value 50 MB away from a known-bad fitt usually succeeds.
        if sentinel_attempts > 0 and fitt > requested_fitt:
            failed_chain = []
            f = requested_fitt
            while f < fitt:
                key = _key(f)
                if key not in sentinel_seen:
                    sentinel_seen.add(key)
                    failed_chain.append(f)
                f *= 2
            _autotune_put({
                "type": "sentinel_seen_update", "model_id": model_id,
                "iter": i, "failed_chain": failed_chain,
                "safe_fitt": fitt, "sentinel_seen": sorted(sentinel_seen),
            })
        actual = int(res["actual_free_mb"])
        total = int(res["total_vram_mb"])
        ctx_seq = int(res["ctx_seq"])
        fit_applied_iter = res.get("fit_applied")  # True / False / None
        rec = {"fitt": fitt, "ctx_seq": ctx_seq,
               "actual_free_mb": actual, "total_vram_mb": total,
               "fit_applied": fit_applied_iter, "iter": i}
        _autotune_put({
            "type": "iter_result", "model_id": model_id, "iter": i,
            "fitt": fitt, "n_ctx_seq": ctx_seq,
            "actual_free_mb": actual, "total_vram_mb": total,
            "fit_applied": fit_applied_iter,
        })
        history.append(rec)
        tried[_key(fitt)] = rec
        if fit_applied_iter is False and fitt > plateau_ceiling:
            plateau_ceiling = fitt
            _autotune_put({"type": "plateau_detected", "model_id": model_id,
                           "iter": i, "fitt": fitt, "actual_free_mb": actual})
        _update_best(rec)
        diff = actual - target_mb
        if abs(diff) <= TOL:
            converged = True
            break

        new_fitt = _pick_next(rec)
        if new_fitt is None or new_fitt == fitt:
            if stop_reason is None:
                stop_reason = ("no further candidate to try (proposal "
                               "matched the current -fitt or the picker "
                               "exhausted its options)")
            break
        fitt = new_fitt
    else:
        if not converged and stop_reason is None:
            stop_reason = "iter_limit: exhausted {n} iterations without converging".format(n=MAX_ITERS)

    return {"ok": best is not None, "ctx": (best or {}).get("ctx_seq"), "free_mb": (best or {}).get("actual_free_mb"),
            "total_mb": (best or {}).get("total_vram_mb"), "fitt": (best or {}).get("fitt"),
            "converged": converged, "iters": iters_done, "stop_reason": stop_reason, "facts": facts, "load_s": load_s}


class _AutotuneBackend:
    """Real backend for the stage engine: llama-server loads, the speed-bench stick, llama-perplexity KL."""

    def __init__(self, model_id: str, env: dict, run_id: str):
        self.model_id, self.env, self.run_id = model_id, env, run_id
        self._energy: "Optional[_bl.PowerIntegrator]" = None
        self._n = 0

    def converge(self, args, target_mb, tolerance_mb, start_fitt, max_iters):
        return _autotune_converge(self.model_id, target_mb, list(args), self.env, tolerance_mb,
                                  start_fitt=start_fitt, max_iters=max_iters)

    def load(self, args, ctx, measure):
        hold = (lambda: self._stick(measure)) if measure else None
        res = _autotune_run_iter(self.model_id, None, list(args), self.env, 0, ctx=ctx, hold=hold)
        loaded, oom = bool(res.get("model_loaded")), bool(res.get("oom"))
        return {"ok": bool(res.get("ok")) or (loaded and not oom),
                "oom": oom,
                "error": "OOM after load" if (loaded and oom) else (None if loaded else res.get("error")),
                "ctx": res.get("ctx_seq"), "free_mb": res.get("actual_free_mb"), "total_mb": res.get("total_vram_mb"),
                "facts": _at.parse_facts(res.get("facts_lines") or []), "load_s": res.get("load_s"),
                "stick": res.get("hold") if measure else None}

    def _server_ready(self, url: str) -> "Optional[str]":
        """Polls /health then /v1/models; returns the model id the server reports."""
        for _ in range(30):
            if _autotune_cancel_event.is_set():
                return None
            try:
                if requests.get(f"{url}/health", timeout=2).ok:
                    data = requests.get(f"{url}/v1/models", timeout=2).json() or {}
                    rows = data.get("data") or []
                    return (rows[0].get("id") if rows else None) or self.model_id
            except Exception: pass  # not ready yet, poll again
            time.sleep(1)
        return None

    def _stick(self, measure: dict) -> dict:
        """One short speed-bench run against the live server: single-turn 1k prompts, fixed osl."""
        cfg = _require_ctx().config
        rt = _bench_live_runtime()
        if not rt["python"] or not rt["script"]:
            return {"ok": False, "error": "speed-bench runtime not installed"}
        url = f"http://127.0.0.1:{_autotune_port()}"
        server_model = self._server_ready(url)
        if not server_model:
            return {"ok": False, "error": "server did not become ready"}
        req = {"model_id": server_model, "bench": _at.STICK_BENCH, "categories": "all",
               "osl": _at.STICK_OSL, "limit": int(measure.get("limit") or _at.STICK_LIMIT),
               "concurrency": [int(measure.get("concurrency") or 1)], "timeout_s": 300,
               "extra_inputs": {"temperature": 0}, "baseline_run_id": None}
        self._n += 1
        out_dir = _bl.bench_dir(cfg.AGENT_INSTALL_DIR) / "runs" / f"at-{self.run_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        token = re.sub(r"[^A-Za-z0-9_.-]", "_", self.model_id)[:60]
        out_path = out_dir / f"stick-{self._n}-{token}.json"
        # A failed run writes nothing; a stale file here would be read as this run's result.
        out_path.unlink(missing_ok=True)
        benv = dict(os.environ, PYTHONUNBUFFERED="1", HF_HUB_DISABLE_PROGRESS_BARS="1")
        level = req["concurrency"][0]
        t0 = time.monotonic()
        rc, cancelled, elapsed = _bl.run_level_subprocess(
            _bl.build_cmd(rt["python"], rt["script"], url, req, level, str(out_path)),
            benv, _autotune_put, self.model_id, level, _autotune_cancel_event,
            _autotune_track_aux, _autotune_untrack_aux)
        wall = elapsed if elapsed is not None else (time.monotonic() - t0)
        if cancelled:
            return {"ok": False, "error": "cancelled"}
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "error": f"speed-bench produced no output (rc={rc})"}
        s = _bl.level_summary(payload, wall)["all"]
        return {"ok": rc in (0, 1) and bool(s.get("pred_tps")), "decode_tps": s.get("pred_tps"),
                "prefill_tps": s.get("prompt_tps"), "latency_s": s.get("latency_s"), "agg_tps": s.get("agg_pred_tps"),
                "accept": s.get("accept_rate"), "completion_tokens": s.get("completion_tokens"),
                "seconds": round(wall, 1), "error": None if rc in (0, 1) else f"speed-bench rc={rc}"}

    def kl(self, args, write_base):
        """Writes the f16 KL base, or scores the current args against it."""
        ppl = _autotune_perplexity_bin()
        text = _autotune_kl_text()
        if ppl is None or not text.is_file():
            return {"ok": False, "kl": None, "error": "llama-perplexity or kl_text.txt missing"}
        hf_arg = _bench_get_hf_arg(self.model_id)
        if hf_arg is None:
            return {"ok": False, "kl": None, "error": "no HF reference for model"}
        base = _bl.bench_dir(_require_ctx().config.AGENT_INSTALL_DIR) / "runs" / f"at-{self.run_id}" / "base.kld"
        base.parent.mkdir(parents=True, exist_ok=True)
        cmd = [str(ppl), "-hf", hf_arg, "-f", str(text), "-c", "2048"] + _at.kl_args(list(args)) \
            + ["--kl-divergence-base", str(base)] + ([] if write_base else ["--kl-divergence"])
        _autotune_put({"type": "line", "model_id": self.model_id, "text": "[autotune] " + " ".join(cmd)})
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace", close_fds=True, env=self.env,
                                    start_new_session=True)
        except OSError as e:
            return {"ok": False, "kl": None, "error": str(e)[:200]}
        _autotune_track_aux(proc)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=1800)
        except subprocess.TimeoutExpired:
            timed_out = True
            with best_effort("autotune kl: kill on timeout", log=log):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with best_effort("autotune kl: reap after timeout", log=log):
                proc.wait(timeout=5)
            out = ""
        finally:
            _autotune_untrack_aux()
        for line in (out or "").splitlines()[-40:]:
            _autotune_put({"type": "line", "model_id": self.model_id, "text": line})
        kl = None if write_base else _at.parse_kl(out or "")
        ok = not timed_out and proc.returncode == 0 and (write_base or kl is not None)
        if timed_out:
            err = "llama-perplexity timed out"
        elif proc.returncode < 0:
            err = (f"llama-perplexity crashed (signal {-proc.returncode}) — the binary looks incompatible "
                   "with the installed llama.cpp libraries; reinstall the llama.cpp tools")
        else:
            err = f"llama-perplexity rc={proc.returncode}"
        return {"ok": ok, "kl": kl, "error": None if ok else err}

    def energy_start(self):
        self._energy = _bl.PowerIntegrator(_live_power_w)
        self._energy.start()

    def energy_stop(self):
        if self._energy is None:
            return None, None
        wh, src = self._energy.stop()
        self._energy = None
        return wh, src


_llama_help_cache: dict[str, set] = {}
_HELP_MIN_VALUED = 5


def _llama_help_valued() -> Optional[set]:
    """Long llama-server options that take a value, from `--help`; cached per binary path+mtime."""
    b = _require_ctx().config.LLAMA_BIN or ""
    if not b:
        return None
    try:
        key = f"{b}:{os.stat(b).st_mtime_ns}"
    except OSError:
        return None
    if key in _llama_help_cache:
        return _llama_help_cache[key]
    try:
        env = os.environ.copy()
        parent = str(Path(b).parent)
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{parent}:{existing}" if existing else parent
        r = subprocess.run([b, "--help"], capture_output=True, text=True, errors="replace",
                           timeout=20, env=env)
        found = _at.parse_help_valued((r.stdout or "") + (r.stderr or ""))
    except Exception as e:
        log.warning("llama-server --help failed: %s", e)
        return None
    if len(found) < _HELP_MIN_VALUED:
        log.warning("llama-server --help parsed %d valued options — treating as unusable", len(found))
        return None
    _llama_help_cache[key] = found
    return found


def _autotune_set_perf_mode(mode: str) -> None:
    """Trigger {performance|powersave}.service via reload-or-restart; emits perf_mode SSE event."""
    if mode not in ("performance", "powersave"):
        return
    rc = None
    err = ""
    ok = False
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemctl", "reload-or-restart", mode],
            capture_output=True, text=True, timeout=30,
        )
        rc = r.returncode
        err = (r.stderr or r.stdout or "").strip()[:240]
        ok = (rc == 0)
    except Exception as e:
        err = str(e)[:240]
    _autotune_put({"type": "perf_mode", "mode": mode, "ok": ok,
                   "rc": rc, "error": (None if ok else (err or "unknown"))})


def _autotune_run_all(req: dict) -> None:
    global _autotune_active, _autotune_proc
    _autotune_cancel_event.clear()
    run_id = _autotune_run_id
    try:
        env = os.environ.copy()
        parent = str(Path(_require_ctx().config.LLAMA_BIN).parent) if _require_ctx().config.LLAMA_BIN else ""
        existing = env.get("LD_LIBRARY_PATH", "")
        if parent:
            env["LD_LIBRARY_PATH"] = f"{parent}:{existing}" if existing else parent
        env["FORCE_COLOR"] = "0"
        env["PYTHONUNBUFFERED"] = "1"
        # Flip to performance so load timing isn't skewed; restored in finally.
        _autotune_set_perf_mode("performance")
        rt = _bench_live_runtime()
        sizes = {}
        with best_effort("autotune: catalog sweep", log=log):
            sizes = _llama_catalog_sweep()[0]
        drafts = []
        with best_effort("autotune: cache gguf list", log=log):
            drafts = _list_cache_ggufs(_hf_cache_root())
        valued = _llama_help_valued()
        if valued is None:
            _autotune_put({"type": "line",
                           "text": "[autotune] could not read llama-server --help; on/off flags are guessed"})
        info = {"run_id": run_id, "runtime": bool(rt["python"] and rt["script"]),
                "perplexity": _autotune_perplexity_bin() is not None and _autotune_kl_text().is_file(),
                "drafts": drafts, "cores": _at.physical_cores(_read_cpuinfo(), os.cpu_count() or 1),
                "llama_build": _llama_build_last or ""}
        for mid in req["model_ids"]:
            if _autotune_cancel_event.is_set():
                break
            cp = _llama_read_ini()
            section = dict(cp[mid]) if cp.has_section(mid) else {}
            hf_arg = _bench_get_hf_arg(mid) or ""
            menv = dict(info, target_repo=hf_arg.split(":")[0], target_size=int(sizes.get(mid) or 0))
            menv["valued"] = valued
            if req.get("mode") == "quality":
                done = _at.run_quality(mid, section, req, _AutotuneBackend(mid, env, run_id), _autotune_put,
                                       _autotune_cancel_event.is_set, menv)
                _shared.post_tool_run(_require_ctx(), "quality", "llama", run_id, mid, done["ok"],
                                      _at.ledger_summary(done))
                continue
            done = _at.run_model(mid, section, req, _AutotuneBackend(mid, env, run_id), _autotune_put,
                                 _autotune_cancel_event.is_set, menv)
            _shared.post_tool_run(_require_ctx(), "autotune", "llama", run_id, mid, done["ok"],
                                  _at.ledger_summary(done))
        cancelled = _autotune_cancel_event.is_set()
        _autotune_put({"type": "done", "ok": not cancelled, "cancelled": cancelled,
                       "count": len(req["model_ids"])})
    except Exception as e:
        log.error("autotune run error: %s", e, exc_info=True)
        _autotune_put({"type": "done", "ok": False, "error": str(e)})
    finally:
        with best_effort("autotune: restore powersave perf mode", log=log):
            _autotune_set_perf_mode("powersave")
        # base.kld and the stick JSONs are scratch; the summaries already went out as events.
        with best_effort("autotune: drop run scratch dir", log=log):
            shutil.rmtree(_bl.bench_dir(_require_ctx().config.AGENT_INSTALL_DIR) / "runs" / f"at-{run_id}",
                          ignore_errors=True)
        _autotune_proc = None
        _autotune_untrack_aux()
        with _autotune_lock:
            _autotune_active = False


def llama_autotune_preflight(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """What the tuner can do on this host: cores, RAM/VRAM, KL tooling, speed-bench runtime, drafts."""
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    rt = _bench_live_runtime()
    sizes: dict = {}
    with best_effort("autotune preflight: catalog sweep", log=log):
        sizes = _llama_catalog_sweep()[0]
    gpu: dict = {}
    with best_effort("autotune preflight: gpu", log=log):
        gpu = collect_gpu() or {}
    vram = gpu.get("vram_total_bytes")
    hv = _llama_help_valued()
    pdet = _autotune_perplexity_status()
    return {"ok": True, "busy": bool(_bench_active or _autotune_active), "unit_active": _llama_unit_active(),
            "help_valued": {"ok": hv is not None, "count": len(hv or ())},
            "llama_build": _llama_build_last or "",
            "cores": _at.physical_cores(_read_cpuinfo(), os.cpu_count() or 1),
            "perplexity": pdet["ok"],
            "perplexity_detail": {"present": pdet["present"], "kl_text": pdet["kl_text"],
                                  "runnable": pdet["runnable"], "rc": pdet["rc"], "hint": pdet["hint"]},
            "runtime": {"ok": bool(rt["python"] and rt["script"]), **rt},
            "drafts": _list_cache_ggufs(_hf_cache_root()), "sizes": sizes,
            "vram_total_mb": int(vram // (1024 * 1024)) if isinstance(vram, (int, float)) and vram else None,
            "ram_total_mb": _ram_total_mb()}


def llama_autotune_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    global _autotune_active, _autotune_run_id
    cache_root = _hf_cache_root()
    try:
        req = _at.validate_request(body or {}, cache_root=cache_root, v1_args=_autotune_build_optional_args)
    except (ValueError, TypeError, AttributeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if req.get("mode") == "quality":
        pdet = _autotune_perplexity_status()
        if not pdet["ok"]:
            return {"ok": False, "error": pdet["hint"]}
    # Refuse to start if llama-server is running — port/VRAM would collide.
    if _llama_unit_active():
        return {"ok": False, "error": f"{_require_ctx().config.LLAMA_SYSTEMD_UNIT} is running — stop it before auto-tune"}
    with _autotune_lock:
        if _autotune_active or _bench_active:
            return {"ok": False, "error": "Another benchmark or auto-tune is in progress"}
        _autotune_active = True
        _autotune_run_id = uuid.uuid4().hex[:12]
        # Reset the buffer before the lock drops: a stream landing between
        # active=True and start_run would replay the prior run's stale done.
        with _autotune_cond:
            _autotune_replay.start_run(_autotune_run_id)
    threading.Thread(target=_autotune_run_all, args=(req,), daemon=True).start()
    return {"ok": True, "run_id": _autotune_run_id}


def llama_autotune_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    _require_ctx().check_stream_auth(authorization, token, "/llama/autotune/stream")
    _llama_check_enabled()
    return _shared.bench_replay_sse(
        _autotune_replay, _autotune_cond, lambda: _autotune_active, last_event_id)


def llama_autotune_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    _autotune_cancel_event.set()
    aux, aux_pgid = _autotune_aux_proc, _autotune_aux_pgid
    if aux is not None and aux.poll() is None:
        with best_effort("autotune cancel: terminate aux group", log=log):
            if aux_pgid is not None:
                try: os.killpg(aux_pgid, signal.SIGTERM)
                except ProcessLookupError: pass  # group already gone
            else:
                aux.terminate()
            try:
                aux.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if aux_pgid is not None:
                    try: os.killpg(aux_pgid, signal.SIGKILL)
                    except ProcessLookupError: pass  # group already gone
                else:
                    aux.kill()
    proc, pgid = _autotune_proc, _autotune_pgid
    if proc is None:
        res = _pkill_strays(['llama-server'], "autotune cancel")
        if res["ok"]:
            res["msg"] = "no tracked autotune process"
        return res
    try:
        if pgid is not None:
            # tolerate the group already being gone
            try: os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError: pass
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                try:
                    # force-kill; tolerate the group already being gone
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    log.warning("autotune cancel: killpg SIGKILL failed: %s", e)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.warning("autotune cancel: process %s survived SIGKILL", proc.pid)
            return {"ok": False, "error": "autotune process did not terminate"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


# ── Route registration ────────────────────────────────────────────────

def llama_tools_state(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    """Whether a bench/autotune job is running, for the manager Tools view."""
    _require_ctx().check_bearer(authorization)
    return {"ok": True, "bench_active": bool(_bench_active),
            "autotune_active": bool(_autotune_active)}


_ROUTES: tuple = (
    ("GET",    "/llama/state",                    llama_state_endpoint),
    ("GET",    "/llama/server/status",            llama_server_status_endpoint),
    ("POST",   "/llama/server/start",             llama_server_start_endpoint),
    ("POST",   "/llama/server/stop",              llama_server_stop_endpoint),
    ("POST",   "/llama/server/restart",           llama_server_restart_endpoint),
    ("POST",   "/llama/server/wake",              llama_server_wake_endpoint),
    ("GET",    "/llama/server/svcconfig",         llama_svcconfig_get),
    ("POST",   "/llama/server/svcconfig",         llama_svcconfig_post),
    ("GET",    "/llama/log/tail",                 llama_log_tail),
    ("GET",    "/llama/log/stream",               llama_log_stream),
    ("GET",    "/llama/models",                   llama_models_endpoint),
    ("GET",    "/llama/models/sizes",              llama_model_sizes_endpoint),
    ("POST",   "/llama/openai/chat/completions",  llama_openai_chat),
    ("POST",   "/llama/openai/completions",       llama_openai_completions),
    ("POST",   "/llama/load",                     llama_load_endpoint),
    ("POST",   "/llama/unload",                   llama_unload_endpoint),
    ("GET",    "/llama/config",                   llama_config_get),
    ("POST",   "/llama/config",                   llama_config_post),
    ("DELETE", "/llama/config/{model_id:path}",   llama_config_delete),
    ("POST",   "/llama/download",                 llama_download_endpoint),
    ("POST",   "/llama/download/cancel",          llama_download_cancel),
    ("GET",    "/llama/download/stream",          llama_download_stream),
    ("POST",   "/llama/build",                    llama_build),
    ("GET",    "/llama/build/stream",             llama_build_stream),
    ("GET",    "/llama/cache",                    llama_cache_list),
    ("GET",    "/llama/cache/gguf",               llama_cache_gguf),
    ("POST",   "/llama/cache/prune",              llama_cache_prune),
    ("POST",   "/llama/cache/rm",                 llama_cache_rm),
    ("GET",    "/llama/hf-trending",              llama_hf_trending),
    ("POST",   "/llama/bench/run",                llama_bench_run),
    ("GET",    "/llama/bench/stream",             llama_bench_stream),
    ("POST",   "/llama/bench/cancel",             llama_bench_cancel),
    ("POST",   "/llama/bench/perf-mode",          llama_bench_perf_mode),
    ("GET",    "/llama/bench/live/preflight",     llama_bench_live_preflight),
    ("POST",   "/llama/bench/live/setup",         llama_bench_live_setup),
    ("POST",   "/llama/bench/live/run",           llama_bench_live_run),
    ("GET",    "/llama/autotune/preflight",       llama_autotune_preflight),
    ("POST",   "/llama/autotune/run",             llama_autotune_run),
    ("GET",    "/llama/autotune/stream",          llama_autotune_stream),
    ("POST",   "/llama/autotune/cancel",          llama_autotune_cancel),
    ("GET",    "/llama/tools/state",              llama_tools_state),
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])


def start_background() -> "Optional[asyncio.Task]":
    """Spawn the /models/sse listener + perf-controller task. Call from FastAPI lifespan."""
    ctx = _require_ctx()
    _maybe_start_sse_listener()
    if not ctx.config.PERF_CONTROLLER_ENABLED:
        return None
    return asyncio.create_task(perf_controller_loop())
