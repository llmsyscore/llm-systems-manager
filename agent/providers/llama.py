"""llama.cpp provider — 32 routes + collector + perf-controller background."""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
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
from collections import deque
from pathlib import Path
from typing import Any, Iterator, Optional

import requests
from fastapi import Header, HTTPException, Query
from fastapi.responses import StreamingResponse

import stream_pool  # type: ignore[import-not-found]  # sibling at agent root
from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling at agent root

from collectors.gpu import collect_gpu  # type: ignore
from . import llama_install
from . import llama_upgrade

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

_llama_info_cache: dict[str, Any] = {}
_llama_info_last_poll: float = 0.0
_llama_info_last_active_ts: float = 0.0
_llama_info_last_tokens_total: "int | None" = None
_llama_info_last_loaded_model: "str | None" = None
_llama_info_conn_fail_count: int = 0
_llama_info_idle_logged: bool = False

_HF_REPO_RE = re.compile(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$')
_LLAMA_LOG_IGNORE = (
    "GET /v1/models",
    "GET /metrics",
    "update_slots: all slots are idle",
)

_dl_queue: "_queue_lib.Queue[dict[str, Any]]" = _queue_lib.Queue(maxsize=2000)
_dl_lock = threading.Lock()
_dl_active = False
_dl_proc: "Optional[subprocess.Popen]" = None
_dl_cancelled = False

_log_queue: "_queue_lib.Queue[str]" = _queue_lib.Queue(maxsize=4096)
_log_lock = threading.Lock()
_log_streaming = False

_LLAMA_VALUE_FLAGS = {
    "--threads", "--timeout", "--log-file", "--sleep-idle-seconds",
    "--host", "--port", "--parallel", "--models-max",
    "--models-preset", "--model",
    "--api-key", "--keep", "--ctx-size", "--batch-size",
    "--gpu-layers", "--tensor-parallel", "-t", "-c", "-ngl",
    "--mmap", "--no-mmap", "--log-disable", "--cont-batch",
    "--embedding", "--no-display", "--simple-io",
    "--chat-template",
}

_build_queue: "_queue_lib.Queue[dict[str, Any]]" = _queue_lib.Queue(maxsize=4000)
_build_lock = threading.Lock()
_build_running = False

_bench_queue: "_queue_lib.Queue[dict]" = _queue_lib.Queue(maxsize=5000)
_bench_lock = threading.Lock()
_bench_active = False
_bench_proc: "Optional[subprocess.Popen]" = None
_bench_pgid: "Optional[int]" = None
_bench_cancel_event = threading.Event()
_BENCH_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_autotune_queue: "_queue_lib.Queue[dict]" = _queue_lib.Queue(maxsize=5000)
_autotune_lock = threading.Lock()
_autotune_active = False
_autotune_proc: "Optional[subprocess.Popen]" = None
_autotune_pgid: "Optional[int]" = None
_autotune_cancel_event = threading.Event()

_AT_NCTX_RE = re.compile(r"n_ctx_seq\s*\(\s*(\d+)\s*\)")
_AT_NCTX_FALLBACK_RE = re.compile(r"\bn_ctx\b\s*=\s*(\d+)")
_AT_MEM_RE = re.compile(
    r"common_memory_breakdown_print:\s*\|\s*-\s*(?P<label>[^|]*?)\|\s*(?P<total>\d+)\s*=\s*(?P<free>\d+)\s*\+"
)
_AT_GPU_HINT_RE = re.compile(r"(?i)vulkan|rocm|cuda|hip|metal")
_AT_MODEL_LOADED_RE = re.compile(r"(?:^|\s)(?:\w+\s*:\s*)?model loaded\b", re.IGNORECASE)
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
    """Best-effort state: state-file+TCP-probe, then /v1/models, else unknown.

    State file alone lies after `systemctl stop` (perf controller doesn't
    clear it), so verify with a non-disturbing TCP connect.
    """
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
    try:
        return float(line.split()[-1])
    except Exception:
        return None


def collect_llama_for_metrics() -> dict[str, Any]:
    """Agent-side rich llama snapshot; skips /metrics + /slots when sleeping or token-idle."""
    if not _require_ctx().config.LLAMA_ENABLED:
        return {}

    global _llama_info_cache, _llama_info_last_poll
    global _llama_info_last_active_ts, _llama_info_last_tokens_total
    global _llama_info_last_loaded_model, _llama_info_conn_fail_count
    global _llama_info_idle_logged

    now = time.time()
    interval = max(2.0, _require_ctx().config.POLL_INTERVAL_S)

    if now - _llama_info_last_poll < interval:
        return dict(_llama_info_cache) if _llama_info_cache else {}
    _llama_info_last_poll = now

    state = llama_get_state()
    api_base = _require_ctx().config.LLAMA_API_URL.rstrip("/")

    llama: dict[str, Any] = {
        "state": state,
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
    }

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

    if matched_sleep and cur != "sleeping":
        log.info("perf transition wake->sleep (matched %r); switching to %s",
                    matched_sleep, _require_ctx().config.PERF_TARGET_SLEEP)
        await _perf_switch(_require_ctx().config.PERF_TARGET_SLEEP)
        llama_write_state_file("sleeping")
        with _require_ctx().runtime_lock:
            _require_ctx().state["llama_state"] = "sleeping"
            _require_ctx().state["perf_sleep_count"] += 1
            _require_ctx().state["perf_last_transition"] = {"to": "sleeping", "ts": _require_ctx().now_iso(), "marker": matched_sleep}
        _push_llama_state_to_manager("sleeping")
        return

    if matched_wake and cur != "awake":
        log.info("perf transition sleep->wake (matched %r); switching to %s",
                    matched_wake, _require_ctx().config.PERF_TARGET_AWAKE)
        await _perf_switch(_require_ctx().config.PERF_TARGET_AWAKE)
        llama_write_state_file("awake")
        with _require_ctx().runtime_lock:
            _require_ctx().state["llama_state"] = "awake"
            _require_ctx().state["perf_wake_count"] += 1
            _require_ctx().state["perf_last_transition"] = {"to": "awake", "ts": _require_ctx().now_iso(), "marker": matched_wake}
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


def llama_state_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization)
    if not _require_ctx().config.LLAMA_ENABLED:
        raise HTTPException(status_code=503, detail="llama not enabled on this agent")
    return {
        "state": llama_get_state(),
        "port": int(_require_ctx().config.LLAMA_API_URL.rsplit(":", 1)[-1]) if ":" in _require_ctx().config.LLAMA_API_URL else None,
        "perf_controller_enabled": _require_ctx().config.PERF_CONTROLLER_ENABLED,
        "last_transition": _require_ctx().state.get("perf_last_transition"),
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


def _delete_quant_from_hf_cache(model_id: str) -> "tuple[list[str], Optional[str]]":
    """Unlink the specific quant's .gguf from the HF cache. Returns (deleted_paths, error_or_none)."""
    repo = None
    quant = None
    try:
        cp = _llama_read_ini()
        if cp.has_section(model_id):
            sec = cp[model_id]
            repo = sec.get("hf-repo") or sec.get("--hf-repo")
            quant = sec.get("hf-file") or sec.get("--hf-file")
    except Exception as e:
        log.warning("hf-cache delete: read ini failed: %s", e, exc_info=True)

    if not repo and ":" in model_id and "/" in model_id:
        repo, quant = model_id.rsplit(":", 1)
    elif not repo:
        return [], f"Could not derive repo from model_id={model_id!r}"

    if not repo or "/" not in repo:
        return [], f"Repo missing or malformed: {repo!r}"
    if not quant:
        return [], "Quant identifier missing — refusing to wildcard-delete the whole repo"

    cache_root = _hf_cache_root()
    repo_dir = cache_root / f"models--{repo.replace('/', '--')}"
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return [], f"Snapshots dir not found: {snapshots}"

    deleted: list[str] = []
    for snap in snapshots.iterdir():
        if not snap.is_dir():
            continue
        if quant.endswith(".gguf"):
            candidates = sorted(snap.glob(quant))
        else:
            candidates = [
                p for p in snap.glob(f"*{quant}*.gguf")
                if not p.name.startswith("mmproj-")
            ]
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

    if not deleted:
        return [], f"No quant files matched {quant!r} under {snapshots}"
    return deleted, None


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

        _dl_put({"type": "start", "cmd": " ".join(str(c) for c in cmd)})

        ansi_re = re.compile(r'\x1b\[[0-9;]*[mGKHF]')

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
                line = ansi_re.sub("", raw.decode("utf-8", errors="replace")).strip()
                if line:
                    _dl_put({"type": "line", "text": line})
            proc.wait()
        else:
            import pty
            import select as _select

            env["TERM"] = "xterm-256color"
            master_fd, slave_fd = pty.openpty()
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
                while True:
                    try:
                        r, _, _ = _select.select([master_fd], [], [], 0.5)
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
                        text = ansi_re.sub("", text)
                        buf += text
                        parts = buf.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                        buf = parts[-1]
                        for line in parts[:-1]:
                            line = line.strip()
                            if line and line != last_line:
                                _dl_put({"type": "line", "text": line})
                                last_line = line
                    except (OSError,):
                        break

                if buf.strip() and buf.strip() != last_line:
                    _dl_put({"type": "line", "text": buf.strip()})
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
    """tail -F llama-server.log → _log_queue, dropping idle/noisy lines."""
    global _log_streaming
    proc = None
    try:
        proc = subprocess.Popen(
            ["tail", "-n", "100", "-F", _require_ctx().config.LLAMA_LOG_FILE],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
        )
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            if not _log_streaming:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line and _llama_log_should_keep(line):
                try:
                    _log_queue.put(line, timeout=1)
                except _queue_lib.Full:
                    try: _log_queue.get_nowait()
                    except _queue_lib.Empty: pass
                    try: _log_queue.put_nowait(line)
                    except _queue_lib.Full: pass
    except Exception as e:
        try: _log_queue.put_nowait(f"[log stream error: {e}]")
        except _queue_lib.Full: pass
    finally:
        if proc is not None:
            with best_effort("log streamer: terminate proc", log=log):
                proc.terminate()
            try: proc.wait(timeout=3)
            except Exception:
                with best_effort("log streamer: kill proc", log=log):
                    proc.kill()
        with _log_lock:
            _log_streaming = False


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


def llama_server_wake_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    # GET /v1/models for loaded id, then POST chat/completions to wake (timeout generous for reload).
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    base = _require_ctx().config.LLAMA_API_URL.rstrip("/")
    model_id = None
    try:
        mr = _require_ctx().post_session.get(f"{base}/v1/models", timeout=5)
        if not mr.ok:
            return {"ok": False, "status": mr.status_code,
                    "error": f"GET /v1/models returned HTTP {mr.status_code}: {mr.text[:200]}"}
        data = mr.json() or {}
        for entry in (data.get("data") or []):
            if entry.get("id"):
                model_id = entry["id"]; break
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
                    expects_value = p in _LLAMA_VALUE_FLAGS
                    if expects_value and i + 1 < len(parts):
                        args.append({"flag": p, "value": parts[i + 1], "bool": False})
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
    args_list = body.get("args", [])
    binary = body.get("binary", "")
    do_restart = bool(body.get("restart", False))
    svc_path = _llama_svc_file_path()

    try:
        content = Path(svc_path).read_text()
    except Exception as e:
        return {"ok": False, "error": f"Cannot read service file: {e}"}

    cmd_parts = [binary]
    for a in args_list:
        cmd_parts.append(a["flag"])
        if not a.get("bool") and a.get("value") not in (None, ""):
            cmd_parts.append(str(a["value"]))
    exec_line = "ExecStart=" + " ".join(cmd_parts)
    new_lines = []
    for line in content.splitlines():
        if line.strip().startswith("ExecStart="):
            new_lines.append(exec_line)
        else:
            new_lines.append(line)
    new_content = "\n".join(new_lines) + "\n"

    try:
        r = subprocess.run(
            ["sudo", "-n", "tee", svc_path],
            input=new_content.encode(), capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            return {"ok": False,
                    "error": r.stderr.decode().strip() or "sudo tee failed — check sudoers NOPASSWD for tee"}
    except Exception as e:
        return {"ok": False, "error": f"Write failed: {e}"}

    try:
        subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "daemon-reload"],
                       timeout=10, check=True, capture_output=True)
    except Exception as e:
        return {"ok": False, "error": f"daemon-reload failed: {e}"}

    if do_restart:
        try:
            subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", _require_ctx().config.LLAMA_SYSTEMD_UNIT],
                           timeout=30, check=True, capture_output=True)
        except Exception as e:
            return {"ok": False, "error": f"restart failed: {e}"}
    return {"ok": True}


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

    global _log_streaming
    with _log_lock:
        if not _log_streaming:
            _log_streaming = True
            while not _log_queue.empty():
                try: _log_queue.get_nowait()
                except Exception: break
            threading.Thread(target=_llama_log_streamer, daemon=True).start()

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


def llama_models_endpoint(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    try:
        resp = requests.get(f"{_require_ctx().config.LLAMA_API_URL.rstrip('/')}/v1/models", timeout=5)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


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
            if isinstance(st, dict) and st.get("value") in ("loaded", "loading", "sleeping"):
                log.info("Unloading %s before loading %s", m["id"], model_id)
                ur = requests.post(f"{api}/models/unload",
                                   json={"model": m["id"]}, timeout=30)
                log.info("Unload response: %s %s", ur.status_code, ur.text[:200])

        time.sleep(2)
        log.info("Loading model: %s", model_id)
        lr = requests.post(f"{api}/models/load", json={"model": model_id}, timeout=30)
        log.info("Load response: %s %s", lr.status_code, lr.text[:200])

        if lr.status_code == 404:
            raise HTTPException(status_code=404,
                                detail=f"Model not found in llama-server (404). "
                                       f"Verify '{model_id}' matches a registered model ID.")
        try:
            body_resp = lr.json()
        except Exception:
            body_resp = {"raw": lr.text[:500]}
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


def llama_unload_endpoint(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    model_id = body.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="model required")
    try:
        resp = requests.post(f"{_require_ctx().config.LLAMA_API_URL.rstrip('/')}/models/unload",
                             json={"model": model_id}, timeout=15)
        return {"ok": True, "response": resp.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
                    if not res.skipped:
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


def llama_hf_trending(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    env = dict(os.environ)
    env["FORCE_COLOR"] = "0"
    try:
        out = subprocess.check_output(
            [_hf_cli_path(), "models", "ls",
             "--sort", "trending_score",
             "--limit", "10",
             "--format", "json",
             "--expand", "author,downloadsAllTime,trendingScore,createdAt,lastModified",
             "--num-parameters", "min:27B,max:35B"],
            text=True, timeout=30, close_fds=True,
            stderr=subprocess.DEVNULL, env=env,
        )
        try:
            data = json.loads(out)
        except Exception:
            return {"ok": False, "error": "Failed to parse JSON", "raw": out}
        return {"ok": True, "data": data if isinstance(data, list) else [data]}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": getattr(e, "output", str(e))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _bench_put(msg: dict) -> None:
    try:
        _bench_queue.put_nowait(msg)
    except _queue_lib.Full:
        try: _bench_queue.get_nowait()
        except _queue_lib.Empty: pass
        try: _bench_queue.put_nowait(msg)
        except _queue_lib.Full: pass


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
    if not isinstance(row, dict):
        return (None, None)
    if tool == "llama-bench":
        ts_val = row.get("avg_ts") or row.get("t_s")
        if ts_val is None:
            return (None, None)
        n_p = int(row.get("n_prompt", 0) or 0)
        n_g = int(row.get("n_gen", 0) or 0)
        if n_p > 0 and n_g == 0: return (None, float(ts_val))
        if n_g > 0 and n_p == 0: return (float(ts_val), None)
        if n_p > 0 and n_g > 0:  return (float(ts_val), None)
        return (None, None)
    pp = row.get("pp_tps") or row.get("avg_pp_tps") or row.get("t_pp")
    tg = row.get("tg_tps") or row.get("avg_tg_tps") or row.get("t_tg")
    return (float(tg) if tg is not None else None,
            float(pp) if pp is not None else None)


def _bench_tool_path(tool: str) -> str:
    if not _require_ctx().config.LLAMA_BIN:
        raise RuntimeError("LLAMA_BIN not configured")
    return str(Path(_require_ctx().config.LLAMA_BIN).parent / tool)


def _bench_run_one(model_id: str, tool: str, switches: list, env: dict) -> None:
    global _bench_proc, _bench_pgid
    try:
        tool_path = _bench_tool_path(tool)
    except Exception as e:
        _bench_put({"type": "model_done", "model_id": model_id, "ok": False, "error": str(e)})
        return
    if not Path(tool_path).exists():
        _bench_put({"type": "model_done", "model_id": model_id, "ok": False,
                    "error": f"binary not found on agent: {tool_path}"})
        return
    hf_arg = _bench_get_hf_arg(model_id)
    if not hf_arg:
        _bench_put({"type": "model_done", "model_id": model_id, "ok": False,
                    "error": f"no HF reference found for {model_id}"})
        return
    jsonl_flags = ["-o", "jsonl"]
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

    latest_gen = None
    latest_ppt = None
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
        gen_tps, ppt_tps = _bench_parse_row(row, tool)
        if gen_tps is None and ppt_tps is None:
            continue
        if gen_tps is not None: latest_gen = gen_tps
        if ppt_tps is not None: latest_ppt = ppt_tps
        result_row = {
            "n_prompt": int(row.get("n_prompt", 0) or 0),
            "n_gen":    int(row.get("n_gen", 0) or 0),
            "n_depth":  int(row.get("n_depth", 0) or 0),
            "n_batch":  int(row.get("n_batch", 0) or 0),
            "n_ubatch": int(row.get("n_ubatch", 0) or 0),
            "avg_ts":   float(row.get("avg_ts", 0) or 0),
        }
        result_rows.append(result_row)
        _bench_put({"type": "result", "model_id": model_id,
                    "gen_tps": gen_tps, "ppt_tps": ppt_tps, **result_row})

    proc.wait()
    _bench_proc = None
    cancelled = _bench_cancel_event.is_set()
    _bench_put({"type": "model_done", "model_id": model_id,
                "ok": (not cancelled) and proc.returncode == 0,
                "rc": proc.returncode, "cancelled": cancelled,
                "last_gen_tps": latest_gen, "last_ppt_tps": latest_ppt,
                "results": result_rows})


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
    if tool not in ("llama-bench", "llama-batched-bench"):
        raise HTTPException(status_code=400, detail="invalid tool")
    switches = body.get("switches") or []
    if not isinstance(switches, list):
        raise HTTPException(status_code=400, detail="switches must be a list")
    with _bench_lock:
        if _bench_active:
            return {"ok": False, "error": "Another benchmark is in progress"}
        _bench_active = True
        while not _bench_queue.empty():
            try: _bench_queue.get_nowait()
            except Exception: break
    threading.Thread(target=_bench_run_all,
                     args=(model_ids, tool, switches), daemon=True).start()
    return {"ok": True}


def llama_bench_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    _require_ctx().check_stream_auth(authorization, token, "/llama/bench/stream")
    _llama_check_enabled()
    def generate() -> Iterator[bytes]:
        while True:
            try:
                msg = _bench_queue.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n".encode()
                if msg.get("type") == "done":
                    break
            except _queue_lib.Empty:
                yield b'data: {"type":"keepalive"}\n\n'
    if not stream_pool.POOL.try_acquire():
        raise HTTPException(status_code=503, detail="agent at stream capacity; retry shortly")
    return StreamingResponse(
        stream_pool.guarded_async(generate()), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
        res = _pkill_strays(['llama-bench', 'llama-batched-bench'], "bench cancel")
        if res["ok"]:
            res["msg"] = "no tracked benchmark process"
        return res
    try:
        if pgid is not None:
            try: os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError: pass
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    log.warning("bench cancel: killpg SIGKILL failed: %s", e)
        with best_effort("bench cancel: pkill HIP/ROCm child procs", log=log):
            subprocess.run(['pkill', '-9', '-f', 'llama-bench'], capture_output=True, timeout=3)
            subprocess.run(['pkill', '-9', '-f', 'llama-batched-bench'], capture_output=True, timeout=3)
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


def _autotune_put(msg: dict) -> None:
    try:
        _autotune_queue.put_nowait(msg)
    except _queue_lib.Full:
        try: _autotune_queue.get_nowait()
        except _queue_lib.Empty: pass
        try: _autotune_queue.put_nowait(msg)
        except _queue_lib.Full: pass


def _autotune_build_optional_args(params: dict) -> list:
    """Translate optional-params dict into llama-server args; omits flags the user didn't enable."""
    out: list = []
    if not isinstance(params, dict):
        return out
    p = params
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


def _autotune_run_iter(model_id: str, fitt_mb: int, optional_params: dict,
                       env: dict, iter_idx: int) -> dict:
    """Run llama-server once with -fitt, wait for model-loaded, SIGTERM, parse output."""
    global _autotune_proc, _autotune_pgid
    if not _require_ctx().config.LLAMA_BIN:
        return {"ok": False, "error": "LLAMA_BIN not configured"}
    bin_path = _require_ctx().config.LLAMA_BIN
    if not Path(bin_path).exists():
        return {"ok": False, "error": f"llama-server not found at {bin_path}"}
    hf_arg = _bench_get_hf_arg(model_id)
    if not hf_arg:
        return {"ok": False, "error": f"no HF reference found for {model_id}"}

    cmd = [bin_path, "--models-max", "1",
           "-fitt", str(int(fitt_mb)), "-lv", "4"]
    cmd += _autotune_build_optional_args(optional_params)
    cmd += ["-hf", hf_arg]

    _autotune_put({
        "type": "iter_start", "model_id": model_id, "iter": iter_idx,
        "fitt": int(fitt_mb), "cmd": " ".join(str(c) for c in cmd),
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
    shutdown_buf: list = []
    start_ts = time.time()
    LOAD_TIMEOUT = 300
    last_progress = start_ts

    _hb_stop = threading.Event()
    def _hb_loop():
        while not _hb_stop.wait(2.0):
            elapsed = time.time() - start_ts
            _autotune_put({
                "type": "loading_progress", "model_id": model_id,
                "iter": iter_idx, "fitt": int(fitt_mb),
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
                    except ValueError: pass
                else:
                    mm2 = _AT_NCTX_FALLBACK_RE.search(line)
                    if mm2:
                        try: ctx_fallback = int(mm2.group(1))
                        except ValueError: pass
                # Detect whether llama-server's auto-fit actually trimmed
                # the ctx. Later occurrences overwrite earlier ones — the
                # final fit decision is what we want.
                if "common_params_fit_impl" in line:
                    if "no changes needed" in line:
                        fit_applied = False
                    elif "context size reduced from" in line:
                        fit_applied = True
            if _AT_MODEL_LOADED_RE.search(line):
                model_loaded = True
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

    # SIGTERM triggers llama-server's clean shutdown + post-load memory breakdown.
    pgid = _autotune_pgid
    if pgid is not None:
        try: os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError: pass
        except Exception as e:
            _autotune_put({"type": "line", "model_id": model_id,
                           "text": f"[autotune] SIGTERM error: {e}"})

    # Last GPU breakdown row found during shutdown is authoritative for free/total.
    with best_effort("autotune: drain shutdown stdout", log=log):
        for raw in iter(proc.stdout.readline, ""):
            if not raw:
                break
            line = _BENCH_ANSI_RE.sub("", raw.rstrip("\n"))
            if line:
                shutdown_buf.append(line)
                _autotune_put({"type": "line", "model_id": model_id, "text": line})
                mm = _AT_NCTX_RE.search(line)
                if mm:
                    try: ctx_seq = int(mm.group(1))
                    except ValueError: pass
                else:
                    mm2 = _AT_NCTX_FALLBACK_RE.search(line)
                    if mm2:
                        try: ctx_fallback = int(mm2.group(1))
                        except ValueError: pass

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if pgid is not None:
            try:
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

    if not model_loaded:
        return {"ok": False, "error": "model never reached 'main: model loaded'",
                "ctx_seq": ctx_seq, "actual_free_mb": None, "total_vram_mb": None}

    mem = _autotune_parse_shutdown_mem(shutdown_buf)
    if mem["ok"]:
        if ctx_seq is None:
            return {"ok": False, "error": "got memory breakdown but no n_ctx_seq",
                    "ctx_seq": None,
                    "actual_free_mb": mem["free_mb"], "total_vram_mb": mem["total_mb"],
                    "fit_applied": fit_applied}
        return {"ok": True, "ctx_seq": ctx_seq,
                "actual_free_mb": mem["free_mb"], "total_vram_mb": mem["total_mb"],
                "fit_applied": fit_applied}
    # Sentinel/missing — return raw numbers so the caller can retry with doubled -fitt.
    return {"ok": False, "sentinel": True,
            "raw_free_mb": mem.get("raw_free"),
            "raw_total_mb": mem.get("raw_total"),
            "reason": mem.get("reason"),
            "ctx_seq": ctx_seq, "actual_free_mb": None, "total_vram_mb": None}


def _autotune_run_one_model(model_id: str, target_mb: int, optional_params: dict,
                            env: dict, tolerance_mb: int = 50) -> None:
    """Iteratively converge actual_free_mb on target_mb (±tolerance_mb).

    Three regimes: plateau (fit doesn't engage; fit_applied=False), monotonic-up
    (secant/bisection), non-monotonic (compute buffer balloons at high -fitt).
    """
    TOL = max(1, int(tolerance_mb))
    MAX_ITERS = 10
    MAX_STEP = 1024
    MIN_STEP = 128
    # Must be < TOL so refinement can hit the requested precision.
    DEDUP_MB = max(5, min(25, TOL // 2 if TOL > 1 else 5))
    _autotune_put({"type": "model_start", "model_id": model_id,
                   "target_mb": int(target_mb), "tolerance_mb": TOL})

    fitt = max(0, int(target_mb))
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
            res = _autotune_run_iter(model_id, fitt, optional_params, env, i)
            if res.get("ok") or not res.get("sentinel"):
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

    _autotune_put({
        "type": "model_done", "model_id": model_id,
        "converged": converged,
        "final_fitt": (best or {}).get("fitt"),
        "ctx_size": (best or {}).get("ctx_seq"),
        "free_mb": (best or {}).get("actual_free_mb"),
        "total_vram_mb": (best or {}).get("total_vram_mb"),
        "iters": iters_done,
        "applied_params": optional_params or {},
        "stop_reason": stop_reason,
        "ok": best is not None,
    })


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


def _autotune_run_all(model_ids: list, target_mb: int, optional_params: dict,
                      tolerance_mb: int = 50) -> None:
    global _autotune_active, _autotune_proc
    _autotune_cancel_event.clear()
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
        for mid in model_ids:
            if _autotune_cancel_event.is_set():
                break
            _autotune_run_one_model(mid, target_mb, optional_params, env,
                                    tolerance_mb=tolerance_mb)
        cancelled = _autotune_cancel_event.is_set()
        _autotune_put({"type": "done", "ok": not cancelled, "cancelled": cancelled,
                       "count": len(model_ids)})
    except Exception as e:
        log.error("autotune run error: %s", e, exc_info=True)
        _autotune_put({"type": "done", "ok": False, "error": str(e)})
    finally:
        with best_effort("autotune: restore powersave perf mode", log=log):
            _autotune_set_perf_mode("powersave")
        _autotune_proc = None
        with _autotune_lock:
            _autotune_active = False


def llama_autotune_run(body: dict, authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    global _autotune_active
    model_ids = body.get("model_ids") or []
    if isinstance(model_ids, str):
        model_ids = [model_ids]
    model_ids = [str(m).strip() for m in model_ids if str(m).strip()]
    if not model_ids:
        raise HTTPException(status_code=400, detail="model_ids required")
    try:
        target_mb = int(body.get("target_mb"))
    except Exception:
        raise HTTPException(status_code=400, detail="target_mb (int, MB) required")
    if target_mb < 0:
        raise HTTPException(status_code=400, detail="target_mb must be >= 0")
    optional_params = body.get("optional_params") or {}
    if not isinstance(optional_params, dict):
        raise HTTPException(status_code=400, detail="optional_params must be an object")
    try:
        tolerance_mb = int(body.get("tolerance_mb", 50))
    except Exception:
        raise HTTPException(status_code=400, detail="tolerance_mb must be an integer")
    if tolerance_mb < 1:
        tolerance_mb = 1

    # Refuse to start if llama-server is running — port/VRAM would collide.
    with best_effort("autotune: probe llama unit is-active", log=log):
        st = subprocess.run(
            ["systemctl", "is-active", _require_ctx().config.LLAMA_SYSTEMD_UNIT],
            capture_output=True, text=True, timeout=5,
        )
        if (st.stdout or "").strip() == "active":
            return {"ok": False, "error": f"{_require_ctx().config.LLAMA_SYSTEMD_UNIT} is running — stop it before auto-tune"}

    with _autotune_lock:
        if _autotune_active:
            return {"ok": False, "error": "Another auto-tune is in progress"}
        _autotune_active = True
        while not _autotune_queue.empty():
            try: _autotune_queue.get_nowait()
            except Exception: break
    threading.Thread(target=_autotune_run_all,
                     args=(model_ids, target_mb, optional_params, tolerance_mb),
                     daemon=True).start()
    return {"ok": True}


def llama_autotune_stream(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    _require_ctx().check_stream_auth(authorization, token, "/llama/autotune/stream")
    _llama_check_enabled()
    def generate() -> Iterator[bytes]:
        while True:
            try:
                msg = _autotune_queue.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n".encode()
                if msg.get("type") == "done":
                    break
            except _queue_lib.Empty:
                yield b'data: {"type":"keepalive"}\n\n'
    if not stream_pool.POOL.try_acquire():
        raise HTTPException(status_code=503, detail="agent at stream capacity; retry shortly")
    return StreamingResponse(
        stream_pool.guarded_async(generate()), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def llama_autotune_cancel(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    _require_ctx().check_bearer(authorization); _llama_check_enabled()
    _autotune_cancel_event.set()
    proc, pgid = _autotune_proc, _autotune_pgid
    if proc is None:
        res = _pkill_strays(['llama-server'], "autotune cancel")
        if res["ok"]:
            res["msg"] = "no tracked autotune process"
        return res
    try:
        if pgid is not None:
            try: os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError: pass
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                try:
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
    ("POST",   "/llama/cache/prune",              llama_cache_prune),
    ("POST",   "/llama/cache/rm",                 llama_cache_rm),
    ("GET",    "/llama/hf-trending",              llama_hf_trending),
    ("POST",   "/llama/bench/run",                llama_bench_run),
    ("GET",    "/llama/bench/stream",             llama_bench_stream),
    ("POST",   "/llama/bench/cancel",             llama_bench_cancel),
    ("POST",   "/llama/bench/perf-mode",          llama_bench_perf_mode),
    ("POST",   "/llama/autotune/run",             llama_autotune_run),
    ("GET",    "/llama/autotune/stream",          llama_autotune_stream),
    ("POST",   "/llama/autotune/cancel",          llama_autotune_cancel),
)


def register_routes(app) -> None:
    for method, path, handler in _ROUTES:
        app.add_api_route(path, handler, methods=[method])


def start_background() -> "Optional[asyncio.Task]":
    """Spawn the perf-controller log-tail task when enabled. Call from FastAPI lifespan."""
    ctx = _require_ctx()
    if not ctx.config.PERF_CONTROLLER_ENABLED:
        return None
    return asyncio.create_task(perf_controller_loop())
