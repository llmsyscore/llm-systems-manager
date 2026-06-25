#!/usr/bin/env python3
"""
================================================================================
llm-systems-manager.py  —  LLM Systems Manager  (version: see __version__ below)
================================================================================
Flask backend for the LLM Systems Manager. Runs on
the main Linux host. Collects local GPU, CPU, RAM, disk,
network, UPS, and llama.cpp metrics on a dynamic 2-30 second interval driven
by GPU performance level and LMS activity. Receives remote LM Studio metrics
from the llm-systems-manager-agent via HTTP POST. All metric history is
persisted in InfluxDB via the alarm engine; SQLite (data/metrics.db) holds
only the model_benchmarks table. Serves the frontend index.html.

Dependencies / Requirements:
    Python 3.10+
    pip install flask psutil requests
    lm-sensors, liquidctl (optional, for hardware monitoring)
    sudo access for systemctl (llama_server.service)

Local endpoints served:
    GET  /                              — frontend index.html
    GET  /api/metrics                   — latest full system snapshot
    GET  /api/history                   — scalar time-series (proxies alarm engine / InfluxDB)
    GET  /api/alert                     — current alert flags
    GET  /api/config                    — dynamic poll interval
    GET  /api/llama-state               — llama server state + model + port
    POST /api/llm/server/start          — start llama_server.service
    POST /api/llm/server/stop           — stop llama_server.service
    POST /api/llm/server/restart        — restart llama_server.service
    GET  /api/llm/server/status         — systemctl status output
    GET  /api/llm/server/log/stream     — SSE: live llama-server log tail
    GET  /api/llm/server/log/tail       — last 50 filtered log lines (JSON)
    GET  /api/llm/models                — proxy /v1/models from llama-server
    POST /api/llm/load                  — load a model in llama-server
    POST /api/llm/unload                — unload a model from llama-server
    GET  /api/llm/config                — read config.ini
    POST /api/llm/config                — write config.ini
    DELETE /api/llm/config/<model>      — remove model section from config.ini
    POST /api/llm/download              — start HuggingFace model download
    GET  /api/llm/download/stream       — SSE: download progress
    GET  /api/llm/cache                 — list HF cache
    POST /api/llm/cache/prune           — prune HF cache detached revisions
    POST /api/llm/cache/rm              — remove HF cached repo
    GET  /api/llm/hf-trending           — top HF models 27-35B by downloads
    POST /api/remote/lmstudio           — receive LM Studio agent payload
    GET  /api/lmstudio/metrics          — latest LM Studio metrics
    GET  /api/lmstudio/models           — proxy /v1/models from LM Studio
    GET  /api/lmstudio/server/status    — lms server status (proxied via primary LMS agent)
    POST /api/lmstudio/server/start     — lms server start (proxied via primary LMS agent)
    POST /api/lmstudio/server/stop      — lms server stop (proxied via primary LMS agent)
    POST /api/lmstudio/server/restart   — lms server stop+start (proxied via primary LMS agent)
    GET  /api/lmstudio/server/log       — recent LMS server log (proxied via primary LMS agent)
    POST /api/lmstudio/load             — load model in LM Studio
    POST /api/lmstudio/unload           — unload model from LM Studio
    POST /api/lmstudio/download         — start LM Studio model download (proxied via primary LMS agent)
    GET  /api/layout                    — load card layout JSON
    POST /api/layout                    — save card layout JSON
    GET  /proxy/llmchat/<path>          — reverse proxy for LLM Chat UI
    GET  /llm/log                       — standalone llama log viewer page
================================================================================
"""

# Defer annotation evaluation on every Python version. Without this, bare
# names in module-level annotations (Any, Optional, etc.) must be imported
# before the line they appear on — Python 3.14+ defers by default (PEP 649),
# pre-3.14 does not, and that split between the dev box (3.14) and the prod
# box (3.13) bit us once already.
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any  # Required pre-Python 3.14; 3.14+ defers annotation eval (PEP 649).

# ---------------------------------------------------------------------------
# Bootstrap — make the shared schema/loader importable.
# The systemd unit launches this file directly, so Python only puts
# backend/ on sys.path. Add the repo root (/opt/llm-systems-manager) so the
# top-level `config` package resolves. The manager lives at:
#   <repo_root>/llm-systems-manager/backend/llm-systems-manager.py
# so the repo root is three parents up from __file__.
# ---------------------------------------------------------------------------
_REPO_ROOT_PATH = Path(__file__).resolve().parents[2]
_REPO_ROOT = str(_REPO_ROOT_PATH)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config.unified_config import settings, CONFIG_PATH  # noqa: E402

import logging
import logging.handlers

# ---------------------------------------------------------------------------
# Logging configuration — sourced from settings.paths + settings.logging.
# Per-service override: settings.manager.log_level wins over [logging].level.
# ---------------------------------------------------------------------------
LOG_DIR   = settings.paths.log_dir
LOG_FILE  = os.path.join(LOG_DIR, "llm-systems-manager.log")

os.makedirs(LOG_DIR, exist_ok=True)

_log_formatter = logging.Formatter(
    settings.logging.format,
    datefmt=settings.logging.datefmt,
)

log = logging.getLogger("llm-systems-manager")
log.setLevel(getattr(logging, settings.manager.log_level.upper(), logging.INFO))

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
log.addHandler(_console_handler)

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=settings.paths.log_max_bytes,
    backupCount=settings.paths.log_backup_count,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)
try:
    log.addHandler(_file_handler)
except PermissionError:
    # If we can't write to /var/log, log silently — don't crash the app
    pass

import re
import urllib.parse
import signal

import requests
import socket
from flask import Flask, jsonify, request as flask_request, Response


_HOSTNAME = socket.gethostname()


def _local_hostname() -> str:
    """Identifies which server produced a metric — flows through to the alarm
    engine so alerts/dropdowns can show the originating device."""
    return _HOSTNAME

# ---------------------------------------------------------------------------
# Version — single source of truth. Update this string only; the startup
# banner reads it. Bump suffix (-1, -2, …) for same-day iterations; roll
# the date for a new day's first change.
# ---------------------------------------------------------------------------
__version__ = "v2026.06.25-7"

# Wall-clock at first import (Cheroot main process); the shutdown banner
# reads it for the uptime line.
_startup_ts: float = time.time()

# Sibling-module imports: provider_state.STORE holds per-(provider, agent_id)
# telemetry; providers registers ProviderSpecs at import time.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import provider_state  # type: ignore[import-not-found]  # noqa: E402  # leaf, no cycle
from _best_effort import best_effort  # type: ignore[import-not-found]  # noqa: E402  # leaf, no cycle
from _bench_replay import BenchReplayBuffer  # type: ignore[import-not-found]  # noqa: E402  # leaf, no cycle
import providers       # type: ignore[import-not-found]  # noqa: E402,F401  # side-effect: registers specs
import stream_pool     # type: ignore[import-not-found]  # noqa: E402  # leaf, no cycle
import stream_health   # type: ignore[import-not-found]  # noqa: E402  # leaf, no cycle
import sse_daemon     # type: ignore[import-not-found]  # noqa: E402  # leaf, lazy-aiohttp

# Cheroot servers (HTTP + TLS), appended as each binds — read by stream_health
# for live worker-thread + backlog counts.
_cheroot_servers: list = []
import model_profiles  # type: ignore[import-not-found]  # noqa: E402  # leaf, no cycle


def _patch_cheroot_flush_noise() -> None:
    """Quiet 'Exception ignored' tracebacks from cheroot's BufferedWriter on
    Python 3.13+.

    When a Cheroot connection closes, the underlying socket goes away but the
    BufferedWriter wrapper survives until GC. Python 3.13 made
    io.BufferedWriter.__del__ flush more aggressively on collection, so on
    cleanup it calls self.raw.write() against the dead fd → OSError(EBADF).
    Because the error fires inside __del__, the interpreter prints the full
    traceback to stderr and systemd captures it as log spam (24+ per day on
    a normally-loaded dashboard).

    Upstream cheroot/makefile.py:_flush_unlocked only catches BlockingIOError.
    We widen the catch to OSError and discard the buffered bytes, which are
    unrecoverable anyway because the peer already disconnected. Revisit if
    cheroot ships a fix upstream — at that point this monkey-patch is dead
    weight and can be removed in one revert.
    """
    try:
        from cheroot import makefile as _cheroot_makefile
    except ImportError:
        return
    _orig = _cheroot_makefile.BufferedWriter._flush_unlocked

    def _safe_flush_unlocked(self):
        try:
            _orig(self)
        except OSError:
            self._write_buf = bytearray()

    _cheroot_makefile.BufferedWriter._flush_unlocked = _safe_flush_unlocked


_patch_cheroot_flush_noise()

# ---------------------------------------------------------------------------
# Paths
#
#   _REPO_ROOT_PATH  → /opt/llm-systems-manager                (data/, agent/, llm-systems-alarm-engine/)
#   _PKG_DIR         → /opt/llm-systems-manager/llm-systems-manager  (this package's backend/ + frontend/)
# ---------------------------------------------------------------------------
_PKG_DIR   = Path(__file__).resolve().parent.parent
STATIC_DIR = _PKG_DIR / "frontend"
DATA_DIR   = _REPO_ROOT_PATH / "data"
DB_PATH    = DATA_DIR / "metrics.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# SQLite setup
# ---------------------------------------------------------------------------
import threading as _threading

_db_tls = _threading.local()

def get_db():
    """Return a per-thread SQLite connection. metrics.db now holds only the
    tiny model_benchmarks table; WAL keeps reads/writes concurrent across
    Flask threads, busy_timeout absorbs lock contention. Other perf-tuning
    PRAGMAs were dropped — they were pointless against a 16 KB DB."""
    conn = getattr(_db_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _db_tls.conn = conn
    return conn

def init_db():
    """Create non-metric SQLite tables. Metric data lives in InfluxDB via the alarm engine."""
    conn = get_db()
    # Migrate a pre-multi-agent table (UNIQUE(model_id), no agent_id) to the
    # per-agent shape (UNIQUE(model_id, agent_id)) so two agents keep separate
    # results for a same-named model. Existing rows become legacy agent_id=''.
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_benchmarks'"
    ).fetchone()
    if existing:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(model_benchmarks)").fetchall()]
        if "agent_id" not in cols:
            conn.executescript("""
                ALTER TABLE model_benchmarks RENAME TO _model_benchmarks_old;
                CREATE TABLE model_benchmarks (
                    id          INTEGER PRIMARY KEY,
                    model_id    TEXT NOT NULL,
                    agent_id    TEXT NOT NULL DEFAULT '',
                    avg_gen_tps REAL,
                    avg_ppt_tps REAL,
                    bench_tool  TEXT,
                    switches    TEXT,
                    ts          TEXT,
                    UNIQUE(model_id, agent_id)
                );
                INSERT INTO model_benchmarks
                    (model_id, agent_id, avg_gen_tps, avg_ppt_tps, bench_tool, switches, ts)
                    SELECT model_id, '', avg_gen_tps, avg_ppt_tps, bench_tool, switches, ts
                    FROM _model_benchmarks_old;
                DROP TABLE _model_benchmarks_old;
            """)
            log.info("model_benchmarks migrated to per-agent schema (model_id, agent_id)")
        if "avg_pg_tps" not in cols:
            conn.execute("ALTER TABLE model_benchmarks ADD COLUMN avg_pg_tps REAL")
    conn.execute("""
            CREATE TABLE IF NOT EXISTS model_benchmarks (
                id          INTEGER PRIMARY KEY,
                model_id    TEXT NOT NULL,
                agent_id    TEXT NOT NULL DEFAULT '',
                avg_gen_tps REAL,
                avg_ppt_tps REAL,
                avg_pg_tps  REAL,
                bench_tool  TEXT,
                switches    TEXT,
                ts          TEXT,
                UNIQUE(model_id, agent_id)
            )
        """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bench_model ON model_benchmarks(model_id, agent_id)")
    conn.commit()

init_db()

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
# Heartbeat counters / last-emit timestamps. Each long-lived background loop
# emits one INFO line per 60s wall-clock so a tail -F shows continued liveness
# without flooding the file at the 2s collection cadence.
# Forward-success counters reset each heartbeat window
# Process startup epoch — exposed via /api/admin/system-health for the
# admin tab's "uptime" column.
_manager_startup_ts = time.time()

# Silence the cheroot/cpython 3.14 finalizer noise that appears as
# "Exception ignored while calling deallocator … OSError: [Errno 9]
# Bad file descriptor" in journalctl. It fires when a client (browser /
# agent / SSE consumer) disconnects mid-stream: the socket FD is closed,
# then the buffered IO wrapper's __del__ tries to flush stale bytes to
# the dead FD. The bytes were never going to be delivered — the client
# already left. Drop only this exact case; everything else still goes to
# the default unraisable hook so real bugs aren't hidden.
_default_unraisable_hook = sys.unraisablehook
def _filter_socket_cleanup_noise(args):
    exc = args.exc_value
    if (isinstance(exc, OSError) and exc.errno == 9
            and "_flush_unlocked" in (args.err_msg or "")):
        return
    obj = args.object
    if (isinstance(exc, OSError) and exc.errno == 9
            and obj is not None
            and type(obj).__module__.startswith(("_pyio", "io", "socket", "cheroot"))):
        return
    _default_unraisable_hook(args)
sys.unraisablehook = _filter_socket_cleanup_noise

# Anti-flap state for /api/admin/system-health's alarm-engine probe.
_ae_health_state: dict[str, int] = {"consecutive_failures": 0}

# Dynamic interval state — driven by primary-llama-agent's reported
# llama_state (awake/sleeping) AND LMS activity. Values come from the
# unified config:
#   [manager] poll_interval        — slow (idle) interval, default 30 s
#   [manager] fast_poll_interval   — fast (busy) interval,  default 2 s
_current_interval  = settings.manager.poll_interval   # seconds
_interval_lock     = threading.Lock()
_lms_active        = False   # True when LMS has a non-IDLE model in ps
_lms_active_lock   = threading.Lock()
_llama_awake       = False
_llama_awake_lock  = threading.Lock()

# Manual interval override — None = auto, positive int = forced seconds (persists until reset)
_interval_override      = None
_interval_override_lock = threading.Lock()

def get_interval() -> int:
    """Return current poll interval. Manual override takes precedence over auto."""
    # Read both variables under a single lock to prevent TOCTOU race where
    # _interval_override is None -> cleared by another thread -> _current_interval
    # is returned even though user set a manual override just microseconds later.
    with _interval_lock:
        with _interval_override_lock:
            override = _interval_override
        if override is not None:
            return override
        return _current_interval

def set_interval(perf_level: str, lms_active: bool | None = None,
                 llama_awake: bool | None = None):
    global _current_interval
    gpu_wants_fast = (perf_level == "auto")
    if lms_active is None:
        with _lms_active_lock:
            lms_active = _lms_active
    if llama_awake is None:
        with _llama_awake_lock:
            llama_awake = _llama_awake
    # Cached _lms_active goes stale when no LMS agent has pushed in >15s.
    # Re-check against the per-agent store as the authoritative truth.
    if lms_active:
        lms_active = _any_lms_busy()
    new = (settings.manager.fast_poll_interval
           if (gpu_wants_fast or lms_active or llama_awake)
           else settings.manager.poll_interval)
    with _interval_lock:
        if new != _current_interval:
            reason = []
            if gpu_wants_fast: reason.append(f"GPU perf={perf_level}")
            if lms_active:     reason.append("LMS active")
            if llama_awake:    reason.append("llama awake")
            log.info(f"Poll interval: {_current_interval}s → {new}s"
                     + (f" ({', '.join(reason)})" if reason else " (idle)"))
        _current_interval = new

def set_lms_active(active: bool):
    """Called by receive_lmstudio_metrics when agent data arrives."""
    global _lms_active
    with _lms_active_lock:
        prev = _lms_active
        _lms_active = active
    if active != prev:
        log.info(f"LMS active state: {'idle' if prev else 'busy'} → {'busy' if active else 'idle'}")
    set_interval("", lms_active=active)


def set_llama_awake(awake: bool):
    global _llama_awake
    with _llama_awake_lock:
        prev = _llama_awake
        _llama_awake = awake
    if awake != prev:
        log.info(f"llama awake state: {'sleeping' if prev else 'awake'} → {'awake' if awake else 'sleeping'}")
    set_interval("", llama_awake=awake)


# ---------------------------------------------------------------------------
# Main metric collection
# ---------------------------------------------------------------------------
# Hostnames that are only meaningful on the local machine. Used wherever
# a URL has to be rewritten for a remote consumer (browser, agent) —
# sending a loopback host to a remote client would resolve to the
# client's own box. 0.0.0.0 (the "all interfaces" bind) and "" (empty
# host after parse failure) are bundled in because they're equally
# unhelpful as broadcast values.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def _self_host_names() -> "frozenset[str]":
    """Loopback hosts plus this manager's own hostname/FQDN — every name that
    means 'this box'. A co-located AE configured with the manager's hostname
    (not just localhost) still resolves to here, so its advertised URL must be
    rewritten to an address the remote agent can actually reach."""
    names = set(_LOOPBACK_HOSTS)
    for _fn in (socket.gethostname, socket.getfqdn):
        try:
            h = (_fn() or "").lower()
        except Exception:
            continue
        if h:
            names.add(h)
            names.add(h.split(".", 1)[0])
    return frozenset(names)


_SELF_HOSTS = _self_host_names()


def _rewrite_loopback_host(url: str, fallback_host: str) -> str:
    """If url's host points at this manager box (loopback or our own
    hostname/FQDN), return url with the host swapped for fallback_host — the
    address the client used to reach the manager. Otherwise unchanged. Used by
    the heartbeat ack (rewrite for a remote agent) and the alarm-engine
    WebSocket URL builder (rewrite for the browser). Handles IPv6
    bracket syntax via urlsplit (e.g. `[::1]:5000`)."""
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
    except ValueError as e:
        log.debug("rewrite_loopback_host: urlsplit failed for %r: %s", url, e)
        return url
    host = (parts.hostname or "").lower()
    if host not in _SELF_HOSTS or not fallback_host:
        return url
    netloc = f"{fallback_host}:{parts.port}" if parts.port else fallback_host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _request_host_no_port() -> str:
    """flask_request.host without the port. IPv6 hosts come back from
    Flask in `[::1]:port` form — urlsplit handles the brackets so a
    naive split(":") doesn't shear off the leading `[`."""
    raw = (flask_request.host or "")
    if not raw:
        return ""
    from urllib.parse import urlsplit
    try:
        return (urlsplit(f"//{raw}").hostname or "")
    except ValueError:
        return raw.split(":", 1)[0]


# ---------------------------------------------------------------------------
# Alarm Engine — reachable for read-only queries from /api/admin/system-health,
# /api/history, and /api/alert.
# ---------------------------------------------------------------------------
_alarm_engine_url = os.environ.get("ALARM_ENGINE_URL", settings.manager.alarm_engine_url)
# When [alarm_engine].tls_enabled is on, the AE serves HTTPS only — flip our
# outbound URL from http:// to https:// so the proxy / probes / history fan-out
# all hit the right scheme, and route them through a Session whose `verify`
# points at the internal CA (the AE's cert is signed by it).
# IMPORTANT activation order: update the AE systemd unit FIRST (`update.sh`),
# then turn this flag on — otherwise the launcher that actually serves TLS
# never runs and these https calls hit a plain-HTTP socket → 502.
_AE_CA_PATH = str((DATA_DIR / "internal-ca.crt").resolve())
if (bool(settings.alarm_engine.tls_enabled)
        and _alarm_engine_url and _alarm_engine_url.startswith("http://")):
    _alarm_engine_url = "https://" + _alarm_engine_url[len("http://"):]
    log.info("AE TLS on — flipped outbound _alarm_engine_url to %s", _alarm_engine_url)
_ae_session = requests.Session()
if bool(settings.alarm_engine.tls_enabled):
    _ae_session.verify = _AE_CA_PATH


def install_topology() -> dict:
    """One-stop snapshot of this host's deployment shape.

    Returns:
      {
        "ae_local_disk":  bool,   # llm-systems-alarm-engine/ deployed on disk
        "ae_local_unit":  bool,   # llm-systems-alarm-engine.service installed
        "ae_local_url":   bool,   # configured _alarm_engine_url points at localhost
        "split":          bool,   # any signal says the AE is somewhere else
      }

    "split" is the conservative OR: any one disagreement (config points
    remote, or local AE bits absent) flips it true. Used by the various
    proxy / frontend-injection sites so they stop ad-hoc-checking on
    their own. None of these helpers raise — they all degrade to False.
    """
    from urllib.parse import urlsplit
    ae_local_disk = False
    with best_effort("install topology: probe AE frontend dir"):
        ae_local_disk = (_REPO_ROOT_PATH / "llm-systems-alarm-engine"
                                         / "frontend").is_dir()
    ae_local_unit = os.path.isfile(
        "/etc/systemd/system/llm-systems-alarm-engine.service"
    )
    ae_local_url = False
    if _alarm_engine_url:
        with best_effort("install topology: classify AE url host"):
            host = (urlsplit(_alarm_engine_url).hostname or "").lower()
            ae_local_url = host in _LOOPBACK_HOSTS
    split = not (ae_local_disk and ae_local_unit and ae_local_url)
    return {
        "ae_local_disk": ae_local_disk,
        "ae_local_unit": ae_local_unit,
        "ae_local_url":  ae_local_url,
        "split":         split,
    }

# ---------------------------------------------------------------------------
# Background collector thread — dynamic sleep
# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
# Cap request body size — agent payloads are ~30KB, layout.json ~5KB.
# Rejects runaway/malicious POSTs before they reach request handlers.
app.config["MAX_CONTENT_LENGTH"] = settings.manager.uploads.max_content_length

# ---------------------------------------------------------------------------
# Request-logging middleware
#   - Mutating methods (POST/PUT/DELETE/PATCH): always logged at INFO
#   - GETs: logged only if duration >500ms or status >=400 (keeps the 2s
#     /api/metrics poll out of the log)
#   - Status >=400: logged at WARNING with traceback if exception
#   - Unhandled exceptions: caught by errorhandler → ERROR with stack
# ---------------------------------------------------------------------------
from flask import request as _flask_request, g as _flask_g

@app.before_request
def _log_request_start():
    _flask_g._t0 = time.perf_counter()

@app.after_request
def _log_request_end(resp):
    try:
        t0 = getattr(_flask_g, "_t0", None)
        dur_ms = (time.perf_counter() - t0) * 1000 if t0 else 0.0
        method, path, status = _flask_request.method, _flask_request.path, resp.status_code
        is_mut  = method in ("POST", "PUT", "DELETE", "PATCH")
        is_slow = dur_ms > 500
        is_err  = status >= 400
        if is_err:
            # Carry the client identity on errors so "who's the 401 source?"
            # questions answer themselves without an ss correlation. UA is
            # truncated because some scrapers ship multi-kilobyte strings.
            remote = _flask_request.remote_addr or "?"
            ua = (_flask_request.headers.get("User-Agent") or "")[:80]
            log.warning("%s %s -> %s (%.0fms) from=%s ua=%r",
                        method, path, status, dur_ms, remote, ua)
        elif is_mut or is_slow:
            log.info("%s %s -> %s (%.0fms)", method, path, status, dur_ms)
    except Exception:
        # Never let logging break a response
        log.debug("request-log hook failed", exc_info=True)
    return resp

@app.errorhandler(Exception)
def _log_unhandled(e):
    # Werkzeug HTTPExceptions carry their own status code; let them through.
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception("unhandled exception in %s %s", _flask_request.method, _flask_request.path)
    return jsonify({"ok": False, "error": "internal server error"}), 500

# ---------------------------------------------------------------------------
# PTY terminal sessions — every route + helper lives in the dedicated
# `terminal` module (Tier 3 / PR M3). Main no longer holds any terminal
# state. It's wired further down via `terminal.register_routes(app, ctx)`
# alongside auth + agent_registry, after the shared Context is built.
# ---------------------------------------------------------------------------

@app.teardown_request
def _close_db(exc):
    """Close the per-thread SQLite connection after every request.
    Flask creates a new thread per request in threaded mode; without this each
    thread's connection (and its 3 WAL FDs) would leak until GC runs."""
    conn = getattr(_db_tls, "conn", None)
    if conn is not None:
        with best_effort("teardown: close per-thread sqlite conn", log=log):
            conn.close()
        _db_tls.conn = None

_INITIAL_HIDE_IDS = (
    # (id, capability-key — element is hidden when the cap is missing)
    ("tabBtnOverall",         "either"),  # LLM Overall — needs llama OR lms
    ("tabBtnLlmControl",      "either"),
    ("serverStateBanner",     "llama"),   # LLCPP pill
    ("lmsStateBanner",        "lms"),
    ("subTabBtnDashLlamacpp", "llama"),
    ("subTabBtnDashLmstudio", "lms"),
    ("subTabBtnLlmLlamacpp",  "llama"),
    ("subTabBtnLlmLmstudio",  "lms"),
)

@app.route("/")
def index():
    """Serves index.html with conditional tabs/pills pre-hidden inline so the
    browser paints the correct state immediately — checkConfig() polling
    continues to handle dynamic updates while the page is open. Avoids the
    visible-then-hidden flash on fresh installs with no approved agents."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    visible = agent_registry.approved_agent_caps()
    # Inject agent ids into the page so client code can scope alarm-engine
    # catalog/history reads by agent (resolved to a host server-side), never
    # by a browser-held hostname. json.dumps + `</` → `<\/` keeps weird
    # values from breaking out of the inline script block.
    def _safe_js(v):
        return json.dumps(v).replace("</", "<\\/")
    # Same alarm-engine WS URL the AE iframe gets via _inject_alarm_ws_url —
    # the manager frontend's toast bus needs it too, otherwise it falls back
    # to a hardcoded ws://<host>:8081/ws that breaks the moment AE TLS is on
    # (because the AE then only speaks wss with an internal-CA cert).
    globals_js = (
        f"window.__MGR_AGENT={_safe_js(agent_registry.self_agent_id())};"
        f"window.__LMS_AGENT={_safe_js(agent_registry.default_agent_id_for('lms'))};"
        f"window.__AE_WS_URL__={_safe_js(proxies.ae_ws_url_for_browser())};"
    )
    html = html.replace("</head>", f"<script>{globals_js}</script>\n</head>", 1)
    # Brand palette → :root vars consumed by the header logo, so the logged-in
    # dashboard matches the login page's chosen palette ([manager.branding]).
    html = html.replace(
        "</head>", f"<style>{_brand_css_vars(_brand_palette())}</style>\n</head>", 1)
    for el_id, cap in _INITIAL_HIDE_IDS:
        if visible[cap]:
            continue
        # Inject display:none on the opening tag. Two cases: element has an
        # existing style attribute (append `;display:none` to its value) or it
        # doesn't (add a new style attribute). The pre-element regex captures
        # the tag opener through the id attribute so we anchor only on our
        # known IDs and ignore everything else.
        existing = re.sub(
            rf'(<[a-zA-Z]+[^>]*\bid="{re.escape(el_id)}"[^>]*\bstyle=")([^"]*)(")',
            r'\1\2;display:none\3',
            html,
            count=1,
        )
        if existing != html:
            html = existing
            continue
        html = re.sub(
            rf'(<[a-zA-Z]+[^>]*\bid="{re.escape(el_id)}")',
            r'\1 style="display:none"',
            html,
            count=1,
        )
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/api/metrics")
def get_latest():
    return jsonify(_llama_sample_for_request())

# Maps the legacy /api/history row-field name (what the frontend pushes into
# Chart.js datasets) to the (source, metric_name) pair the alarm engine
# actually stores. Everything now lives under source="system" except
# llama (own source) and the UPS (not currently reporting). Drift here
# silently empties the history ring — verify after agent renames by running
# tools/llm-systems-manager_smoke_test.sh (it checks the ring).
_HISTORY_LEGACY_FIELD_MAP = [
    # (alarm-engine source, alarm-engine metric_name,        legacy field name)
    # Core OS/hardware (all under source="system" after the agent rename)
    ("system",  "cpu_total",                                  "cpu_total"),
    ("system",  "ram_percent",                                "ram_percent"),
    ("system",  "gpu_gpu_util_percent",                       "gpu_util"),
    ("system",  "gpu_vram_usage_percent",                     "gpu_vram"),
    ("system",  "gpu_temperature_c",                          "gpu_temp"),
    ("system",  "gpu_power_watts",                            "gpu_power"),
    ("system",  "net_bytes_sent_per_s",                       "net_sent"),
    ("system",  "net_bytes_recv_per_s",                       "net_recv"),
    ("system",  "disk_io_read_bytes_per_sec",                 "io_read"),
    ("system",  "disk_io_write_bytes_per_sec",                "io_write"),
    ("system",  "ups_percent",                                "ups_percent"),
    # Disk usage % for the two mounts plotted on the disk-usage chart.
    # The third tracked mount (/media/USB) was retired — the per-mount
    # bar list still iterates m.disk so any mount continues to show as
    # a bar in the current snapshot.
    ("system",  "disk_root_percent",                          "disk_root_pct"),
    ("system",  "disk_mnt_iscsi_percent",                     "disk_iscsi_pct"),
    # liquidctl AIO + Corsair PSU — names contain spaces because the
    # agent flattens liquidctl's nested dict keys verbatim; the REST
    # validator on the alarm engine was updated to allow spaces so these
    # round-trip cleanly. urlencoded by _fetch_history_series.
    ("system",  "liquidctl_aio_Liquid temperature_value",  "aio_temp"),
    ("system",  "liquidctl_psu_Total power output_value",     "psu_out"),
    ("system",  "liquidctl_psu_Estimated input power_value",  "psu_in"),
    # Llama-server stats aren't ingested into the alarm engine yet — agents
    # push them straight to the manager's /api/remote/host-metrics. Leave
    # placeholders so the legacy field names exist; they stay None until
    # the agent → AE wiring lands.
    ("llama",   "tokens_per_second",                          "llama_tps"),
    ("llama",   "prompt_tokens_per_second",                   "llama_pps"),
    ("llama",   "n_tokens_max",                               "llama_ctx"),
    ("llama",   "total_tokens_generated",                     "llama_gen_tokens"),
]


# ---------------------------------------------------------------------------
# /api/history — 60-minute in-memory ring buffer.
#
# A background thread queries the alarm engine for the last 60 min of every
# legacy field every HISTORY_REFRESH_INTERVAL_S seconds, merges the points
# into a single sorted row list, and caches it. /api/history reads the cached
# list with zero outbound I/O on the hot path.
#
# Memory cost: ~12 fields × 720 rows × ~80 bytes ≈ <1 MB.
# Staleness ceiling: HISTORY_REFRESH_INTERVAL_S (default 5 s) — invisible at
# the dashboard's 2 s redraw cadence.
# ---------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor as _HistoryExecutor

HISTORY_WINDOW_MINUTES = settings.manager.history.window_minutes
HISTORY_REFRESH_INTERVAL_S = settings.manager.history.refresh_interval_s
HISTORY_FETCH_LIMIT = settings.manager.history.fetch_limit

_history_rows: list[dict] = []
_history_lock = _threading.Lock()
_history_pool = _HistoryExecutor(max_workers=min(12, len(_HISTORY_LEGACY_FIELD_MAP)))

# Long-window response cache: keyed by (since_minutes, limit). The default
# /api/history hot path (<= window_minutes) is served from the in-memory
# ring above; longer windows (4 h, 24 h, 7 d…) fan out to the alarm engine
# and then to InfluxDB, which is expensive enough that bench mode at 16-
# way concurrency could saturate Influx with 12 parallel queries × N
# concurrent UI clients. A short TTL on the merged response collapses
# repeat clients onto one upstream call.
_HISTORY_LONG_TTL_S: float = 30.0
_history_long_cache: dict[tuple[int, int], tuple[float, list[dict]]] = {}
_history_long_lock = _threading.Lock()

# Scoped history cache for ?agent= / ?fleet= requests. Keyed by
# (scope, since, limit) so concurrent pickers of the same agent/fleet collapse
# onto one upstream fan-out. Short TTL — same rationale as the long cache.
_HISTORY_SCOPED_TTL_S: float = 5.0
_history_scoped_cache: dict[tuple, tuple[float, list[dict]]] = {}
_history_scoped_lock = _threading.Lock()


def _history_scoped(scope: tuple, since_minutes: int, limit: int,
                    builder) -> list[dict]:
    """TTL-cached wrapper for a scoped (agent/fleet) history build."""
    key = (scope, since_minutes, limit)
    now_ts = time.time()
    with _history_scoped_lock:
        cached = _history_scoped_cache.get(key)
        if cached and (now_ts - cached[0]) < _HISTORY_SCOPED_TTL_S:
            return cached[1]
    rows = builder()
    with _history_scoped_lock:
        _history_scoped_cache[key] = (now_ts, rows)
        # Bound the cache — at most ~16 distinct scope/window combos.
        if len(_history_scoped_cache) > 16:
            oldest = min(_history_scoped_cache.items(), key=lambda kv: kv[1][0])[0]
            _history_scoped_cache.pop(oldest, None)
    return rows

def _fetch_history_series(base: str, source: str, metric_name: str, field: str,
                          since_minutes: int, limit: int,
                          hostname: "str | None" = None):
    # URL-encode the path components — some agent-emitted metric names
    # contain spaces (e.g. liquidctl_aio_Liquid temperature_value), and
    # `requests` doesn't auto-quote path parts.
    src_enc = urllib.parse.quote(source, safe="")
    name_enc = urllib.parse.quote(metric_name, safe="")
    params = {"since_minutes": since_minutes, "limit": limit}
    if hostname:
        params["hostname"] = hostname
    try:
        r = _ae_session.get(
            f"{base}/api/alarm/metrics/{src_enc}/{name_enc}",
            params=params,
            timeout=5,
        )
        if r.status_code != 200:
            return field, []
        return field, r.json()
    except Exception as e:
        log.debug(f"history fetch failed for {source}/{metric_name}: {e}")
        return field, []

def _build_history_rows(since_minutes: int, limit: int,
                        hostname: "str | None" = None) -> list[dict]:
    """Fan out parallel reads against the alarm engine and merge into rows.
    When hostname is set, every series is filtered to that one host (the AE's
    /api/alarm/metrics/<source>/<name> endpoint takes a hostname query param)."""
    if not _alarm_engine_url:
        return []
    base = _alarm_engine_url.rstrip("/")
    rows_by_ts: dict[str, dict] = {}
    futures = [
        _history_pool.submit(_fetch_history_series, base, src, name, field,
                             since_minutes, limit, hostname)
        for src, name, field in _HISTORY_LEGACY_FIELD_MAP
    ]
    for fut in futures:
        field, points = fut.result()
        for p in points:
            ts = p.get("timestamp")
            if not ts:
                continue
            rows_by_ts.setdefault(ts, {"ts": ts})[field] = p.get("value")
    return sorted(rows_by_ts.values(), key=lambda r: r["ts"])


# Per-field aggregation for fleet history: how to combine the same field
# across hosts at one time bucket. Defaults to "mean" for anything unlisted.
_FLEET_FIELD_AGG: dict[str, str] = {
    "cpu_total": "mean", "ram_percent": "mean", "ups_percent": "mean",
    "gpu_util": "mean",
    "gpu_temp": "max", "gpu_vram": "max", "aio_temp": "max",
    "disk_root_pct": "max", "disk_iscsi_pct": "max",
    "gpu_power": "sum", "psu_out": "sum", "psu_in": "sum",
    "net_sent": "sum", "net_recv": "sum", "io_read": "sum", "io_write": "sum",
    "llama_tps": "sum", "llama_pps": "sum", "llama_ctx": "sum",
    "llama_gen_tokens": "sum",
}
_FLEET_BUCKET_S = 5.0


def _bucket_iso(iso_ts: str, bucket_s: float) -> str:
    """Round an ISO timestamp to the nearest bucket_s seconds so points from
    different hosts (which tick independently) line up for aggregation."""
    try:
        t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return iso_ts
    bucketed = round(t.timestamp() / bucket_s) * bucket_s
    return datetime.fromtimestamp(bucketed, tz=timezone.utc).isoformat()


def _agg_values(kind: str, vals: list) -> "float | None":
    nums = [v for v in vals if isinstance(v, (int, float))]
    if not nums:
        return None
    if kind == "sum":
        return sum(nums)
    if kind == "max":
        return max(nums)
    return sum(nums) / len(nums)  # mean


def _build_fleet_history_rows(provider: str, since_minutes: int,
                              limit: int) -> list[dict]:
    """Aggregate per-host history across every approved+capable agent of a
    provider. Per host+bucket we keep the last value, then combine across
    hosts with each field's _FLEET_FIELD_AGG function."""
    if not _alarm_engine_url:
        return []
    spec = providers.get(provider)
    cap_key = spec.capability_key if spec else provider
    data = agent_registry.load_agents()
    hosts = sorted({
        (a.get("hostname") or "").strip()
        for a in (data.get("agents") or {}).values()
        if a.get("status") == "approved"
        and (a.get("capabilities") or {}).get(cap_key)
        and (a.get("hostname") or "").strip()
    })
    if not hosts:
        return []
    base = _alarm_engine_url.rstrip("/")
    # accum[field][bucket_ts] = list of one value per host (host-deduped).
    accum: dict[str, dict[str, list]] = {}
    futures = [
        _history_pool.submit(_fetch_history_series, base, src, name, field,
                             since_minutes, limit, host)
        for host in hosts
        for src, name, field in _HISTORY_LEGACY_FIELD_MAP
    ]
    # Each future returns its own field, so one value per host lands in
    # accum[field][bucket] regardless of iteration order.
    for fut in futures:
        f, points = fut.result()
        hb: dict[str, object] = {}
        for p in points:
            ts = p.get("timestamp")
            if not ts:
                continue
            hb[_bucket_iso(ts, _FLEET_BUCKET_S)] = p.get("value")
        for bts, val in hb.items():
            accum.setdefault(f, {}).setdefault(bts, []).append(val)
    rows_by_ts: dict[str, dict] = {}
    for field, buckets in accum.items():
        agg = _FLEET_FIELD_AGG.get(field, "mean")
        for bts, vals in buckets.items():
            v = _agg_values(agg, vals)
            if v is not None:
                rows_by_ts.setdefault(bts, {"ts": bts})[field] = v
    return sorted(rows_by_ts.values(), key=lambda r: r["ts"])

_OFFLINE_SWEEP_INTERVAL_S = 5.0


def _offline_sweep_loop():
    """Background sweep that fires the True→False latch transition for any
    agent whose last_seen exceeds the provider's online_threshold_s. Closes
    the pre-PR1 gap where LMS offline logs only fired when /api/lmstudio/metrics
    was polled. Also de-activates set_lms_active when the fleet goes idle."""
    while not _shutting_down:
        try:
            now = time.time()
            for prov in providers.names():
                spec = providers.get(prov)
                threshold = spec.online_threshold_s if spec else 30.0
                for aid, wrap in provider_state.STORE.all_for(prov).items():
                    last_seen = float(wrap.get("last_seen") or 0)
                    if not last_seen:
                        continue
                    age = now - last_seen
                    if age <= threshold:
                        continue
                    if provider_state.STORE.mark_offline(prov, aid):
                        log.warning("%s agent offline — last seen %.0fs ago [agent %s]",
                                    prov, age, aid[:8])
            # Fleet-aware lms_active demotion: if no LMS agent is fresh+busy,
            # flip the dynamic poll interval back to slow.
            if not _any_lms_busy():
                set_lms_active(False)
        except Exception as e:
            log.warning("offline sweep iteration failed: %s", e)
        # Sleep in short slices so shutdown wakes within ~0.5s.
        slept = 0.0
        while slept < _OFFLINE_SWEEP_INTERVAL_S and not _shutting_down:
            time.sleep(0.5)
            slept += 0.5


def _history_refresher_loop():
    """Refill the in-memory 60-min ring every HISTORY_REFRESH_INTERVAL_S.

    A transient alarm-engine hiccup (e.g. a long Flux query holding the
    event loop, or a cache sweep in progress) can make the 12-parallel
    fan-out come back with zero rows. The previous version overwrote the
    ring with that empty result, blanking the dashboard for one full
    refresh cycle. We now keep the prior ring on empty fetches so a
    transient failure can't blank good data.
    """
    global _history_rows
    consecutive_empties = 0
    while True:
        try:
            rows = _build_history_rows(HISTORY_WINDOW_MINUTES, HISTORY_FETCH_LIMIT)
            with _history_lock:
                if rows:
                    _history_rows = rows
                    consecutive_empties = 0
                elif not _history_rows:
                    # Existing ring is also empty (cold start); accept
                    # the empty so the next-tick race-fix below can flip
                    # back to populated when the AE recovers.
                    _history_rows = rows
            if not rows:
                consecutive_empties += 1
                if consecutive_empties == 1:
                    log.info("history refresher: 0 rows this tick; keeping prior ring")
                elif consecutive_empties % 20 == 0:
                    log.warning(
                        "history refresher: still 0 rows after %d ticks "
                        "(~%ds); alarm engine may be misbehaving",
                        consecutive_empties,
                        consecutive_empties * int(HISTORY_REFRESH_INTERVAL_S),
                    )
        except Exception as e:
            log.debug("history refresher: %s", e)
        time.sleep(HISTORY_REFRESH_INTERVAL_S)

# Started below in __main__ — see the startup block at the bottom of the file.

@app.route("/api/history")
def get_history():
    """Serve metric history with a split data path:

      - since_minutes <= 60  →  in-memory ring (built by _history_refresher_loop).
        Hot path; no outbound I/O.
      - since_minutes  > 60  →  passthrough to the alarm engine, which queries
        InfluxDB live. Slower (1 round-trip × 12 parallel field-fetches) but
        unbounded by retention. Not cached because the use case is one-off
        (ad-hoc deep dives), and caching long windows would dwarf the ring.

    On cold start, if the ring is empty for an in-window request, fall back to
    a synchronous fetch so first paint doesn't see blank charts.
    """
    # The cold-start fallback below assigns to these module-level names,
    # which would otherwise make Python treat them as locals for the whole
    # function and raise UnboundLocalError on the read at line ~782.
    global _history_rows

    # Parse query params first — they determine which path we take.
    try:
        since_minutes = int(flask_request.args.get("since_minutes", HISTORY_WINDOW_MINUTES))
    except (TypeError, ValueError):
        since_minutes = HISTORY_WINDOW_MINUTES
    try:
        limit = int(flask_request.args.get("limit", 0))
    except (TypeError, ValueError):
        limit = 0
    # Guard against pathological inputs.
    if since_minutes < 1:
        since_minutes = HISTORY_WINDOW_MINUTES
    if limit < 0:
        limit = 0

    # ── Scoped: per-agent (?agent=) or fleet (?fleet=) history ──
    # These bypass the shared ring (which is default-agent-only) and fan out
    # host-filtered reads on demand, collapsed by a short TTL cache.
    agent_id = flask_request.args.get("agent")
    fleet = flask_request.args.get("fleet")
    if agent_id or fleet:
        if not _alarm_engine_url:
            return jsonify([])
        fetch_since = min(since_minutes, 43200)
        fetch_limit = min(limit, 10000) if limit > 0 else HISTORY_FETCH_LIMIT
        # fleet wins if both params are sent (the frontend never sends both).
        if fleet:
            if fleet not in providers.names():
                return jsonify({"ok": False, "error": f"unknown provider: {fleet}"}), 400
            rows = _history_scoped(
                ("fleet", fleet), fetch_since, fetch_limit,
                lambda: _build_fleet_history_rows(fleet, fetch_since, fetch_limit))
        else:
            # Host-wide, provider-agnostic: no capability filter (a host's full
            # metric set is valid history) and scoped by hostname, so distinct
            # agent_ids on one host share the series.
            agent = agent_registry.resolve_agent_by_id(agent_id)
            hostname = (agent or {}).get("hostname")
            if not hostname:
                return jsonify([])
            rows = _history_scoped(
                ("agent", hostname), fetch_since, fetch_limit,
                lambda: _build_history_rows(fetch_since, fetch_limit, hostname))
        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return jsonify(rows)

    # ── Beyond-window: passthrough to alarm engine / InfluxDB ──
    if since_minutes > HISTORY_WINDOW_MINUTES:
        if not _alarm_engine_url:
            return jsonify([])
        # The alarm engine caps since_minutes at 43200 (30d) and limit at 10000;
        # clamp before delegating so requests outside that range don't 422.
        alarm_since = min(since_minutes, 43200)
        # Default to the per-series cap; user-supplied limit is honored when
        # it fits. The alarm engine returns rows per series and the manager
        # merges them by timestamp, so this cap is per-field, not total.
        alarm_limit = min(limit, 10000) if limit > 0 else 10000

        # Per-(since, limit) TTL cache. A 30-s collapse keeps concurrent
        # clients from each firing the full 12-fanout against AE+Influx —
        # the bench saw 95/109 errors on this path because every parallel
        # caller was its own InfluxDB query.
        cache_key = (alarm_since, alarm_limit)
        now_ts = time.time()
        with _history_long_lock:
            cached = _history_long_cache.get(cache_key)
            if cached and (now_ts - cached[0]) < _HISTORY_LONG_TTL_S:
                rows = cached[1]
            else:
                cached = None

        if cached is None:
            rows = _build_history_rows(alarm_since, alarm_limit)
            with _history_long_lock:
                _history_long_cache[cache_key] = (now_ts, rows)

        if limit and len(rows) > limit:
            rows = rows[-limit:]
        return jsonify(rows)

    # ── In-window: serve from the ring ──
    with _history_lock:
        rows = _history_rows
    if not rows and _alarm_engine_url:
        rows = _build_history_rows(HISTORY_WINDOW_MINUTES, HISTORY_FETCH_LIMIT)
        # Only adopt the fetched rows if (a) the ring is still empty AND
        # (b) we actually got data. Otherwise leave the ring alone — a
        # background refresher tick might land a non-empty result first
        # and we'd race over each other otherwise.
        if rows:
            with _history_lock:
                if not _history_rows:
                    _history_rows = rows
    # Filter to the requested sub-window if the caller asked for less than 60m.
    if since_minutes < HISTORY_WINDOW_MINUTES:
        cutoff = time.time() - (since_minutes * 60)
        def _ts_after(r):
            ts = r.get("ts")
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                return True
            return t >= cutoff
        rows = [r for r in rows if _ts_after(r)]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return jsonify(rows)

@app.route("/api/llama-state")
def get_llama_state():
    """Lightweight endpoint polled every 2s. Reads state + model from the
    primary llama agent's most recent host-metrics push. Optional ?agent=
    targets a specific agent (PR1: only the primary writes to STORE so
    this is forward-compat plumbing)."""
    aid = flask_request.args.get("agent") or _primary_llama_agent_id()
    return jsonify(_build_llama_state_payload(aid))

@app.route("/api/llm/server/svcconfig")
def get_server_svcconfig():
    """Read and parse llama_server.service ExecStart args."""
    return proxies.proxy_to_primary("llama", "GET", "/llama/server/svcconfig")
@app.route("/api/llm/server/svcconfig", methods=["POST"])
def save_server_svcconfig():
    """Write updated ExecStart args back to llama_server.service, daemon-reload, optionally restart."""
    return proxies.proxy_to_primary("llama", "POST", "/llama/server/svcconfig",
                                json=flask_request.get_json(force=True) or {})
@app.route("/api/config")
def get_config():
    """Frontend bootstrap: poll interval, mode, proxy-tab visibility,
    agent-capability presence (drives LLM-related tab/pill visibility on
    fresh installs that have no llama or LMS agent approved yet)."""
    with _interval_override_lock:
        override = _interval_override
    caps = agent_registry.approved_agent_caps()
    return jsonify({
        "poll_interval":    get_interval(),
        "interval_mode":    "manual" if override is not None else "auto",
        "interval_override": override,
        "proxies": {
            "llm_chat":  proxies.resolve_proxy_target("llm_chat")  is not None,
            "openclaw":  proxies.resolve_proxy_target("openclaw")  is not None,
            "image_gen": proxies.resolve_proxy_target("image_gen") is not None,
        },
        "agents": {
            "llama_present": caps["llama"],
            "lms_present":   caps["lms"],
        },
    })

@app.route("/api/config/interval", methods=["POST"])
def set_interval_endpoint():
    """Set polling interval mode: auto (dynamic) or manual (fixed seconds)."""
    global _interval_override
    data = flask_request.get_json(force=True)
    mode = data.get("mode", "auto")
    if mode == "manual":
        val = int(data.get("value", 5))
        val = max(1, min(300, val))
        with _interval_override_lock:
            _interval_override = val
        log.info(f"Poll interval set to manual {val}s")
        return jsonify({"ok": True, "mode": "manual", "value": val})
    else:
        with _interval_override_lock:
            _interval_override = None
        log.info("Poll interval set to auto")
        return jsonify({"ok": True, "mode": "auto", "value": None})

@app.route("/api/alert")
def get_alert():
    latest = dict(_llama_sample_for_request())
    if not latest:
        return jsonify({"cpu": False, "cpu_cores": False,
                        "gpu_temp": False, "gpu_vram": False,
                        "disk": False, "ups": False})

    # CPU sustained > 95% — check last 30 seconds via alarm engine (InfluxDB-backed)
    cpu_sustained = False
    if _alarm_engine_url:
        with best_effort("interval: probe sustained CPU from AE", log=log):
            r = _ae_session.get(
                f"{_alarm_engine_url.rstrip('/')}/api/alarm/metrics/cpu/usage_percent",
                params={"since_minutes": 1, "limit": 100},
                timeout=2,
            )
            if r.status_code == 200:
                cutoff = time.time() - 30
                points = r.json()
                recent = []
                for p in points:
                    ts_raw = p.get("timestamp", "")
                    try:
                        ts_val = datetime.fromisoformat(ts_raw).timestamp()
                    except (ValueError, TypeError):
                        continue
                    if ts_val >= cutoff:
                        recent.append(p)
                if recent and all((p.get("value") or 0) > 95.0 for p in recent):
                    cpu_sustained = True

    cpu_cores = latest.get("cpu_per_core", [])
    cores_at_max = sum(1 for c in cpu_cores if c >= 99.0)

    disk_alert = any(d["percent"] > 90.0 for d in latest.get("disk", []))

    ups = latest.get("ups", {})
    ups_alert = ups.get("on_battery") is True or (
        ups.get("warning_level") not in (None, "none", "")
        and ups.get("warning_level") != "none"
    )

    g = latest.get("gpu", {})
    gpu_temp = g.get("temperature_c") or 0
    lq = latest.get("liquidctl", {})
    aio = lq.get("aio", {})
    aio_temp_entry = aio.get("Liquid temperature")
    if isinstance(aio_temp_entry, dict):
        aio_temp = aio_temp_entry.get("value") or 0
    elif isinstance(aio_temp_entry, (int, float)):
        aio_temp = aio_temp_entry
    else:
        aio_temp = 0

    return jsonify({
        "cpu":              cpu_sustained,
        "cpu_cores":        cores_at_max > 12,
        "gpu_temp":         gpu_temp > 85.0,
        "gpu_temp_crit":    gpu_temp > 90.0,
        "gpu_vram":         (g.get("vram_usage_percent") or 0) > 96.0,
        "disk":             disk_alert,
        "ups":              ups_alert,
        "aio_temp_warn": aio_temp > 38.0,
        "aio_temp_crit": aio_temp > 40.0,
    })

# ---------------------------------------------------------------------------
# LLM Control
# ---------------------------------------------------------------------------
import configparser
import queue as _queue

CONFIG_INI   = Path("/usr/local/llama-server/config.ini")


# Per-run replay buffer for benchmark streaming (llama-bench / llama-batched-bench)
_bench_replay = BenchReplayBuffer(maxlen=settings.manager.benchmark.stream_queue_size)
_bench_cond   = threading.Condition()
_bench_active = False
_bench_lock   = threading.Lock()

def _bench_put(msg: dict):
    """Append to the per-run replay buffer and wake any waiting streams."""
    with _bench_cond:
        _bench_replay.append(msg)
        _bench_cond.notify_all()

# Separate queue for llama server log streaming



@app.route("/api/llm/server/log/tail")
def llama_log_tail():
    """Return last 50 filtered lines of the llama log file (no streaming).

    Reads only the tail of the file (~128KB) — llama-server logs can grow to
    hundreds of MB, and loading the whole file into memory would balloon the
    process. Using a bounded deque also caps memory at <50 filtered lines.
    """
    return proxies.proxy_to_primary("llama", "GET", "/llama/log/tail")


def _request_agent(kind: str) -> "dict | None":
    """Resolve the agent a request targets: the picker ?agent= (capability-
    checked) else the provider default — matching proxy_to_primary's routing.
    Used by the *-stream-info direct-SSE URLs and the config.ini reader, so a
    picker selection follows through to the same host the proxies pick.
    Defensive: outside a request context (no flask_request) it returns the
    default, so module-level / non-HTTP callers stay safe."""
    aid = None
    try:
        aid = flask_request.args.get("agent")
    except Exception:
        aid = None
    if aid:
        a = agent_registry.resolve_agent_by_id(aid, capability=kind)
        if a:
            return a
    did = agent_registry.default_agent_id_for(kind)
    return (agent_registry.resolve_agent_by_id(did) if did else None) \
        or agent_registry.primary_agent(kind)


@app.route("/api/llm/server/log/stream-info")
def llama_log_stream_info():
    agent = _request_agent("llama")
    if not agent:
        return jsonify({"ok": False, "error": "no primary llama agent set"}), 503
    path = "/llama/log/stream"
    token = agent_registry.issue_stream_token(agent["agent_id"], path, ttl=300)
    return jsonify({
        "ok": True,
        "url": f"{agent_registry.browser_reachable_bind_url(agent)}{path}?token={token}",
        "expires_in": 300,
    })


@app.route("/api/llm/server/log/stream")
def llama_log_stream():
    """SSE endpoint streaming llama-server log lines."""
    # Tight reap: the agent log keepalive is 15s, so 30s never kills a live
    # stream but quickly frees the worker when an agent goes silent — the
    # dominant thread-leak source under multi-agent log switching.
    return proxies.proxy_stream_to_primary("llama", "/llama/log/stream", read_timeout=30)
@app.route("/llm/log")
def llm_log_page():
    """Standalone pop-out llama-server log viewer."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Llama Server Log</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0a0a; color: #8a8; font-family: monospace; font-size: 0.88em; display: flex; flex-direction: column; height: 100vh; }
#toolbar { background: #111; border-bottom: 1px solid #222; display: flex; align-items: center; gap: 10px; padding: 8px 12px; flex-shrink: 0; }
#toolbar span { color: #666; font-size: 0.85em; }
#autoScroll { accent-color: #7af; }
#log { flex: 1; overflow-y: auto; padding: 12px; white-space: pre-wrap; word-break: break-all; }
</style>
</head>
<body>
<div id="toolbar">
  <span>Llama Server Log — live</span>
  <label style="display:flex;align-items:center;gap:5px;color:#666;font-size:0.85em;cursor:pointer;margin-left:auto;">
    <input type="checkbox" id="autoScroll" checked> Auto-scroll
  </label>
  <button onclick="document.getElementById('log').textContent=''" style="background:#222;border:1px solid #333;border-radius:4px;color:#888;cursor:pointer;font-size:0.8em;padding:3px 10px;">Clear</button>
</div>
<div id="log"></div>
<script>
const log = document.getElementById('log');
const _ag = new URLSearchParams(location.search).get('agent');
const src = new EventSource('/api/llm/server/log/stream' + (_ag ? ('?agent=' + encodeURIComponent(_ag)) : ''));
src.onmessage = e => {
  const msg = JSON.parse(e.data);
  if (msg.keepalive) return;
  log.textContent += msg.line + '\\n';
  if (document.getElementById('autoScroll').checked) log.scrollTop = log.scrollHeight;
};
src.onerror = () => { log.textContent += '\\n[stream disconnected]\\n'; };
</script>
</body>
</html>"""
    resp = app.response_class(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/llm/server/status")
def llama_server_status():
    return proxies.proxy_to_primary("llama", "GET", "/llama/server/status")
@app.route("/api/llm/server/stop", methods=["POST"])
def llama_server_stop():
    return proxies.proxy_to_primary("llama", "POST", "/llama/server/stop")
@app.route("/api/llm/server/start", methods=["POST"])
def llama_server_start():
    return proxies.proxy_to_primary("llama", "POST", "/llama/server/start")
@app.route("/api/llm/server/restart", methods=["POST"])
def llama_server_restart():
    return proxies.proxy_to_primary("llama", "POST", "/llama/server/restart")


@app.route("/api/llm/server/wake", methods=["POST"])
def llama_server_wake():
    # Generous timeout — when --sleep-idle-seconds has unloaded the model,
    # the wake forces a reload from disk that can take many seconds.
    data     = flask_request.get_json(silent=True) or {}
    model_id = data.get("model_id") or data.get("model")
    proxied = proxies.proxy_to_primary("llama", "POST", "/llama/server/wake",
                                       json=(data or None), timeout=75, model_id=model_id)
    if proxied is not None:
        return proxied
    return jsonify({"ok": False, "error": "no approved primary llama agent"}), 503
def _read_ini():
    cp = configparser.ConfigParser(default_section="__DEFAULTS__", interpolation=None)
    cp.optionxform = str
    agent = _request_agent("llama")
    if agent:
        resp, _tried, _err = agent_registry.agent_request(
            "GET", agent, "/llama/config",
            headers={"Authorization": f"Bearer {agent['token']}"},
            timeout=5,
        )
        if resp is not None and resp.ok:
            data = resp.json() or {}
            for section, kv in data.items():
                cp.add_section(section)
                for k, v in (kv or {}).items():
                    cp.set(section, k, str(v) if v is not None else "")
            return cp
    cp.read(str(CONFIG_INI))
    return cp


@app.route("/api/llm/models")
def llm_models():
    return proxies.proxy_to_primary("llama", "GET", "/llama/models")
@app.route("/api/llm/load", methods=["POST"])
def llm_load():
    data     = flask_request.get_json(force=True)
    model_id = (data or {}).get("model_id") or (data or {}).get("model")
    return proxies.proxy_to_primary("llama", "POST", "/llama/load",
                                 json=data, timeout=60, model_id=model_id)
@app.route("/api/llm/unload", methods=["POST"])
def llm_unload_model():
    data     = flask_request.get_json(force=True)
    model_id = (data or {}).get("model_id") or (data or {}).get("model")
    return proxies.proxy_to_primary("llama", "POST", "/llama/unload",
                                 json=data, model_id=model_id)
@app.route("/api/llm/config", methods=["GET"])
def llm_get_config():
    return proxies.proxy_to_primary("llama", "GET", "/llama/config")
@app.route("/api/llm/config", methods=["POST"])
def llm_save_config():
    body = flask_request.get_json(force=True)
    return proxies.proxy_to_primary("llama", "POST", "/llama/config", json=body)
@app.route("/api/llm/config/<path:model_id>", methods=["DELETE"])
def llm_delete_config(model_id):
    """Remove a model from config.ini.

    If `?delete_cache=true` is passed, also unlink the specific quant's
    .gguf file from the HuggingFace cache. We delete only the quant-matching
    file (and its underlying blob) — never the whole repo — so a repo with
    multiple quants (e.g. Q4_K_M and Q5_K_M) loses only the one being deleted.
    Sharded quants (foo-Q4_K_M-00001-of-00003.gguf etc.) are matched by glob.
    """
    return proxies.proxy_to_primary(
        "llama", "DELETE",
        f"/llama/config/{model_id}",
        params={"delete_cache": "true"} if (flask_request.args.get("delete_cache") or "").lower() == "true" else {},
    )

# ---------------------------------------------------------------------------
# Benchmark (llama-bench / llama-batched-bench) — see plan
# ---------------------------------------------------------------------------
LLAMA_CPP_DIR        = "/usr/local/llama.cpp"
LLAMA_BENCH_BIN      = f"{LLAMA_CPP_DIR}/llama-bench"
LLAMA_BATCHED_BENCH_BIN = f"{LLAMA_CPP_DIR}/llama-batched-bench"


def _get_model_hf_arg(model_id: str):
    """Return the string to pass to '-hf' (e.g. 'author/repo:QUANT'), or None."""
    with best_effort("read -hf arg from config.ini", log=log):
        cp = _read_ini()
        section = cp[model_id] if model_id in cp.sections() else None
        if section:
            hf_repo = section.get('hf-repo') or section.get('--hf-repo')
            hf_file = section.get('hf-file') or section.get('--hf-file')
            if hf_repo:
                return f"{hf_repo}:{hf_file}" if hf_file else hf_repo
    # Fallback: model_id itself is already in repo:quant format
    if '/' in model_id:
        return model_id
    return None


def _parse_bench_row(row: dict, tool: str):
    """Extract (gen_tps, ppt_tps) from a single jsonl row. Returns (None, None)
    for non-result rows (build info, etc.).

    llama-bench JSONL has no 'test' field — test type is inferred from
    n_prompt / n_gen counts, with avg_ts being tokens/sec for that test."""
    if not isinstance(row, dict):
        return (None, None)
    if tool == 'llama-bench':
        ts_val = row.get('avg_ts')
        if ts_val is None:
            ts_val = row.get('t_s')  # legacy field
        if ts_val is None:
            return (None, None)
        n_prompt = int(row.get('n_prompt', 0) or 0)
        n_gen    = int(row.get('n_gen', 0) or 0)
        if n_prompt > 0 and n_gen == 0:
            return (None, float(ts_val))                  # pp test → ppt_tps
        if n_gen > 0 and n_prompt == 0:
            return (float(ts_val), None)                  # tg test → gen_tps
        if n_prompt > 0 and n_gen > 0:
            return (float(ts_val), None)                  # pg test → count as gen
        return (None, None)
    else:
        # llama-batched-bench JSONL: each row exposes pp/tg throughput.
        pp = row.get('pp_tps') or row.get('avg_pp_tps') or row.get('t_pp')
        tg = row.get('tg_tps') or row.get('avg_tg_tps') or row.get('t_tg')
        gen_tps = float(tg) if tg is not None else None
        ppt_tps = float(pp) if pp is not None else None
        return (gen_tps, ppt_tps)


_bench_proc = None   # current running subprocess, set for cancel support
_bench_pgid = None   # process group id of current subprocess (for killpg)
_bench_cancel_event = threading.Event()  # signals reader loop to exit early on cancel

def _run_one_model(model_id: str, tool: str, tool_path: str, jsonl_flags: list,
                   switches: list, env: dict, ansi_re) -> dict:
    """Run bench for a single model. avg_ts in each JSONL row is already
    the per-run average over repetitions, so we emit raw values only."""

    hf_arg = _get_model_hf_arg(model_id)
    if not hf_arg:
        _bench_put({"type": "model_done", "model_id": model_id, "ok": False,
                    "error": f"no HF reference found for {model_id}"})
        return {"model_id": model_id, "ok": False, "last_gen_tps": None, "last_ppt_tps": None}

    cmd = [tool_path]
    for sw in (switches or []):
        flag = (sw.get('flag') or '').strip()
        if not flag:
            continue
        cmd.append(flag)
        val = sw.get('value')
        if val is not None and str(val).strip() != '':
            cmd.append(str(val))
    cmd += ['-hf', hf_arg]
    cmd += jsonl_flags

    _bench_put({"type": "model_start", "model_id": model_id,
                "cmd": " ".join(str(c) for c in cmd)})

    global _bench_proc, _bench_pgid
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True, bufsize=1,
        close_fds=True, env=env,
        start_new_session=True,  # own process group → killpg kills HIP/ROCm children too
    )
    _bench_proc = proc
    try:
        _bench_pgid = os.getpgid(proc.pid)
    except Exception:
        _bench_pgid = None

    result_rows = []
    latest_gen  = None
    latest_ppt  = None

    def _drain_stderr():
        with best_effort("bench: drain subprocess stderr", log=log):
            for line in iter(proc.stderr.readline, ''):
                if not line:
                    break
                txt = ansi_re.sub('', line.rstrip('\n'))
                if txt:
                    _bench_put({"type": "line", "model_id": model_id, "text": txt})
    threading.Thread(target=_drain_stderr, daemon=True).start()

    for raw in iter(proc.stdout.readline, ''):
        if _bench_cancel_event.is_set():
            break
        if not raw:
            break
        line = ansi_re.sub('', raw.rstrip('\n'))
        if not line:
            continue
        _bench_put({"type": "line", "model_id": model_id, "text": line})
        try:
            row = json.loads(line)
        except Exception:
            continue
        gen_tps, ppt_tps = _parse_bench_row(row, tool)
        if gen_tps is None and ppt_tps is None:
            continue
        if gen_tps is not None:
            latest_gen = gen_tps
        if ppt_tps is not None:
            latest_ppt = ppt_tps
        # Emit full JSONL fields so frontend can use any field for axes
        result_row = {
            "n_prompt": int(row.get('n_prompt', 0) or 0),
            "n_gen":    int(row.get('n_gen',    0) or 0),
            "n_depth":  int(row.get('n_depth',  0) or 0),
            "n_batch":  int(row.get('n_batch',  0) or 0),
            "n_ubatch": int(row.get('n_ubatch', 0) or 0),
            "avg_ts":   float(row.get('avg_ts', 0) or 0),
        }
        result_rows.append(result_row)
        _bench_put({"type": "result", "model_id": model_id,
                    "gen_tps": gen_tps, "ppt_tps": ppt_tps,
                    **result_row})

    proc.wait()
    _bench_proc = None

    cancelled = _bench_cancel_event.is_set()
    # Send raw last-seen values — no further averaging (avg_ts is already per-run average)
    _bench_put({"type": "model_done", "model_id": model_id,
                "ok": (not cancelled) and proc.returncode == 0,
                "rc": proc.returncode, "cancelled": cancelled,
                "last_gen_tps": latest_gen, "last_ppt_tps": latest_ppt,
                "results": result_rows})
    return {"model_id": model_id, "ok": (not cancelled) and proc.returncode == 0,
            "cancelled": cancelled,
            "last_gen_tps": latest_gen, "last_ppt_tps": latest_ppt}


def _run_benchmark(model_ids: list, tool: str, switches: list):
    """Run llama-bench sequentially for each model in model_ids, emitting
    per-model events and a final 'done' with all results."""
    global _bench_active, _bench_proc
    _bench_cancel_event.clear()
    try:
        import os as _os

        if tool == 'llama-bench':
            tool_path = LLAMA_BENCH_BIN
            jsonl_flags = ['-o', 'jsonl']
        elif tool == 'llama-batched-bench':
            tool_path = LLAMA_BATCHED_BENCH_BIN
            jsonl_flags = ['--output-format', 'jsonl']
        else:
            _bench_put({"type": "done", "ok": False, "error": f"unknown tool: {tool}"})
            return

        env = dict(_os.environ)
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{LLAMA_CPP_DIR}:{existing}" if existing else LLAMA_CPP_DIR
        env["FORCE_COLOR"] = "0"
        env["PYTHONUNBUFFERED"] = "1"

        ansi_re = re.compile(r'\x1b\[[0-9;]*[mGKHF]')

        all_results = []
        for model_id in model_ids:
            if _bench_cancel_event.is_set():
                break
            result = _run_one_model(model_id, tool, tool_path, jsonl_flags, switches, env, ansi_re)
            all_results.append(result)

        cancelled = _bench_cancel_event.is_set()
        _bench_put({"type": "done", "ok": not cancelled, "cancelled": cancelled,
                    "results": all_results})
    except Exception as e:
        log.error(f"_run_benchmark error: {e}", exc_info=True)
        _bench_put({"type": "done", "ok": False, "error": "benchmark failed"})
    finally:
        _bench_proc = None
        with _bench_lock:
            _bench_active = False


def _queue_benchmark(model_ids: list, tool: str, switches: list) -> bool:
    """Acquire lock, clear queue, start _run_benchmark in a thread."""
    global _bench_active
    with _bench_lock:
        if _bench_active:
            return False
        _bench_active = True
    with _bench_cond:
        _bench_replay.start_run(uuid.uuid4().hex[:12])
    threading.Thread(target=_run_benchmark, args=(model_ids, tool, switches), daemon=True).start()
    return True


@app.route("/api/llm/download", methods=["POST"])
def llm_download():
    data     = flask_request.get_json(force=True)
    return proxies.proxy_to_primary("llama", "POST", "/llama/download", json=data)


@app.route("/api/llm/download/cancel", methods=["POST"])
def llm_download_cancel():
    """Kill the running hf download / cache-prune subprocess on the primary
    llama agent. No body required."""
    proxied = proxies.proxy_to_primary("llama", "POST", "/llama/download/cancel")
    if proxied is not None:
        return proxied
    return jsonify({"ok": False, "error": "no primary llama agent"}), 503
@app.route("/api/llm/download/stream-info")
def llm_download_stream_info():
    """Direct-agent SSE URL for HF download progress."""
    agent = _request_agent("llama")
    if not agent:
        return jsonify({"ok": False, "error": "no primary llama agent set"}), 503
    path = "/llama/download/stream"
    token = agent_registry.issue_stream_token(agent["agent_id"], path, ttl=900)  # downloads can be long
    return jsonify({
        "ok": True,
        "url": f"{agent_registry.browser_reachable_bind_url(agent)}{path}?token={token}",
        "expires_in": 900,
    })


@app.route("/api/llm/download/stream")
def llm_download_stream():
    return proxies.proxy_stream_to_primary("llama", "/llama/download/stream", long_running=True)


@app.route("/api/llm/build/stream-info")
def llm_build_stream_info():
    agent = _request_agent("llama")
    if not agent:
        return jsonify({"ok": False, "error": "no primary llama agent set"}), 503
    path = "/llama/build/stream"
    # 30-minute TTL because llama.cpp rebuilds can take >10 min on slower hosts.
    token = agent_registry.issue_stream_token(agent["agent_id"], path, ttl=1800)
    return jsonify({
        "ok": True,
        "url": f"{agent_registry.browser_reachable_bind_url(agent)}{path}?token={token}",
        "expires_in": 1800,
    })


@app.route("/api/llm/build", methods=["POST"])
def llm_build():
    return proxies.proxy_to_primary("llama", "POST", "/llama/build")


@app.route("/api/llm/build/stream")
def llm_build_stream():
    return proxies.proxy_stream_to_primary("llama", "/llama/build/stream", long_running=True)


@app.route("/api/benchmark/run", methods=["POST"])
def benchmark_run():
    # Proxy to the primary llama agent — llama-bench lives next to llama-server
    # on the inference host, never on the manager host.
    body = flask_request.get_json(force=True) or {}
    proxied = proxies.proxy_to_primary("llama", "POST", "/llama/bench/run", json=body, timeout=15)
    if proxied is not None:
        return proxied
    try:
        data = body
        # Accept model_ids (list) or legacy model_id (single string)
        model_ids = data.get("model_ids")
        if not model_ids:
            single = (data.get("model_id") or "").strip()
            model_ids = [single] if single else []
        if not isinstance(model_ids, list):
            model_ids = [str(model_ids)]
        model_ids = [m.strip() for m in model_ids if str(m).strip()]
        tool     = (data.get("tool") or "").strip()
        switches = data.get("switches") or []
        if not model_ids:
            return jsonify({"ok": False, "error": "at least one model_id required"}), 400
        if tool not in ("llama-bench", "llama-batched-bench"):
            return jsonify({"ok": False, "error": "invalid tool"}), 400
        if not isinstance(switches, list):
            return jsonify({"ok": False, "error": "switches must be a list"}), 400
        if not _queue_benchmark(model_ids, tool, switches):
            return jsonify({"ok": False, "error": "Another benchmark is in progress"}), 409
        return jsonify({"ok": True})
    except Exception as e:
        return _err_json("internal error", 500, exc=e)


@app.route("/api/benchmark/stream")
def benchmark_stream():
    proxied = proxies.proxy_stream_to_primary("llama", "/llama/bench/stream", long_running=True)
    if proxied is not None:
        return proxied
    from flask import stream_with_context
    last_event_id = flask_request.headers.get("Last-Event-ID")
    def generate():
        with _bench_cond:
            cur_run = _bench_replay.run_id
            last_seq = _bench_replay.seq_for(last_event_id)
        while True:
            with _bench_cond:
                if cur_run and _bench_replay.run_id != cur_run:
                    return   # superseded by a newer run; client opens a fresh stream
                cur_run = _bench_replay.run_id
                new = _bench_replay.records_after_seq(last_seq)
                if not new:
                    _bench_cond.wait(timeout=10)
                    if cur_run and _bench_replay.run_id != cur_run:
                        return
                    new = _bench_replay.records_after_seq(last_seq)
            if not new:
                yield 'data: {"type":"keepalive"}\n\n'
                continue
            for rec in new:
                yield f"id: {rec['id']}\ndata: {json.dumps(rec['event'])}\n\n"
                last_seq = rec["seq"]
                if rec["event"].get("type") == "done":
                    return
    return app.response_class(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/benchmark/results", methods=["GET"])
def benchmark_results():
    try:
        agent = _request_agent("llama")
        agent_id = (agent or {}).get("agent_id") or ""
        conn = get_db()
        # The selected agent's rows plus legacy ('') rows; agent-specific wins
        # per model (ORDER puts agent rows first, the dedup below keeps them).
        rows = conn.execute(
            "SELECT model_id, avg_gen_tps, avg_ppt_tps, bench_tool, switches, ts, agent_id, avg_pg_tps "
            "FROM model_benchmarks WHERE agent_id = ? OR agent_id = '' "
            "ORDER BY (agent_id = '') ASC",
            (agent_id,),
        ).fetchall()
        seen = set()
        out = []
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            try: switches = json.loads(r[4]) if r[4] else []
            except Exception: switches = []
            out.append({
                "model_id":    r[0],
                "avg_gen_tps": r[1],
                "avg_ppt_tps": r[2],
                "avg_pg_tps":  r[7],
                "bench_tool":  r[3],
                "switches":    switches,
                "ts":          r[5],
                "agent_id":    r[6],
            })
        return jsonify({"results": out})
    except Exception as e:
        return _err_json("internal error", 500, exc=e)


@app.route("/api/benchmark/store", methods=["POST"])
def benchmark_store():
    try:
        data = flask_request.get_json(force=True) or {}
        model_id    = (data.get("model_id") or "").strip()
        avg_gen_tps = data.get("avg_gen_tps")
        avg_ppt_tps = data.get("avg_ppt_tps")
        avg_pg_tps  = data.get("avg_pg_tps")
        bench_tool  = data.get("bench_tool") or ""
        switches    = data.get("switches") or []
        if not model_id:
            return jsonify({"ok": False, "error": "model_id required"}), 400
        agent = _request_agent("llama")
        agent_id = (agent or {}).get("agent_id") or ""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        conn.execute(
            "INSERT INTO model_benchmarks "
            "(model_id, agent_id, avg_gen_tps, avg_ppt_tps, avg_pg_tps, bench_tool, switches, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(model_id, agent_id) DO UPDATE SET "
            "  avg_gen_tps=excluded.avg_gen_tps,"
            "  avg_ppt_tps=excluded.avg_ppt_tps,"
            "  avg_pg_tps=excluded.avg_pg_tps,"
            "  bench_tool=excluded.bench_tool,"
            "  switches=excluded.switches,"
            "  ts=excluded.ts",
            (model_id, agent_id, avg_gen_tps, avg_ppt_tps, avg_pg_tps, bench_tool,
             json.dumps(switches), ts)
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return _err_json("internal error", 500, exc=e)


@app.route("/api/benchmark/results/<path:model_id>", methods=["DELETE"])
def benchmark_delete(model_id):
    try:
        agent = _request_agent("llama")
        agent_id = (agent or {}).get("agent_id") or ""
        conn = get_db()
        # Remove what this agent's view shows: its own row + any legacy ('') row.
        conn.execute(
            "DELETE FROM model_benchmarks WHERE model_id=? AND (agent_id=? OR agent_id='')",
            (model_id, agent_id),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return _err_json("internal error", 500, exc=e)


@app.route("/api/benchmark/models", methods=["GET"])
def benchmark_models():
    """Return all configured model IDs from config.ini section names."""
    try:
        cp = _read_ini()
        models = list(cp.sections())
        return jsonify({"models": models})
    except Exception as e:
        return _err_json("internal error", 500, exc=e, models=[])


@app.route("/api/benchmark/perf-mode", methods=["POST"])
def benchmark_perf_mode():
    """Switch system perf mode (performance / powersave) for benchmarking.
    Calls 'sudo systemctl restart {service}' on the primary llama agent;
    those systemd targets live with the perf controller, not on the manager."""
    body = flask_request.get_json(force=True) or {}
    proxied = proxies.proxy_to_primary("llama", "POST", "/llama/bench/perf-mode", json=body, timeout=35)
    if proxied is not None:
        return proxied
    try:
        data = body
        mode = (data.get("mode") or "").strip()
        if mode not in ("performance", "powersave"):
            return jsonify({"ok": False, "error": "mode must be 'performance' or 'powersave'"}), 400
        proc = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", mode],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return jsonify({
                "ok": False,
                "error": (proc.stderr or proc.stdout or "").strip()[:300] or f"rc={proc.returncode}",
            }), 500
        return jsonify({"ok": True, "mode": mode})
    except Exception as e:
        return _err_json("internal error", 500, exc=e)


@app.route("/api/benchmark/cancel", methods=["POST"])
def benchmark_cancel():
    """Terminate the currently running benchmark subprocess and its process
    group (covers HIP/ROCm child processes spawned by llama-bench).
    Also signals the reader loop to exit early and runs a pkill fallback
    to catch any GPU compute children that escaped the process group."""
    proxied = proxies.proxy_to_primary("llama", "POST", "/llama/bench/cancel", timeout=10)
    if proxied is not None:
        return proxied
    global _bench_proc, _bench_pgid
    # Signal the reader loop to exit early regardless of process state
    _bench_cancel_event.set()
    proc = _bench_proc
    pgid = _bench_pgid
    if proc is None:
        # No tracked subprocess, but still run pkill in case stragglers exist
        with best_effort("bench cancel: pkill stray bench procs", log=log):
            subprocess.run(['pkill', '-9', '-f', 'llama-bench'], capture_output=True, timeout=3)
            subprocess.run(['pkill', '-9', '-f', 'llama-batched-bench'], capture_output=True, timeout=3)
        return jsonify({"ok": True, "msg": "no tracked benchmark process"})
    try:
        # SIGTERM to whole process group first
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as _e:
                log.warning("killpg SIGTERM failed: %s", _e)
                try: proc.terminate()
                except Exception as _e2: log.warning("proc.terminate fallback failed: %s", _e2)
        else:
            try: proc.terminate()
            except Exception as _e: log.warning("proc.terminate failed: %s", _e)
        # Wait briefly, then SIGKILL if still alive
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            log.warning("proc did not exit within 4s; escalating to SIGKILL")
            if pgid is not None:
                try: os.killpg(pgid, signal.SIGKILL)
                except Exception as _e: log.warning("killpg SIGKILL failed: %s", _e)
            try: proc.kill()
            except Exception as _e: log.warning("proc.kill failed: %s", _e)
        # pkill fallback for any HIP/ROCm child processes that escaped the
        # process group (some GPU runtimes fork into their own session).
        with best_effort("bench cancel: pkill HIP/ROCm child procs", log=log):
            subprocess.run(['pkill', '-9', '-f', 'llama-bench'], capture_output=True, timeout=3)
            subprocess.run(['pkill', '-9', '-f', 'llama-batched-bench'], capture_output=True, timeout=3)
        # Background thread (_run_one_model) will see EOF on stdout, call
        # proc.wait(), and emit model_done + done naturally — no duplicate event.
    except Exception as e:
        return _err_json("internal error", 500, exc=e)
    return jsonify({"ok": True})


# --- Auto-tune CTX (iterative -fitt convergence) ---
# Pure proxy to the primary llama agent. The bisection loop and llama-server
# subprocess live on the agent host where the binary and GPU are.

@app.route("/api/llm/autotune/run", methods=["POST"])
def llm_autotune_run():
    body = flask_request.get_json(force=True) or {}
    proxied = proxies.proxy_to_primary("llama", "POST", "/llama/autotune/run", json=body, timeout=15)
    if proxied is not None:
        return proxied
    return jsonify({"ok": False, "error": "no primary llama agent set"}), 503


@app.route("/api/llm/autotune/stream")
def llm_autotune_stream():
    proxied = proxies.proxy_stream_to_primary("llama", "/llama/autotune/stream", long_running=True)
    if proxied is not None:
        return proxied
    return jsonify({"ok": False, "error": "no primary llama agent set"}), 503


@app.route("/api/llm/autotune/stream-info")
def llm_autotune_stream_info():
    """Direct-agent SSE URL for auto-tune progress (matches download/log pattern)."""
    agent = _request_agent("llama")
    if not agent:
        return jsonify({"ok": False, "error": "no primary llama agent set"}), 503
    path = "/llama/autotune/stream"
    token = agent_registry.issue_stream_token(agent["agent_id"], path, ttl=1800)
    return jsonify({
        "ok": True,
        "url": f"{agent_registry.browser_reachable_bind_url(agent)}{path}?token={token}",
        "expires_in": 1800,
    })


@app.route("/api/llm/autotune/cancel", methods=["POST"])
def llm_autotune_cancel():
    proxied = proxies.proxy_to_primary("llama", "POST", "/llama/autotune/cancel", timeout=10)
    if proxied is not None:
        return proxied
    return jsonify({"ok": False, "error": "no primary llama agent set"}), 503


@app.route("/api/llm/cache")
def llm_cache_list():
    """Run hf cache list --format json and return parsed data."""
    return proxies.proxy_to_primary("llama", "GET", "/llama/cache")
@app.route("/api/llm/cache/prune", methods=["POST"])
def llm_cache_prune():
    return proxies.proxy_to_primary("llama", "POST", "/llama/cache/prune")
@app.route("/api/llm/cache/rm", methods=["POST"])
def llm_cache_rm():
    data    = flask_request.get_json(force=True)
    return proxies.proxy_to_primary("llama", "POST", "/llama/cache/rm", json=data)
@app.route("/api/llm/hf-trending")
def llm_hf_trending():
    """Fetch top 10 trending HF models in 27B-35B param range sorted by downloads."""
    return proxies.proxy_to_primary("llama", "GET", "/llama/hf-trending")


# ---------------------------------------------------------------------------
# Per-agent telemetry helpers — backed by provider_state.STORE.
#
# Storage replaces the prior 11 singleton globals. Default-agent fallback
# preserves byte-for-byte today's single-primary behavior for any caller
# that doesn't pass ?agent= — PR1 keeps the receive_remote_host_metrics
# primary gate so STORE.all_for("llama") still contains at most one
# agent's sample.
# ---------------------------------------------------------------------------

def _primary_llama_agent_id() -> "str | None":
    # default_agent_id_for (default_<p>_id → primary_<p>_id → first approved
    # capable) is the SAME resolver /api/agents/list-by-provider marks as the
    # picker default, so the no-?agent= fallback and the picker default always
    # agree.
    return agent_registry.default_agent_id_for("llama")


def _primary_lms_agent_id() -> "str | None":
    return agent_registry.default_agent_id_for("lms")


def _llama_sample_for_request() -> dict:
    """The llama host sample for the ?agent= selection, else the default
    agent. Drives /api/metrics + /api/alert so the Dashboard llama.cpp cards
    follow the picker."""
    aid = flask_request.args.get("agent") or _primary_llama_agent_id()
    if not aid:
        return {}
    w = provider_state.STORE.get("llama", aid)
    return (w or {}).get("sample") or {}


def _primary_llama_last_seen() -> float:
    aid = _primary_llama_agent_id()
    if not aid:
        return 0.0
    w = provider_state.STORE.get("llama", aid)
    return float((w or {}).get("last_seen") or 0.0)




def _primary_lms_last_seen() -> float:
    aid = _primary_lms_agent_id()
    if not aid:
        return 0.0
    w = provider_state.STORE.get("lms", aid)
    return float((w or {}).get("last_seen") or 0.0)


def _any_lms_busy() -> bool:
    """True iff at least one LMS agent has a fresh sample (<15s) AND any
    ps entry is non-IDLE. Replaces the old singleton staleness check at
    set_interval that read _lmstudio_last_seen directly."""
    now = time.time()
    for wrapper in provider_state.STORE.all_for("lms").values():
        if (now - (wrapper.get("last_seen") or 0)) > 15:
            continue
        ps = (wrapper.get("sample") or {}).get("ps") or []
        if any(p.get("status", "").upper() not in ("IDLE", "STOPPED", "")
               for p in (ps or [])):
            return True
    return False


# Process-wide shutdown signal. Long-lived SSE generators check this and
# exit on True so cheroot.stop() doesn't hang waiting on sockets that
# never close on their own (browser EventSources stay connected forever).
_shutting_down: bool = False


def _build_llama_state_payload(agent_id: "str | None" = None) -> dict:
    """Snapshot the same shape /api/llama-state returns. When agent_id is
    None falls back to the primary llama agent — preserves today's
    behavior for any caller that pre-dates the picker."""
    if agent_id is None:
        agent_id = _primary_llama_agent_id()
    wrapper = provider_state.STORE.get("llama", agent_id) if agent_id else None
    sample = (wrapper or {}).get("sample") or {}
    last_seen = float((wrapper or {}).get("last_seen") or 0.0)
    llama = sample.get("llama") or {}
    m = llama.get("model")
    if isinstance(m, str):
        m = m.replace(" (sleeping)", "").replace(" (unloaded)", "").strip() or None
    agent_age = (time.time() - last_seen) if last_seen else None
    agent_online = agent_age is not None and agent_age < 30.0
    return {
        "state": llama.get("state") or "unknown",
        "model": m,
        "port":  8080,
        "agent_online": agent_online,
        "agent_age_s": round(agent_age, 1) if agent_age is not None else None,
        "build_method": llama.get("build_method"),
    }


def _broadcast_llama_state_if_changed(agent_id: "str | None" = None) -> None:
    """Per-(provider, agent_id) fingerprint-debounced SSE fan-out.
    Falls back to the primary llama agent when agent_id is None."""
    if agent_id is None:
        agent_id = _primary_llama_agent_id()
    if not agent_id:
        return
    payload = _build_llama_state_payload(agent_id)
    provider_state.STORE.broadcast_if_changed(
        "llama", agent_id, payload,
        fingerprint_keys=("state", "model", "agent_online"),
    )


@app.route("/api/remote/host-metrics", methods=["POST"])
def receive_remote_host_metrics():
    tok = agent_registry.bearer_from_request()
    agent = agent_registry.agent_by_token(tok or "")
    if not agent:
        return jsonify({"ok": False, "error": "invalid token or not approved"}), 401
    try:
        data = flask_request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad JSON"}), 400
    # PR2: every approved llama-capable agent pushes; STORE partitions them.
    aid = agent["agent_id"]
    provider_state.STORE.put("llama", aid, data)
    if provider_state.STORE.mark_online("llama", aid):
        log.info(f"llama agent online [agent {aid[:8]}@{agent.get('hostname','?')}]")
    # Broadcast outside the lock — STORE.broadcast_if_changed handles its
    # own locking and drops it before fanning out to queues.
    _broadcast_llama_state_if_changed(aid)
    return jsonify({"ok": True})


@app.route("/api/remote/provider-state", methods=["POST"])
def receive_provider_state():
    """Generic per-provider envelope: {provider, sample}. New agents prefer
    this; old agents keep posting to /api/remote/host-metrics or
    /api/remote/lmstudio (still wired below). Returns 404 for unknown
    providers so an old manager looks indistinguishable to a new agent."""
    tok = agent_registry.bearer_from_request()
    agent = agent_registry.agent_by_token(tok or "")
    if not agent:
        return jsonify({"ok": False, "error": "invalid token or not approved"}), 401
    try:
        body = flask_request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad JSON"}), 400
    provider = (body.get("provider") or "").strip()
    sample = body.get("sample") or {}
    if provider not in providers.names():
        return jsonify({"ok": False, "error": f"unknown provider: {provider}"}), 404
    aid = agent["agent_id"]
    provider_state.STORE.put(provider, aid, sample)
    if provider_state.STORE.mark_online(provider, aid):
        log.info(f"{provider} agent online "
                 f"[agent {aid[:8]}@{agent.get('hostname','?')}]")
    if provider == "llama":
        _broadcast_llama_state_if_changed(aid)
    elif provider == "lms":
        set_lms_active(_any_lms_busy())
    return jsonify({"ok": True})


@app.route("/api/agents/list-by-provider", methods=["GET"])
def agents_list_by_provider():
    """Non-admin-gated picker enumeration. Returns approved agents grouped
    by provider with hostname + is_default + online flag — deliberate
    info-disclosure tradeoff so dashboard viewers can render the picker
    without holding admin CIDR access. See plan §Read endpoint shape."""
    out: dict = {}
    data = agent_registry.load_agents()
    agents_map = data.get("agents") or {}
    now = time.time()
    for prov in providers.names():
        spec = providers.get(prov)
        cap_key = spec.capability_key if spec else prov
        # Same resolver the no-?agent= endpoints use as their fallback, so the
        # picker's default chip == the host every un-parameterized call hits.
        default_id = agent_registry.default_agent_id_for(prov)
        rows = []
        for aid, a in agents_map.items():
            if a.get("status") != "approved":
                continue
            if not (a.get("capabilities") or {}).get(cap_key):
                continue
            wrap = provider_state.STORE.get(prov, aid) or {}
            last_seen = float(wrap.get("last_seen") or 0)
            threshold = spec.online_threshold_s if spec else 30.0
            is_online = (now - last_seen) < threshold if last_seen else False
            rows.append({
                "agent_id": aid,
                "hostname": a.get("hostname"),
                "is_default": aid == default_id,
                "online": is_online,
                "age_s": round(now - last_seen, 1) if last_seen else None,
            })
        out[prov] = rows
    return jsonify(out)


@app.route("/api/agents/metrics", methods=["GET"])
def agents_metrics():
    """Returns every agent's most-recent sample for one provider.
    Requires ?provider=<name>. Admin-gated since it exposes raw telemetry."""
    deny = _require_admin()
    if deny is not None:
        return deny
    provider = (flask_request.args.get("provider") or "").strip()
    if provider not in providers.names():
        return jsonify({"ok": False, "error": f"unknown provider: {provider}"}), 400
    now = time.time()
    spec = providers.get(provider)
    threshold = spec.online_threshold_s if spec else 30.0
    cap_key = spec.capability_key if spec else provider
    samples = provider_state.STORE.all_for(provider)
    # Enumerate every approved+capable agent so a newly-approved agent that
    # hasn't pushed yet still appears (with an empty sample) — mirrors
    # /api/agents/list-by-provider so the admin tab cross-references cleanly.
    agents_map = (agent_registry.load_agents().get("agents") or {})
    rows: list = []
    for aid, a in agents_map.items():
        if a.get("status") != "approved":
            continue
        if not (a.get("capabilities") or {}).get(cap_key):
            continue
        wrap = samples.get(aid) or {}
        last_seen = float(wrap.get("last_seen") or 0)
        rows.append({
            "agent_id": aid,
            "sample": wrap.get("sample") or {},
            "last_seen_epoch": last_seen,
            "age_s": round(now - last_seen, 1) if last_seen else None,
            "online": (now - last_seen) < threshold if last_seen else False,
        })
    return jsonify({"ok": True, "provider": provider, "agents": rows})


@app.route("/api/fleet/<provider>/aggregate", methods=["GET"])
def fleet_aggregate(provider: str):
    """Server-computed fleet rollup for one provider. Delegates to the
    provider's ProviderSpec.aggregator; returns 404 for unknown providers
    or providers without an aggregator wired."""
    spec = providers.get(provider)
    if spec is None:
        return jsonify({"ok": False, "error": f"unknown provider: {provider}"}), 404
    if spec.aggregator is None:
        return jsonify({"ok": False, "error": f"provider {provider} has no aggregator"}), 404
    try:
        rollup = spec.aggregator(provider_state.STORE.all_for(provider))
    except Exception as e:
        log.exception("fleet aggregate %s failed: %s", provider, e)
        return jsonify({"ok": False, "error": "aggregator raised"}), 500
    return jsonify(rollup)


def _llama_state_sse(initial_frame, q, *, max_lifetime_s, keepalive_s=5.0,
                     is_shutting_down=lambda: False, on_finish=None):
    """Keepalive SSE generator: yield initial_frame, forward queue msgs, idle
    keepalives, and RETURN after max_lifetime_s; on_finish runs in finally."""
    deadline = time.monotonic() + max_lifetime_s
    try:
        yield initial_frame
        while not is_shutting_down() and time.monotonic() < deadline:
            try:
                msg = q.get(timeout=keepalive_s)
            except _queue.Empty:
                yield ': keepalive\n\n'
                continue
            if msg is None:  # shutdown sentinel
                break
            yield msg
    except GeneratorExit:
        pass
    finally:
        if on_finish is not None:
            on_finish()


@app.route("/api/llama-state/stream-info")
def stream_llama_state_info():
    """Session-gated handoff: mint an HMAC token + return the daemon SSE URL,
    or {enabled:false} when the daemon is off/down, the page is HTTPS (the
    plain-http daemon would be mixed-content), or no agent resolves — in which
    cases the browser uses the Cheroot /api/llama-state/stream path (#110)."""
    from urllib.parse import quote
    port = int(getattr(settings.manager, "stream_proxy_port", 5445) or 0)
    aid = flask_request.args.get("agent") or _primary_llama_agent_id()
    if port <= 0 or not sse_daemon.is_running() or flask_request.is_secure or not aid:
        return jsonify({"enabled": False})
    ttl = int(getattr(settings.manager.security, "stream_token_ttl_s", 300) or 300)
    token = agent_registry.issue_stream_token(aid, sse_daemon.PATH, ttl)
    host = _request_host_no_port() or "127.0.0.1"
    url = f"http://{host}:{port}{sse_daemon.PATH}?agent={quote(aid)}&token={token}"
    return jsonify({"enabled": True, "url": url})


@app.route("/api/llama-state/stream")
def stream_llama_state():
    """SSE endpoint for llama state changes. Sends the current snapshot on
    connect, then one event per (state | model | agent_online) change,
    plus a keepalive every 25s so reverse proxies don't time the
    connection out. Optional ?agent= picks a specific agent's stream."""
    from flask import stream_with_context
    import queue as _queue

    aid = flask_request.args.get("agent") or _primary_llama_agent_id()
    initial = _build_llama_state_payload(aid)

    # No resolvable agent — emit the initial 'unknown' snapshot and close.
    # The browser EventSource will auto-retry (~3s) and re-resolve the
    # primary on each reconnect, matching pre-PR1 "global subscribers"
    # liveness without leaking an unregistered queue.
    if not aid:
        def generate_oneshot():
            yield f"data: {json.dumps(initial)}\n\n"
        return app.response_class(
            stream_with_context(generate_oneshot()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # This stream is always-on per foreground tab and pins a worker for its
    # whole life. Over the stream cap, emit a one-shot snapshot and close so
    # the pill still shows current state (EventSource reconnects); no slot held.
    if not stream_pool.POOL.try_acquire():
        def generate_capped():
            yield f"data: {json.dumps(initial)}\n\n"
        return app.response_class(
            stream_with_context(generate_capped()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    q: _queue.Queue = _queue.Queue(maxsize=32)
    provider_state.STORE.subscribe("llama", aid, q)
    lifetime = float(getattr(settings.manager, "stream_max_lifetime_s", 120.0) or 120.0)
    resp = app.response_class(
        stream_with_context(_llama_state_sse(
            f"data: {json.dumps(initial)}\n\n", q,
            max_lifetime_s=lifetime,
            is_shutting_down=lambda: _shutting_down,
            on_finish=lambda: provider_state.STORE.unsubscribe("llama", aid, q),
        )),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    resp.call_on_close(stream_pool.POOL.release)
    return resp


@app.route("/api/remote/host-metrics/last", methods=["GET"])
def receive_remote_host_metrics_last():
    """Admin-gated peek at the most recent host-metrics push. Used by the
    smoke test + operators verifying the agent payload shape. Optional
    ?agent=<id> targets a specific agent; default = primary llama."""
    deny = _require_admin()
    if deny is not None:
        return deny
    aid = flask_request.args.get("agent") or _primary_llama_agent_id()
    wrap = provider_state.STORE.get("llama", aid) if aid else None
    m = (wrap or {}).get("sample") or {}
    seen = float((wrap or {}).get("last_seen") or 0.0)
    return jsonify({
        "ok": True,
        "metric": m,
        "last_seen_epoch": seen,
        "age_seconds": max(0.0, time.time() - seen) if seen else None,
        "agent_id": aid,
    })


@app.route("/api/remote/lmstudio", methods=["POST"])
def receive_lmstudio_metrics():
    """LMS agents POST LM Studio state here every 5 seconds."""
    tok = agent_registry.bearer_from_request()
    agent = agent_registry.agent_by_token(tok or "")
    if not agent:
        return jsonify({"ok": False, "error": "invalid token or not approved"}), 401
    try:
        data = flask_request.get_json(force=True)
        aid = agent["agent_id"]
        provider_state.STORE.put("lms", aid, data)
        # Per-agent online latch — log only on the False→True edge.
        if provider_state.STORE.mark_online("lms", aid):
            hw = (data.get("hardware") or {})
            log.info(f"LMS agent online — {hw.get('name','LM Studio host')} "
                     f"({hw.get('cpu','unknown CPU')}, {hw.get('ram','?')}) "
                     f"[agent {aid[:8]}@{agent.get('hostname','?')}]")
        # Drive the dynamic polling interval — fleet-aware: any LMS agent busy.
        set_lms_active(_any_lms_busy())
        return jsonify({"ok": True})
    except Exception as e:
        return _err_json("invalid request", 400, exc=e)


@app.route("/api/lmstudio/metrics")
def get_lmstudio_metrics():
    """Return latest LM Studio metrics for the primary LMS agent.
    Optional ?agent= targets a specific LMS agent."""
    aid = flask_request.args.get("agent") or _primary_lms_agent_id()
    wrap = provider_state.STORE.get("lms", aid) if aid else None
    data = dict((wrap or {}).get("sample") or {})
    last_seen = float((wrap or {}).get("last_seen") or 0.0)
    age = time.time() - last_seen if last_seen else float("inf")
    online = age < 15
    # Per-agent offline latch — log only on the True→False edge.
    if aid and not online and provider_state.STORE.mark_offline("lms", aid):
        log.warning(f"LMS agent offline — last seen {age:.0f}s ago "
                    f"[agent {aid[:8]}]")
    data["agent_online"] = online
    data["agent_age_s"]  = round(age, 1) if last_seen else None
    return jsonify(data)


@app.route("/api/lmstudio/models")
def lmstudio_models():
    """Proxy /v1/models from LM Studio Mac."""
    return proxies.proxy_to_primary("lms", "GET", "/lms/models")


@app.route("/api/lmstudio/server/status")
def lmstudio_server_status():
    return proxies.proxy_to_primary("lms", "GET", "/lms/server/status")
@app.route("/api/lmstudio/server/start", methods=["POST"])
def lmstudio_server_start():
    return proxies.proxy_to_primary("lms", "POST", "/lms/server/start")
@app.route("/api/lmstudio/server/stop", methods=["POST"])
def lmstudio_server_stop():
    return proxies.proxy_to_primary("lms", "POST", "/lms/server/stop")
@app.route("/api/lmstudio/server/restart", methods=["POST"])
def lmstudio_server_restart():
    return proxies.proxy_to_primary("lms", "POST", "/lms/server/restart", timeout=60)
@app.route("/api/lmstudio/server/log")
def lmstudio_server_log():
    """Return recent LM Studio server log lines from the primary LMS agent."""
    return proxies.proxy_to_primary("lms", "GET", "/lms/server/log")
@app.route("/api/lmstudio/load", methods=["POST"])
def lmstudio_load():
    data     = flask_request.get_json(force=True)
    return proxies.proxy_to_primary("lms", "POST", "/lms/load", json=data, timeout=60)
def _valid_model_id(s) -> bool:
    return isinstance(s, str) and bool(_MODEL_ID_RE.match(s))

# HuggingFace repo IDs are "owner/repo-name" — owner and repo are alphanumeric + ._-
# This rejects strings starting with '-' that argparse would interpret as flags.
_HF_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,95}/[A-Za-z0-9][A-Za-z0-9._\-]{0,95}$")

def _valid_hf_repo(s) -> bool:
    return isinstance(s, str) and bool(_HF_REPO_RE.match(s))


@app.route("/api/lmstudio/unload", methods=["POST"])
def lmstudio_unload():
    data     = flask_request.get_json(force=True)
    return proxies.proxy_to_primary("lms", "POST", "/lms/unload", json=data, timeout=30)


@app.route("/api/lmstudio/download", methods=["POST"])
def lmstudio_download():
    """Trigger an LM Studio model download via the primary LMS agent.
    Body: {"model": "<repo/name>"}. The agent forwards to its local LMS API."""
    data    = flask_request.get_json(force=True) or {}
    return proxies.proxy_to_primary("lms", "POST", "/lms/download", json=data, timeout=60)


LAYOUT_FILE = DATA_DIR / "layout.json"


def load_layout() -> dict:
    try:
        return json.loads(LAYOUT_FILE.read_text())
    except Exception:
        return {}

def save_layout(data: dict):
    LAYOUT_FILE.write_text(json.dumps(data))

@app.route("/api/layout", methods=["GET"])
def get_layout():
    return jsonify(load_layout())

@app.route("/api/layout", methods=["POST"])
def set_layout():
    try:
        data = flask_request.get_json(force=True)
        save_layout(data)
        return jsonify({"ok": True})
    except Exception as e:
        return _err_json("invalid request", 400, exc=e)


# ── Model aliases ───────────────────────────────────────────────────────────
# Maps llama.cpp model IDs → operator-friendly display names. UI-only —
# llama-server never sees them, so they live on the manager (not in
# config.ini, where the `alias` key collides with --alias and changes the
# OpenAI-compat model name reported by /v1/models).
ALIASES_FILE = DATA_DIR / "model_aliases.json"
_aliases_lock = threading.Lock()


def load_aliases() -> dict:
    try:
        with _aliases_lock:
            return json.loads(ALIASES_FILE.read_text())
    except Exception:
        return {}


def save_aliases(data: dict) -> None:
    with _aliases_lock:
        ALIASES_FILE.write_text(json.dumps(data, indent=2))


@app.route("/api/llm/aliases", methods=["GET"])
def llm_aliases_get():
    return jsonify(load_aliases())


_ALIAS_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f<>]")
_MODEL_ID_RE        = re.compile(r"[^A-Za-z0-9._/:-]")


def _sanitize_alias(s: str) -> str:
    return _ALIAS_FORBIDDEN_RE.sub("", str(s or "")).strip()[:80]


def _sanitize_model_id(s: str) -> str:
    return _MODEL_ID_RE.sub("", str(s or "").strip())[:200]


@app.route("/api/llm/aliases", methods=["POST"])
def llm_aliases_set():
    body = flask_request.get_json(force=True) or {}
    model_id = _sanitize_model_id(body.get("model_id") or "")
    alias    = _sanitize_alias(body.get("alias") or "")
    new_id   = _sanitize_model_id(body.get("new_model_id") or "")
    if not model_id:
        return jsonify({"ok": False, "error": "model_id required"}), 400
    data = load_aliases()
    # Rename path: move alias from model_id → new_model_id, then drop old key.
    if new_id and new_id != model_id:
        prev = data.pop(model_id, None)
        if alias:
            data[new_id] = alias
        elif prev:
            data[new_id] = prev
    elif alias:
        data[model_id] = alias
    else:
        data.pop(model_id, None)
    save_aliases(data)
    return jsonify({"ok": True, "aliases": data})


@app.route("/api/llm/aliases/<path:model_id>", methods=["DELETE"])
def llm_aliases_delete(model_id: str):
    data = load_aliases()
    data.pop(model_id, None)
    save_aliases(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Agent registry — most of this surface (load/save, by_token/bearer, the
# /api/agents/* routes, heartbeat ack assembly, TLS bundle issuance, primary-
# agent resolution, the round-robin pool, the liveness watcher) lives in the
# dedicated `agent_registry` module. It's wired up further down via
# agent_registry.set_deps(...) + agent_registry.register_routes(app), once
# all the dep callables (manager_secret, request_host_no_port, etc.) are
# defined. What stays here: the admin-CIDR allowlist + the small
# `_require_admin` gate, both consumed by agent_registry routes via set_deps.
# ---------------------------------------------------------------------------
_AGENT_ADMIN_ALLOW = (
    [s.strip() for s in os.environ["AGENT_ADMIN_ALLOW"].split(",") if s.strip()]
    if os.environ.get("AGENT_ADMIN_ALLOW")
    else list(settings.manager.security.admin_cidrs)
)


def _admin_ip_allowed(remote_addr: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    # A dual-stack listener (or upstream) can present a v4 client as an
    # IPv4-mapped IPv6 address (::ffff:192.168.1.5), which would NOT match a
    # plain-v4 CIDR like 192.168.1.0/24. Normalize it back to the v4 form so
    # trusted_cidr works regardless of how the socket surfaced the peer.
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    for entry in _AGENT_ADMIN_ALLOW:
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if ip == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            continue
    return False


def _require_admin():
    if not _admin_ip_allowed(flask_request.remote_addr or ""):
        log.warning("admin route rejected for %s", flask_request.remote_addr)
        return jsonify({"ok": False, "error": "admin gate: IP not allowed"}), 403
    # Fail closed: resolve the role authoritatively (gate value, else session/
    # bypass); a roleless agent/anon context on an admin IP is denied, not admitted.
    if auth.effective_role() != "admin":
        log.warning("admin route role-denied for %s", flask_request.remote_addr)
        return jsonify({"ok": False, "error": "admin role required", "role_denied": True}), 403
    return None


def _err_json(message, status, *, exc=None, detail=None, **extra):
    """Generic error response that never leaks exception/traceback text to the
    client; logs the detail (request path + exception/detail) server-side."""
    if exc is not None:
        log.warning("%s [%s]: %s: %s", flask_request.path, message,
                    type(exc).__name__, exc)
    elif detail is not None:
        log.warning("%s [%s]: %s", flask_request.path, message, detail)
    else:
        log.warning("%s: %s", flask_request.path, message)
    return jsonify({"ok": False, "error": message, **extra}), status


import agent_registry  # type: ignore[import-not-found]  # sibling module; script dir is on sys.path


# ---------------------------------------------------------------------------
# Dashboard authentication lives in the dedicated `auth` module. It exports
# scrypt_hash / scrypt_verify and the auth_* / write_toml_auth_mode helpers,
# and is wired into `app` via `auth.register_auth(app, ctx, ...)` further
# down — paired with the agent_registry wiring so both modules consume the
# same Context populated once all the shared dep callables are defined.
# ---------------------------------------------------------------------------


# Brand accent palettes — single source of truth for the login page and the
# dashboard logo. Selected by [manager.branding].palette; default indigo.
# Keys: accent (app-name accent + button + focus), btn_text (button label on
# the accent), glow ("r,g,b" for the focus ring / logo shadow), grad_top
# (page backdrop top tint), and the five logo colors.
_BRAND_PALETTES = {
    "teal":   {"accent": "#2f9e90", "btn_text": "#04140f", "glow": "47,158,144",
               "grad_top": "#10302b", "core": "#2f9e90", "mid": "#2a7d73",
               "stroke": "#1f5b54", "chip1": "#06140f", "chip2": "#0a1f1b"},
    "indigo": {"accent": "#5b7cfa", "btn_text": "#060a1a", "glow": "91,124,250",
               "grad_top": "#1a2040", "core": "#5b7cfa", "mid": "#4257b0",
               "stroke": "#2f3d80", "chip1": "#0a0e1f", "chip2": "#0e1430"},
    "forest": {"accent": "#5f9e74", "btn_text": "#04140a", "glow": "95,158,116",
               "grad_top": "#15321a", "core": "#5f9e74", "mid": "#4a7d5c",
               "stroke": "#365a42", "chip1": "#08140c", "chip2": "#0e220e"},
    "steel":  {"accent": "#4a90c2", "btn_text": "#04121c", "glow": "74,144,194",
               "grad_top": "#143040", "core": "#4a90c2", "mid": "#3a7095",
               "stroke": "#2a536e", "chip1": "#06121c", "chip2": "#0a1f2e"},
}
_DEFAULT_BRAND = "indigo"


def _brand_palette() -> dict:
    name = (settings.manager.branding.palette or _DEFAULT_BRAND).strip().lower()
    return _BRAND_PALETTES.get(name, _BRAND_PALETTES[_DEFAULT_BRAND])


def _brand_logo_svg(p: dict, size: int = 66) -> str:
    """The concentric-ring "Context" logo recolored to palette `p`. Mirrors
    the dashboard header logo so the two stay visually identical."""
    return f"""<svg width="{size}" height="{size}" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <g fill="none" stroke-linecap="round">
    <circle cx="16" cy="16" r="11.5" stroke="{p['stroke']}" stroke-width="1.4" opacity="0.55" stroke-dasharray="42 30" transform="rotate(20 16 16)"/>
    <circle cx="16" cy="16" r="8.5" stroke="{p['mid']}" stroke-width="1.5" opacity="0.8" stroke-dasharray="34 19" transform="rotate(-60 16 16)"/>
    <circle cx="16" cy="16" r="5.5" stroke="{p['core']}" stroke-width="1.6" stroke-dasharray="24 11" transform="rotate(140 16 16)"/>
  </g>
  <circle cx="27.5" cy="16" r="1.4" fill="{p['core']}"/>
  <circle cx="16" cy="16" r="2.4" fill="{p['core']}"/>
</svg>"""


def _brand_css_vars(p: dict) -> str:
    """`:root` custom properties consumed by the dashboard logo (injected into
    index.html by the `/` route) so it matches the login palette."""
    return (f":root{{--brand-accent:{p['accent']};--brand-core:{p['core']};"
            f"--brand-mid:{p['mid']};--brand-stroke:{p['stroke']};"
            f"--brand-chip1:{p['chip1']};--brand-chip2:{p['chip2']};}}")


import auth  # type: ignore[import-not-found]  # sibling module; script dir is on sys.path
# auth.register_auth(app, ctx, ...) is called further down — alongside
# agent_registry.set_deps + register_routes — so both modules share the
# same Context built once after every dep callable (manager_secret,
# pki_ensure_ca, ...) is defined. Auth's @before_request
# gate is happy to register late; it only fires once Flask serves a
# request, which doesn't happen until app.run() at the bottom of the file.




# ── Admin endpoints (IP-gated) ──

_LATEST_AGENT_VERSION_CACHE: dict[str, Any] = {"v": None, "mtime": 0.0}

def _latest_agent_version() -> "str | None":
    """Parse VERSION = "..." out of the manager's local copy of
    agent/llm-systems-agent.py. Cached by mtime so we re-read only
    when the source actually changes (e.g. after `git pull`)."""
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[2] / "agent" / "llm-systems-agent.py"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return _LATEST_AGENT_VERSION_CACHE["v"]
    if mt == _LATEST_AGENT_VERSION_CACHE["mtime"]:
        return _LATEST_AGENT_VERSION_CACHE["v"]
    try:
        with open(p, "r") as f:
            for raw in f:
                m = re.match(r'^VERSION\s*=\s*["\'](.+?)["\']', raw)
                if m:
                    _LATEST_AGENT_VERSION_CACHE["v"] = m.group(1)
                    _LATEST_AGENT_VERSION_CACHE["mtime"] = mt
                    return m.group(1)
    except OSError:
        pass
    return _LATEST_AGENT_VERSION_CACHE["v"]


# Systemd units the admin tab is allowed to restart, mapped from the short
# service key the frontend sends. Both run on the manager host (the AE only
# when co-located — gated on install_topology()["ae_local_unit"]).
_RESTARTABLE_UNITS = {
    "manager": "llm-systems-manager.service",
    "alarm_engine": "llm-systems-alarm-engine.service",
}


def _sudo_allows(unit_cmd: list) -> "tuple[bool, str]":
    """Pre-flight: does the sudoers policy permit `unit_cmd` for this user with
    no password? `sudo -n -l <cmd>` checks the policy WITHOUT executing — exit 0
    = allowed. Lets the self-restart path report a missing/denied grant instead
    of claiming success the operator can't see fail. Returns (allowed, reason)."""
    try:
        p = subprocess.run(["sudo", "-n", "-l"] + unit_cmd, capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            return True, ""
        return False, ("not permitted by sudoers — run install-manager.sh to install "
                       "the restart grant (/etc/sudoers.d/llm-systems-manager)")
    except Exception as e:
        log.warning("sudo pre-flight failed: %s: %s", type(e).__name__, e)
        return False, "sudo pre-flight failed"


@app.route("/api/admin/service/<svc>/restart", methods=["POST"])
def admin_service_restart(svc: str):
    """Restart the manager or alarm-engine systemd unit from the admin tab.
    Requires admin role + admin CIDR. Needs the sudoers fragment installed by
    install-manager.sh granting NOPASSWD systemctl restart on these two units."""
    deny = _require_admin()
    if deny is not None:
        return deny
    unit = _RESTARTABLE_UNITS.get(svc)
    if not unit:
        return jsonify({"ok": False, "error": f"unknown service '{svc}'"}), 400
    # ae_local_unit = the AE unit file exists here, the precise precondition for
    # a local systemctl restart (reuses install_topology, no getaddrinfo).
    if svc == "alarm_engine" and not install_topology()["ae_local_unit"]:
        return jsonify({"ok": False,
                        "error": "alarm engine runs on a separate host — restart it there"}), 400
    # Absolute path so the invocation matches the sudoers grant exactly — sudo
    # matches the resolved binary path, and the agent uses /usr/bin/systemctl too.
    unit_cmd = ["/usr/bin/systemctl", "--no-block", "restart", unit]
    sysctl = ["sudo", "-n"] + unit_cmd
    if svc == "manager":
        # Self-restart kills this process, so the real return code is never
        # observable. Pre-flight the sudoers grant so we don't report a success
        # the operator can't see fail (missing fragment / denial).
        ok, why = _sudo_allows(unit_cmd)
        if not ok:
            return jsonify({"ok": False, "error": why}), 500
        # Flush the 200 first, then ask systemd to restart us. --no-block hands
        # the job to PID 1 so it completes even though this process is killed;
        # start_new_session detaches the systemctl client.
        def _delayed_self_restart():
            time.sleep(1.0)
            try:
                subprocess.Popen(sysctl, start_new_session=True)
            except Exception:
                logging.exception("manager self-restart spawn failed")
        threading.Thread(target=_delayed_self_restart, daemon=True).start()
        logging.warning("manager self-restart requested via admin tab")
        return jsonify({"ok": True, "restarting": True,
                        "note": "manager restarting — the dashboard will be briefly unavailable"})
    try:
        p = subprocess.run(sysctl, capture_output=True, text=True, timeout=15)
        if p.returncode == 0:
            logging.warning("alarm engine restart requested via admin tab")
            return jsonify({"ok": True, "restarting": True})
        err = (p.stderr or p.stdout or "").strip()[:300] or f"systemctl exited {p.returncode}"
        return jsonify({"ok": False, "error": err}), 500
    except Exception as e:
        logging.exception("alarm engine restart failed")
        return _err_json("alarm engine restart failed", 500, exc=e)


@app.route("/api/admin/system-health", methods=["GET"])
def admin_system_health():
    """One-shot health snapshot for the admin tab's System Health card.
    Aggregates service reachability + data-flow freshness + flag state
    so the operator gets a single panel that surfaces choke points."""
    deny = _require_admin()
    if deny is not None:
        return deny

    now = time.time()
    health: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "manager": {
            "ok": True,
            "uptime_s": round(now - _manager_startup_ts, 1),
            # SSE stream pool: active/limit + peak high-water + refusals since
            # boot. peak==limit or refusals>0 means streams hit the cap and got
            # 503'd (the browser "Stream error"), even after it self-heals.
            "streams": stream_pool.POOL.stats(),
        },
        "services": [],
        "agents": [],
        "data_flow": {},
        "warnings": [],
        # Whether the alarm engine unit is installed here — gates the AE restart
        # button in the admin tab (the manager can only systemctl a local unit).
        "ae_local": install_topology()["ae_local_unit"],
    }

    # ── Alarm engine reachability (cheap GET) ──
    # Anti-flap: track consecutive failures. A single slow /health response
    # under load (InfluxDB query spike) shouldn't flip the dashboard to DOWN
    # if the next poll succeeds. Only emit the "unreachable" warning after
    # two consecutive failures — covers transient slowness without hiding a
    # real outage for long (next 20s frontend poll catches it).
    ae_url = _alarm_engine_url or ""
    if ae_url:
        try:
            t0 = time.perf_counter()
            r = _ae_session.get(ae_url.rstrip("/") + "/health", timeout=5)
            dur = (time.perf_counter() - t0) * 1000
            ae_ok = r.ok
            try:
                info = r.json() if r.ok else {}
            except Exception:
                info = {}
            _ae_health_state["consecutive_failures"] = 0
            # AE TLS state surfaces in the admin tab. components.tls is set by
            # the AE launcher: enabled=true + active=true → serving HTTPS;
            # enabled=true + active=false → cert missing (error in payload).
            _tls_info = (info.get("components") or {}).get("tls") if isinstance(info, dict) else None
            health["services"].append({
                "name": "alarm_engine",
                "ok": ae_ok and info.get("status") == "ok",
                "url": ae_url,
                "latency_ms": round(dur, 1),
                "status_code": r.status_code,
                "tls": _tls_info,
            })
            # Surface tls_enabled-but-missing-cert as a top-level warning so the
            # admin tab can flag it prominently (matches the user spec: "error
            # ... also displayed in the admin tab").
            if isinstance(_tls_info, dict) and _tls_info.get("enabled") and not _tls_info.get("active"):
                err = _tls_info.get("error") or "alarm engine TLS enabled but inactive"
                health["warnings"].append(f"alarm engine TLS: {err}")
            # Influx state piggybacks on the alarm engine /health payload —
            # components.influxdb is "connected" when reachable.
            comps = (info.get("components") or {}) if isinstance(info, dict) else {}
            influx_ok = comps.get("influxdb") == "connected"
            health["services"].append({
                "name": "influxdb",
                "ok": influx_ok,
                "via": "alarm_engine",
                "state": comps.get("influxdb", "unknown"),
            })
            if not ae_ok:
                health["warnings"].append(f"alarm engine returned HTTP {r.status_code}")
        except Exception as e:
            _ae_health_state["consecutive_failures"] += 1
            sustained = _ae_health_state["consecutive_failures"] >= 2
            log.warning("system-health: alarm engine probe failed: %s: %s", type(e).__name__, e)
            health["services"].append({
                "name": "alarm_engine",
                "ok": not sustained,
                "url": ae_url,
                "error": type(e).__name__ if sustained else "slow probe (transient)",
            })
            health["services"].append({
                "name": "influxdb",
                "ok": not sustained,
                "via": "alarm_engine (unreachable)" if sustained else "alarm_engine (slow)",
            })
            if sustained:
                health["warnings"].append(f"alarm engine unreachable: {type(e).__name__}")
    else:
        health["services"].append({"name": "alarm_engine", "ok": False, "error": "ALARM_ENGINE_URL not configured"})
        health["warnings"].append("ALARM_ENGINE_URL not configured")

    # ── Agent fleet ──
    data = agent_registry.load_agents()
    primary_llama_id = (data.get("global") or {}).get("primary_llama_id")
    primary_lms_id   = (data.get("global") or {}).get("primary_lms_id")
    # Whether the registry contains AT LEAST ONE approved agent advertising
    # each capability. Used below to suppress 'no push yet' warnings on a
    # fresh install — until an agent of that type is approved, the absence
    # of metrics is expected, not an alert condition.
    _approved_caps = [
        (a.get("capabilities") or {})
        for a in (data.get("agents") or {}).values()
        if a.get("status") == "approved"
    ]
    has_llama_agent = any(c.get("llama") for c in _approved_caps)
    has_lms_agent   = any(c.get("lms")   for c in _approved_caps)
    for aid, agent in data.get("agents", {}).items():
        liveness = agent_registry.agent_liveness(agent)
        last_hb = agent.get("last_heartbeat")
        age = None
        if last_hb:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_hb)).total_seconds()
            except Exception:
                age = None
        bind = agent.get("bind_url") or ""
        cert_issued = agent.get("last_cert_issued_at")
        if bind.startswith("https://"):
            tls_state = "tls"
        elif cert_issued:
            tls_state = "pending"
        else:
            tls_state = "http"
        # Bidirectional TLS state for the admin tab indicator. Manager→agent is
        # https whenever bind_url is https (and the agent is serving TLS);
        # agent→manager is whatever the agent reported on its last heartbeat
        # (control_channel_tls). "both" = ↔ in the UI, "in" = manager→agent only,
        # "out" = agent→manager only (rare), "none" = plain HTTP both ways.
        mgr_to_agent_tls = bind.startswith("https://")
        agent_to_mgr_tls = bool((agent.get("last_heartbeat_data") or {}).get("control_channel_tls"))
        if mgr_to_agent_tls and agent_to_mgr_tls:
            tls_direction = "both"
        elif mgr_to_agent_tls:
            tls_direction = "in"
        elif agent_to_mgr_tls:
            tls_direction = "out"
        else:
            tls_direction = "none"
        health["agents"].append({
            "id": aid[:8],
            "hostname": agent.get("hostname"),
            "version":  agent.get("version"),
            "role":     agent.get("role"),
            "status":   agent.get("status"),
            "liveness": liveness,
            "last_heartbeat_age_s": round(age, 1) if age is not None else None,
            "is_primary_llama": aid == primary_llama_id,
            "is_primary_lms":   aid == primary_lms_id,
            "tls": tls_state,
            "tls_direction": tls_direction,
            "control_channel_tls": agent_to_mgr_tls,
            "cert_issued_at": cert_issued,
            "bind_url": bind or None,
        })

    # ── Data flow ──
    host_last_seen = _primary_llama_last_seen()
    host_age = round(now - host_last_seen, 1) if host_last_seen else None
    host_agent_id = _primary_llama_agent_id()

    lms_last_seen = _primary_lms_last_seen()
    lms_age = round(now - lms_last_seen, 1) if lms_last_seen else None

    primary_llama_push_ok = bool(host_age is not None and host_age < 60)
    primary_lms_push_ok = bool(lms_age is not None and lms_age < 30)

    health["data_flow"] = {
        "primary_llama_push": {
            "ok": primary_llama_push_ok,
            "agent_id": (host_agent_id or "")[:8] if host_agent_id else None,
            "age_s": host_age,
            "has_agent": has_llama_agent,
        },
        "primary_lms_push": {
            "ok": primary_lms_push_ok,
            "age_s": lms_age,
            "has_agent": has_lms_agent,
        },
        "manager_to_alarm_forwarding": {
            "active": False,
            "reason": "agent forwards directly to alarm engine",
        },
    }

    # ── Derive warnings ──
    if has_llama_agent and not primary_llama_id:
        health["warnings"].append("no default llama agent set — the dashboard has no default host to show (pushes are still stored per-agent)")
    if has_llama_agent and host_age is None:
        health["warnings"].append("no host-metrics push received yet (primary llama agent may not be running)")
    if host_age is not None and host_age > 60:
        health["warnings"].append(f"primary llama host-metrics push is stale ({host_age:.0f}s old)")
    if has_lms_agent and lms_age is not None and lms_age > 30:
        health["warnings"].append(f"LMS push is stale ({lms_age:.0f}s old)")
    down_agents = [a for a in health["agents"] if a["status"] == "approved" and a["liveness"] == "down"]
    if down_agents:
        health["warnings"].append(f"{len(down_agents)} approved agent(s) down: " + ", ".join(a["hostname"] or a["id"] for a in down_agents))

    _cert_warn_within_days = 14
    _now = datetime.now(timezone.utc)
    for aid, reg in (data.get("agents") or {}).items():
        if reg.get("status") != "approved":
            continue
        hb_data = reg.get("last_heartbeat_data") or {}
        exp_iso = hb_data.get("tls_expires_at")
        if not exp_iso:
            continue
        with best_effort("system health: agent TLS cert expiry check"):
            exp = datetime.fromisoformat(str(exp_iso).replace("Z", "+00:00"))
            remaining_days = (exp - _now).total_seconds() / 86400.0
            host = reg.get("hostname") or aid[:8]
            if remaining_days < 0:
                health["warnings"].append(
                    f"agent {host} TLS cert EXPIRED ({-remaining_days:.0f}d ago)"
                )
            elif remaining_days < _cert_warn_within_days:
                health["warnings"].append(
                    f"agent {host} TLS cert expires in {remaining_days:.0f}d (auto-rotation may be stuck)"
                )

    # Also surveil the manager's own TLS server cert when MANAGER_TLS_PORT is on.
    mgr_crt = DATA_DIR / "manager-tls.crt"
    if mgr_crt.is_file():
        with best_effort("system health: manager TLS cert expiry check"):
            from cryptography import x509 as _x509_warn
            cur = _x509_warn.load_pem_x509_certificate(mgr_crt.read_bytes())
            remaining_days = (cur.not_valid_after_utc - _now).total_seconds() / 86400.0
            if remaining_days < 0:
                health["warnings"].append(
                    f"manager TLS cert EXPIRED ({-remaining_days:.0f}d ago) — restart manager to reissue"
                )
            elif remaining_days < _cert_warn_within_days:
                health["warnings"].append(
                    f"manager TLS cert expires in {remaining_days:.0f}d — will auto-reissue on next restart"
                )

    _sp = stream_pool.POOL.stats()
    if _sp["refusals"] > 0 or _sp["active"] >= _sp["limit"]:
        health["warnings"].append(
            f"SSE stream pool saturated: active={_sp['active']}/{_sp['limit']} "
            f"peak={_sp['peak']} refusals={_sp['refusals']} — new streams get 503 "
            f"('Stream error'); restart clears it")

    health["overall"] = "ok" if not health["warnings"] else ("warn" if all("stale" not in w and "unreachable" not in w and "down" not in w for w in health["warnings"]) else "down")
    return jsonify(health)


@app.route("/api/admin/stream-stats", methods=["GET"])
def admin_stream_stats():
    """Live SSE-stream + connection health for the Manager-tab card: manager
    pool active/peak/refusals, Cheroot worker threads + backlog, browser/agent
    connection counts, and each agent's /status streams. Cached snapshot
    refreshed by the stream-health loop; the same numbers go to the alarm
    engine as manager_streams/agent_streams for rule-based alerting."""
    deny = _require_admin()
    if deny is not None:
        return deny
    return jsonify(stream_health.snapshot())


# Cache for AE + InfluxDB versions. Both endpoints are cheap but we
# poll /api/agents on a 5s interval from the admin tab, so collapse to
# one probe per TTL window.
_infra_version_cache: dict = {"at": 0.0, "ae": None, "influxdb": None}
_INFRA_VERSION_TTL_S = 30.0


def _refresh_infra_versions() -> None:
    now = time.time()
    if now - _infra_version_cache["at"] < _INFRA_VERSION_TTL_S:
        return
    _infra_version_cache["at"] = now
    # AE /health also reports the InfluxDB version it just pinged.
    # Prefer that — on split installs the manager often can't reach
    # InfluxDB directly (firewall: only the AE host is allowed), so a
    # direct /ping from here would fail even though Influx is healthy.
    if _alarm_engine_url:
        with best_effort("infra versions: probe AE health"):
            r = _ae_session.get(f"{_alarm_engine_url.rstrip('/')}/health", timeout=2)
            if r.ok:
                body = r.json() or {}
                _infra_version_cache["ae"] = body.get("version")
                ix_v = (body.get("components") or {}).get("influxdb_version")
                if ix_v:
                    _infra_version_cache["influxdb"] = ix_v
    # Fallback: only try the direct /ping when AE didn't tell us
    # (older AE without influxdb_version, or AE itself is down).
    if not _infra_version_cache.get("influxdb"):
        ix_host = settings.influxdb.host
        ix_port = settings.influxdb.port
        if ix_host:
            with best_effort("infra versions: probe InfluxDB ping"):
                r = requests.get(f"http://{ix_host}:{ix_port}/ping", timeout=2)
                v = r.headers.get("X-Influxdb-Version")
                if v:
                    _infra_version_cache["influxdb"] = v





# ---------------------------------------------------------------------------
# agent proxy infrastructure
# ---------------------------------------------------------------------------

MANAGER_SECRET_FILE = DATA_DIR / "manager_secret"

_pki_module = None  # backend._pki module, imported lazily
_pki_ca = None      # tuple (cert, key) once loaded


def _pki_ensure_ca():
    """Lazy-load _pki and return (ca_cert, ca_key, module).

    Returning the module alongside the CA collapses what used to be a
    two-callable pattern (`ensure_ca()` + a separate `module_getter()`)
    into one call — every cert issuer needs the CA AND the signing
    helpers from the module, and splitting them across two reads leaks
    the loader's `_pki_module is None` ordering into every caller.
    """
    global _pki_module, _pki_ca
    if _pki_module is None:
        import importlib.util as _il
        _pki_path = Path(__file__).resolve().parent / "_pki.py"
        _spec = _il.spec_from_file_location("_pki", _pki_path)
        _pki_local = _il.module_from_spec(_spec)
        _spec.loader.exec_module(_pki_local)  # type: ignore[union-attr]
        _pki_module = _pki_local
    if _pki_ca is None:
        cert, key = _pki_module.load_or_create_ca(DATA_DIR)
        _pki_ca = (cert, key)
    return (_pki_ca[0], _pki_ca[1], _pki_module)


def _manager_secret() -> bytes:
    try:
        b = MANAGER_SECRET_FILE.read_bytes()
        if b:
            return b
    except FileNotFoundError:
        pass
    MANAGER_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    b = os.urandom(32)
    MANAGER_SECRET_FILE.write_bytes(b)
    try:
        os.chmod(MANAGER_SECRET_FILE, 0o600)
    except OSError:
        pass
    log.info("generated manager HMAC secret at %s", MANAGER_SECRET_FILE)
    return b


# Sign Flask session cookies with a SECOND, ephemeral secret regenerated on
# every process start. Restarting the manager therefore invalidates every
# browser session and forces every operator to log in again — desired so a
# leaked or shared cookie can be revoked by `systemctl restart`. The stream-
# token / heartbeat HMAC keeps using the persistent _manager_secret() so
# agents don't have to reload it cold on every manager restart (they pick up
# changes within one heartbeat anyway, but in-memory churn here would race
# with the heartbeat cadence and break SSE/PTY tokens for ~60s post-restart).
_SESSION_SECRET = os.urandom(32)
app.secret_key = _SESSION_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(
        days=max(1, int(settings.manager.auth.session_lifetime_days))),
)



# Wire auth + agent_registry into the Flask app. Has to wait until here
# because the shared dep callables (manager_secret, alarm_engine_url, the
# proxy flag reader, the PKI lazy-loader, set_llama_awake / get_interval,
# the infra-version cache, etc.) are all defined earlier in this file.
# Build the app_context.Context once and hand the same instance to both
# modules so neither has to re-thread the 9 cross-module shared deps —
# only their module-specific kwargs stay on the register_*/set_deps
# signatures. agent_registry.agent_by_token / bearer_from_request are
# captured by direct reference at register_auth time (snapshot at startup,
# invoked at request time); they're safe to use as kwargs here because
# agent_registry's module-level definitions don't depend on its _deps
# being populated yet — only the callsites inside the request lifecycle do.
#
# A note on the lambda wrappers below: `alarm_engine_url=lambda: _alarm_engine_url`
# and `agent_admin_allow=lambda: list(_AGENT_ADMIN_ALLOW)` are load-bearing —
# they wrap mutable module-level state (a string flipped http→https at
# manager-TLS startup; a CIDR list editable via the admin tab) into the
# Callable[..., ...] contract declared on Context. `manager_secret=_manager_secret`
# is bare because _manager_secret is already a function. Don't "simplify"
# either by collapsing the patterns: dropping a lambda would pass a value
# where Context's type contract expects a getter, crashing every consumer.
import app_context  # type: ignore[import-not-found]  # sibling module; script dir is on sys.path
import proxies      # type: ignore[import-not-found]  # sibling; PR M4
ctx = app_context.Context(
    settings=settings,
    data_dir=DATA_DIR,
    version=__version__,
    require_admin=_require_admin,
    admin_ip_allowed=_admin_ip_allowed,
    agent_admin_allow=lambda: list(_AGENT_ADMIN_ALLOW),
    alarm_engine_url=lambda: _alarm_engine_url,
    manager_secret=_manager_secret,
    ae_session=_ae_session,
)
auth.register_auth(
    app,
    ctx,
    config_path=CONFIG_PATH,
    agent_by_token=agent_registry.agent_by_token,
    bearer_from_request=agent_registry.bearer_from_request,
    brand_palette=_brand_palette,
    brand_logo_svg=_brand_logo_svg,
)
agent_registry.set_deps(
    ctx,
    request_host_no_port=_request_host_no_port,
    rewrite_loopback_host=_rewrite_loopback_host,
    set_llama_awake=set_llama_awake,
    get_interval=get_interval,
    latest_agent_version=_latest_agent_version,
    refresh_infra_versions=_refresh_infra_versions,
    infra_version_get=lambda k: _infra_version_cache.get(k),
    hostname=_HOSTNAME,
    loopback_hosts=_LOOPBACK_HOSTS,
    pki_ensure_ca=_pki_ensure_ca,
)
agent_registry.register_routes(app)
import terminal  # type: ignore[import-not-found]  # sibling; PR M3
terminal.register_routes(app, ctx)
proxies.register_routes(
    app, ctx,
    repo_root=_REPO_ROOT_PATH,
    install_topology=install_topology,
    request_host_no_port=_request_host_no_port,
    rewrite_loopback_host=_rewrite_loopback_host,
)
import openclaw  # type: ignore[import-not-found]  # sibling; PR M5
openclaw.register_routes(app, ctx)
model_profiles.register_routes(app, ctx, profiles_path=DATA_DIR / "model_profiles.json")
import manager_users  # type: ignore[import-not-found]  # sibling
manager_users.init(
    DATA_DIR / "manager_users.json",
    threshold=settings.manager.auth.lockout_threshold,
    window_s=settings.manager.auth.lockout_window_s,
    duration_s=settings.manager.auth.lockout_duration_s,
)
# Seed the first admin from the legacy single credential (upgrade path).
# Skipped under pytest so the eager test import never writes the live store.
if "pytest" not in sys.modules:
    _seed_user, _seed_hash, _ = auth.auth_credential()
    manager_users.STORE.seed_admin(_seed_user, _seed_hash)
manager_users.register_routes(app, ctx)


# _proxy_to_primary + _proxy_stream_to_primary moved to proxies.py (PR M4).
# Call sites use proxies.proxy_to_primary(...) / proxies.proxy_stream_to_primary(...).


@app.route("/api/admin/llama-models", methods=["GET"])
def admin_llama_models():
    deny = _require_admin()
    if deny is not None:
        return deny

    data = agent_registry.load_agents()
    glob = data.get("global") or {}
    pool = glob.get("llama_pool") or []
    agents_map = data.get("agents") or {}

    # Walk the pool; if pool is empty, fall back to every approved
    # agent with llama capability so the editor still works pre-pool.
    candidates = []
    if pool:
        for aid in pool:
            a = agents_map.get(aid)
            if a and a.get("status") == "approved":
                candidates.append(a)
    else:
        for a in agents_map.values():
            if a.get("status") == "approved" and (a.get("capabilities") or {}).get("llama"):
                candidates.append(a)

    seen: dict[str, list[str]] = {}
    errors: list[dict] = []
    for agent in candidates:
        resp, _tried, err = agent_registry.agent_request(
            "GET", agent, "/llama/models",
            headers={"Authorization": f"Bearer {agent['token']}"},
            timeout=5,
        )
        if resp is None or not resp.ok:
            if err:
                log.warning("model-list fan-out: agent %s request failed: %s",
                            agent.get("hostname"), err)
            errors.append({
                "agent": agent.get("hostname"),
                "error": (resp.status_code if resp else "no-response"),
            })
            continue
        try:
            body = resp.json() or {}
        except Exception:
            errors.append({"agent": agent.get("hostname"), "error": "non-JSON response"})
            continue
        # /llama/models mirrors llama-server's /v1/models which returns
        # {"object":"list","data":[{"id":"...","object":"model",...}]}
        for entry in (body.get("data") or body.get("models") or []):
            mid = (entry or {}).get("id") if isinstance(entry, dict) else entry
            if not mid:
                continue
            seen.setdefault(str(mid), []).append(agent.get("hostname") or agent["agent_id"][:8])

    out = [{"id": mid, "agents": sorted(set(hosts))} for mid, hosts in sorted(seen.items())]
    return jsonify({"ok": True, "models": out, "errors": errors})


@app.route("/api/admin/llama-pins", methods=["POST"])
def admin_llama_pins():
    deny = _require_admin()
    if deny is not None:
        return deny
    body = flask_request.get_json(force=True) or {}
    model_id = (body.get("model_id") or "").strip()
    agent_id = (body.get("agent_id") or "").strip()
    if not model_id:
        return jsonify({"ok": False, "error": "model_id required"}), 400

    with agent_registry.agents_lock:
        data = agent_registry.load_agents()
        glob = data.setdefault("global", {})
        pins = dict(glob.get("llama_model_pins") or {})
        if agent_id:
            agent = data["agents"].get(agent_id)
            if not agent:
                return jsonify({"ok": False, "error": "unknown agent"}), 404
            caps = agent.get("capabilities", {}) or {}
            if not caps.get("llama"):
                return jsonify({"ok": False,
                                "error": "agent does not advertise llama capability"}), 400
            pins[model_id] = agent_id
        else:
            pins.pop(model_id, None)
        glob["llama_model_pins"] = pins
        agent_registry.save_agents(data)
    log.info("llama_model_pins update by %s: model=%s -> agent=%s",
             flask_request.remote_addr, model_id, agent_id or "(cleared)")
    return jsonify({"ok": True, "llama_model_pins": pins})


# ---------------------------------------------------------------------------
# Admin backup / restore — manager-side
# ---------------------------------------------------------------------------
# Builds an encrypted LSMENC archive (backend/_archive.py) containing every
# piece of operator state the manager owns: the unified TOML config, the
# agent registry, the internal CA pair, model_benchmarks SQLite, and the
# UI layout / alias files. Designed for migrating a 1-server prod install
# onto a split topology where the new servers inherit the old IPs — the
# new manager imports the archive and every existing remote agent keeps
# working without re-approval (CA + bearer tokens both survive).
# ---------------------------------------------------------------------------

import _archive  # type: ignore[import-not-found]  # sibling module; script dir is on sys.path

_MANAGER_EXPORT_FILES = [
    "config/llm-systems.toml",
    "data/agents.json",
    "data/internal-ca.crt",
    "data/internal-ca.key",
    "data/layout.json",
    "data/manager_secret",
    "data/model_aliases.json",
]
_MANAGER_EXPORT_SQLITE = ["data/metrics.db"]

# Two-layer import model. The export bundles EVERYTHING (so a single archive
# covers both use cases) but the import asks the operator which layers to
# apply:
#   - "config" (default ON): non-secret settings the operator wants to copy
#     between hosts — TOML, UI layout, model aliases, model_benchmarks DB.
#     Safe to import into ANY manager; the target keeps its own identity.
#   - "identity" (default OFF): the internal CA (cert + private key), the
#     manager HMAC secret, and the agent registry (bearer tokens). Importing
#     this REPLACES the target's identity with the archive's — every leaf
#     cert the target auto-issues from then on chains to the archive's CA,
#     and the archive's bearer tokens become this manager's bearer tokens.
#     ONLY opt in when migrating a manager to a new host and the fleet
#     should keep working without re-approval.
#
# This default flipped after a field incident where importing a dev archive
# into a fresh prod install silently replaced prod's freshly-generated CA
# with dev's CA, leaving prod cryptographically forked off dev. The new
# default ("config only") matches operator intuition for "copy my settings".
_MANAGER_EXPORT_CATEGORIES = {
    "config": frozenset({
        "config/llm-systems.toml",
        "data/layout.json",
        "data/model_aliases.json",
        "data/metrics.db",
    }),
    "identity": frozenset({
        "data/internal-ca.crt",
        "data/internal-ca.key",
        "data/manager_secret",
        "data/agents.json",
    }),
}
_DEFAULT_IMPORT_CATEGORIES = ["config"]


def _file_category(arc_name: str) -> str | None:
    """Return the category an archive entry belongs to, or None if it's not
    recognized (e.g. manifest.json, unexpected entry from a future version)."""
    for cat, members in _MANAGER_EXPORT_CATEGORIES.items():
        if arc_name in members:
            return cat
    return None


def _build_manager_archive() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for rel in _MANAGER_EXPORT_FILES:
        p = _REPO_ROOT_PATH / rel
        if p.is_file():
            files[rel] = p.read_bytes()
    for rel in _MANAGER_EXPORT_SQLITE:
        p = _REPO_ROOT_PATH / rel
        if p.is_file():
            files[rel] = _archive.sqlite_snapshot(str(p))
    return files


# Topology keys exposed in the import preview + patchable on apply.
# Each entry: (label, [(section, key), ...]). The first match wins on
# read; on write we patch every section/key in the list.
_TOPOLOGY_OVERRIDES = {
    "alarm_engine_url": ("Alarm engine URL",
                         [("manager", "alarm_engine_url")]),
    "influxdb_host":    ("InfluxDB host",
                         [("influxdb", "host")]),
    "influxdb_port":    ("InfluxDB port",
                         [("influxdb", "port")]),
    "influxdb_org":     ("InfluxDB org",
                         [("influxdb", "org")]),
    "influxdb_token_metrics":        ("InfluxDB token: metrics bucket",
                                      [("influxdb.tokens", "metrics")]),
    "influxdb_token_metrics_rollup": ("InfluxDB token: metrics_rollup bucket",
                                      [("influxdb.tokens", "metrics_rollup")]),
    "influxdb_token_admin":          ("InfluxDB token: admin (optional)",
                                      [("influxdb.tokens", "admin")]),
}


def _extract_toml_topology(toml_bytes: bytes) -> tuple[dict[str, Any], str | None]:
    """Returns (topology_values, parse_error). On parse failure, the
    first element is empty and the second carries the error string —
    surfaced through the import preview so the operator sees why their
    topology fields are blank instead of just getting silence."""
    import tomllib
    try:
        cfg = tomllib.loads(toml_bytes.decode("utf-8"))
    except Exception as e:
        log.warning("import preview: config TOML parse failed: %s: %s", type(e).__name__, e)
        return {}, "could not parse config TOML"
    out: dict[str, Any] = {}
    for ovr_key, (_label, paths) in _TOPOLOGY_OVERRIDES.items():
        for section, key in paths:
            node: Any = cfg
            for part in section.split("."):
                node = (node or {}).get(part) or {}
            if isinstance(node, dict) and key in node:
                out[ovr_key] = node[key]
                break
    return out, None


def _patch_toml_lines(toml_text: str,
                      overrides: dict[str, Any]) -> tuple[str, list[str]]:
    """Line-based TOML patcher. For each override (matching a key in
    _TOPOLOGY_OVERRIDES), find `key = <value>` inside the right
    section and rewrite just the value, preserving leading whitespace
    and any trailing comment. Returns (patched_text, applied_keys)."""
    if not overrides:
        return toml_text, []
    targets: dict[tuple[str, str], Any] = {}
    for ovr_key, value in overrides.items():
        if ovr_key not in _TOPOLOGY_OVERRIDES:
            continue
        if value is None or value == "":
            continue
        _label, paths = _TOPOLOGY_OVERRIDES[ovr_key]
        for section, key in paths:
            targets[(section, key)] = value
    if not targets:
        return toml_text, []
    # Splits `key = value` lines into named groups indent/key/sp/val/tail.
    # e.g. `  port = 8081  # c` → key=port, val=8081, tail="  # c".
    line_re = re.compile(
        r'^(?P<indent>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)(?P<sp>\s*=\s*)'
        r'(?P<val>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s#]+)'
        r'(?P<tail>.*)$'
    )
    section_re = re.compile(r'^\s*\[(?P<name>[^\]]+)\]\s*(?:#.*)?$')
    current = ""
    applied: set[tuple[str, str]] = set()
    out_lines: list[str] = []
    for line in toml_text.splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        sm = section_re.match(stripped)
        if sm:
            current = sm.group("name").strip()
            out_lines.append(line)
            continue
        m = line_re.match(stripped)
        if m:
            key = m.group("key")
            tgt = targets.get((current, key))
            if tgt is not None:
                applied.add((current, key))
                if isinstance(tgt, bool):
                    val_repr = "true" if tgt else "false"
                elif isinstance(tgt, int):
                    val_repr = str(tgt)
                else:
                    s = str(tgt).replace("\\", "\\\\").replace('"', '\\"')
                    val_repr = f'"{s}"'
                eol = line[len(stripped):]  # preserve \n / \r\n
                out_lines.append(f'{m.group("indent")}{m.group("key")}{m.group("sp")}{val_repr}{m.group("tail")}{eol}')
                continue
        out_lines.append(line)
    return "".join(out_lines), [f"{s}.{k}" for s, k in sorted(applied)]


def _import_apply_manager(files: dict[str, bytes]) -> dict[str, Any]:
    """Write the supplied {arcname: bytes} mapping back to disk. Each
    target file is backed up to <path>.preimport.<ts>.bak before being
    rewritten, mode 0600, atomic via tmp + os.replace."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[str] = []
    backups: list[str] = []
    allow = set(_MANAGER_EXPORT_FILES) | set(_MANAGER_EXPORT_SQLITE)
    for arc_name, data in files.items():
        if arc_name == "manifest.json":
            continue
        if arc_name not in allow:
            log.warning("ignoring unexpected entry in import archive: %s", arc_name)
            continue
        dest = _REPO_ROOT_PATH / arc_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            bak = f"{dest}.preimport.{ts}.bak"
            shutil.copy2(dest, bak)
            os.chmod(bak, 0o600)
            backups.append(bak)
        tmp = f"{dest}.{ts}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)
        # Order matters: clear stale -wal/-shm/-journal BEFORE swapping
        # the DB file so a SQLite opener arriving mid-swap never sees
        # (new DB + old sidecars) — that combination can roll a stale
        # WAL forward against the imported DB and silently overwrite
        # the imported rows with pre-import state.
        if arc_name.endswith(".db"):
            _archive.clear_sqlite_sidecars(str(dest))
        os.replace(tmp, str(dest))
        written.append(str(dest))
    return {"written": written, "backups": backups, "ts": ts}


def _archive_manifest(component: str, files: dict[str, bytes],
                      extra: dict[str, Any] | None = None) -> bytes:
    manifest = {
        "component": component,
        "manager_version": __version__,
        "hostname": socket.gethostname(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "files": sorted([
            {"name": name, "size": len(data)}
            for name, data in files.items()
        ], key=lambda x: x["name"]),
    }
    if extra:
        manifest.update(extra)
    return json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


@app.route("/api/admin/export/manager", methods=["POST"])
def admin_export_manager():
    """Build and return the manager backup archive. POST body
    accepts {"password": "<>"}; empty/missing means an unencrypted
    archive (with a clear warning surfaced by the frontend)."""
    deny = _require_admin()
    if deny is not None:
        return deny
    body = flask_request.get_json(force=True, silent=True) or {}
    password = body.get("password") or ""
    files = _build_manager_archive()
    files["manifest.json"] = _archive_manifest("manager", files)
    try:
        tgz = _archive.pack_tar(files)
        blob = _archive.encrypt(tgz, password if password else None)
    except ValueError as e:
        return _err_json("invalid request", 400, exc=e)
    fname = f"lsm-manager-{socket.gethostname()}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.lsmenc"
    log.warning("manager export by %s (%d files, %d bytes, encrypted=%s)",
                flask_request.remote_addr, len(files), len(blob), bool(password))
    return app.response_class(
        blob, mimetype="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Lsmenc-Encrypted": "1" if password else "0",
        },
    )


def _read_import_blob() -> tuple[bytes | None, str, str | None]:
    """Extract uploaded archive + password from a multipart request.
    Returns (blob, password, error)."""
    f = flask_request.files.get("file")
    if not f:
        return None, "", "no 'file' field in upload"
    blob = f.read()
    password = flask_request.form.get("password") or ""
    return blob, password, None


def _parse_manifest(files: dict[str, bytes]) -> dict[str, Any]:
    raw = files.get("manifest.json")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


@app.route("/api/admin/import/manager/preview", methods=["POST"])
def admin_import_manager_preview():
    """Decrypt + unpack the upload, return manifest + entry list so the
    operator can confirm before applying."""
    deny = _require_admin()
    if deny is not None:
        return deny
    blob, password, err = _read_import_blob()
    if err:
        return jsonify({"ok": False, "error": err}), 400
    enc = _archive.sniff_encrypted(blob)
    if enc is None:
        return jsonify({"ok": False, "error": "not an LSMENC archive (bad magic)"}), 400
    try:
        payload = _archive.decrypt(blob, password)
    except ValueError as e:
        return _err_json("invalid request", 400, exc=e, encrypted=enc)
    try:
        files = _archive.unpack_tar(payload)
    except Exception as e:
        return _err_json("tar extraction failed", 400, exc=e)
    manifest = _parse_manifest(files)
    if manifest.get("component") and manifest["component"] != "manager":
        return jsonify({"ok": False,
                        "error": f"archive is for component '{manifest['component']}', "
                                 f"not 'manager'"}), 400
    entries = sorted([
        {"name": n, "size": len(d), "category": _file_category(n) or "other"}
        for n, d in files.items() if n != "manifest.json"
    ], key=lambda x: x["name"])
    # Surface which categories are PRESENT in this archive so the import
    # dialog can hide a toggle (e.g. "identity") when the archive has none
    # of those files. The default opt-in set lives here too so the frontend
    # doesn't need to hardcode it.
    cats_present = {e["category"] for e in entries if e["category"] != "other"}
    import_categories = {
        "available": sorted(cats_present),
        "default_apply": [c for c in _DEFAULT_IMPORT_CATEGORIES if c in cats_present],
        "labels": {
            "config":   "Config (TOML, layout, model aliases, benchmarks DB)",
            "identity": "Identity (internal CA, manager HMAC, agent registry)",
        },
        "descriptions": {
            "config":   "Safe to import into any manager. Keeps the target's "
                        "own cryptographic identity intact.",
            "identity": "REPLACES this host's CA, HMAC secret, and agent "
                        "registry with the archive's. Only opt in when "
                        "migrating a manager to a new host and the existing "
                        "fleet should keep working without re-approval.",
        },
    }
    topology: dict[str, Any] = {}
    topology_error: str | None = None
    toml_bytes = files.get("config/llm-systems.toml")
    if toml_bytes:
        topology, topology_error = _extract_toml_topology(toml_bytes)
    topology_schema = [
        {"key": k, "label": label} for k, (label, _) in _TOPOLOGY_OVERRIDES.items()
    ]
    return jsonify({"ok": True, "encrypted": enc, "manifest": manifest,
                    "entries": entries, "topology": topology,
                    "topology_schema": topology_schema,
                    "topology_error": topology_error,
                    "import_categories": import_categories})


@app.route("/api/admin/import/manager/apply", methods=["POST"])
def admin_import_manager_apply():
    """Write the archive's contents to disk. Each touched file is
    backed up first. The manager does NOT auto-restart — the operator
    must restart the service for config and registry changes to take
    effect."""
    deny = _require_admin()
    if deny is not None:
        return deny
    blob, password, err = _read_import_blob()
    if err:
        return jsonify({"ok": False, "error": err}), 400
    try:
        payload = _archive.decrypt(blob, password)
        files = _archive.unpack_tar(payload)
    except ValueError as e:
        return _err_json("invalid request", 400, exc=e)
    manifest = _parse_manifest(files)
    if manifest.get("component") and manifest["component"] != "manager":
        return jsonify({"ok": False,
                        "error": f"archive is for '{manifest['component']}', not 'manager'"}), 400
    # Category filter. Default is config-only (see _DEFAULT_IMPORT_CATEGORIES) —
    # importing identity is an opt-in that the frontend's dialog asks the
    # operator to confirm explicitly, because it REPLACES the target's CA +
    # HMAC + agent registry with the archive's.
    cats_raw = flask_request.form.get("categories")
    if cats_raw:
        try:
            categories = json.loads(cats_raw)
            if not isinstance(categories, list) or \
               not all(isinstance(c, str) for c in categories):
                raise ValueError("categories must be a JSON array of strings")
        except Exception as e:
            return _err_json("bad categories", 400, exc=e)
    else:
        categories = list(_DEFAULT_IMPORT_CATEGORIES)
    unknown = [c for c in categories if c not in _MANAGER_EXPORT_CATEGORIES]
    if unknown:
        return jsonify({"ok": False,
                        "error": f"unknown import categories: {unknown}"}), 400
    keep = set().union(*(_MANAGER_EXPORT_CATEGORIES[c] for c in categories))
    # manifest.json is always preserved through unpack; _import_apply_manager
    # ignores it anyway via the explicit allow-list.
    skipped: list[str] = []
    filtered: dict[str, bytes] = {}
    for name, data in files.items():
        if name == "manifest.json" or name in keep:
            filtered[name] = data
        elif _file_category(name) is not None:
            skipped.append(name)
        # Anything else (unrecognized entries) drops through — _import_apply_manager
        # rejects unknown arc_names anyway via its allow-list.
    files = filtered
    # Topology overrides: multipart 'topology_overrides' field carries
    # a JSON dict {ovr_key: new_value}. Patched into the TOML bytes
    # before _import_apply_manager writes anything to disk.
    overrides_raw = flask_request.form.get("topology_overrides") or "{}"
    try:
        overrides = json.loads(overrides_raw) if overrides_raw else {}
        if not isinstance(overrides, dict):
            raise ValueError("topology_overrides must be a JSON object")
    except Exception as e:
        return _err_json("bad topology_overrides", 400, exc=e)
    patched_keys: list[str] = []
    if overrides and files.get("config/llm-systems.toml"):
        try:
            old_text = files["config/llm-systems.toml"].decode("utf-8")
            new_text, patched_keys = _patch_toml_lines(old_text, overrides)
            files["config/llm-systems.toml"] = new_text.encode("utf-8")
        except Exception as e:
            return _err_json("TOML patch failed", 400, exc=e)
    try:
        result = _import_apply_manager(files)
    except Exception as e:
        log.exception("manager import failed")
        return _err_json("internal error", 500, exc=e)
    log.warning("manager import applied by %s: categories=%s, %d files written, "
                "%d skipped (filtered out), backups=%s, patched=%s",
                flask_request.remote_addr, ",".join(categories),
                len(result["written"]), len(skipped), result["ts"],
                ",".join(patched_keys) or "none")
    return jsonify({"ok": True, **result, "patched_toml_keys": patched_keys,
                    "categories_applied": categories,
                    "skipped_by_category": skipped,
                    "note": "Restart the manager service for the imported "
                            "config + agent registry to take effect."})




@app.route("/api/agents/<agent_id>/status-check", methods=["POST"])
def agents_status_check(agent_id: str):
    deny = _require_admin()
    if deny is not None:
        return deny
    data = agent_registry.load_agents()
    agent = data["agents"].get(agent_id)
    if not agent or not agent.get("bind_url"):
        return jsonify({"ok": False, "error": "unknown agent or no bind_url"}), 404
    t0 = time.perf_counter()
    r, tried, err = agent_registry.agent_request("GET", agent, "/identify", timeout=5)
    dur_ms = (time.perf_counter() - t0) * 1000
    if r is None:
        return _err_json("upstream agent request failed", 502, detail=err,
                         tried=tried, latency_ms=round(dur_ms, 1))
    return jsonify({"ok": r.ok, "status_code": r.status_code,
                    "latency_ms": round(dur_ms, 1),
                    "tried": tried,
                    "data": r.json() if r.ok else "upstream agent returned an error"})


@app.route("/api/agents/<agent_id>/restart", methods=["POST"])
def agents_restart(agent_id: str):
    deny = _require_admin()
    if deny is not None:
        return deny
    data = agent_registry.load_agents()
    agent = data["agents"].get(agent_id)
    if not agent or not agent.get("bind_url") or not agent.get("token"):
        return jsonify({"ok": False, "error": "unknown agent or missing token"}), 404
    r, tried, err = agent_registry.agent_request(
        "POST", agent, "/agent/restart",
        headers={"Authorization": f"Bearer {agent['token']}"},
        timeout=5,
    )
    if r is None:
        return _err_json("upstream agent request failed", 502, detail=err, tried=tried)
    return jsonify({"ok": r.ok, "status_code": r.status_code, "tried": tried,
                    "data": r.json() if r.ok else "upstream agent returned an error"})


@app.route("/api/agents/<agent_id>/config-file", methods=["GET", "PUT"])
def agents_config_file(agent_id: str):
    """Admin-gated proxy to the agent's /agent/config-file (GET reads
    the on-disk agent_config.yaml; PUT writes new text after backing
    up the existing file). Body shape for PUT:
        {"text": "<full YAML>", "expected_mtime": <float from GET>}
    The agent validates that the YAML parses and that MANAGER_URL is
    present before rewriting. Restart the agent afterwards to apply."""
    deny = _require_admin()
    if deny is not None:
        return deny
    data = agent_registry.load_agents()
    agent = data["agents"].get(agent_id)
    if not agent or not agent.get("bind_url") or not agent.get("token"):
        return jsonify({"ok": False, "error": "unknown agent or missing token"}), 404
    headers = {"Authorization": f"Bearer {agent['token']}"}
    if flask_request.method == "GET":
        r, tried, err = agent_registry.agent_request(
            "GET", agent, "/agent/config-file", headers=headers, timeout=10,
        )
    else:
        body = flask_request.get_json(force=True, silent=True) or {}
        r, tried, err = agent_registry.agent_request(
            "PUT", agent, "/agent/config-file", headers=headers,
            json=body, timeout=15,
        )
    if r is None:
        return _err_json("upstream agent request failed", 502, detail=err, tried=tried)
    try:
        payload = r.json()
    except Exception:
        log.warning("agent config-file PUT: non-JSON upstream response (status %s)", r.status_code)
        payload = {"ok": r.ok, "raw": "upstream returned a non-JSON body"}
    return jsonify(payload), r.status_code


@app.route("/api/agents/<agent_id>/log/tail", methods=["GET"])
def agents_log_tail(agent_id: str):
    """Admin-gated JSON proxy to the agent's /agent/log/tail."""
    deny = _require_admin()
    if deny is not None:
        return deny
    data = agent_registry.load_agents()
    agent = data["agents"].get(agent_id)
    if not agent or not agent.get("bind_url") or not agent.get("token"):
        return jsonify({"ok": False, "error": "unknown agent or missing token"}), 404
    r, tried, err = agent_registry.agent_request(
        "GET", agent, "/agent/log/tail",
        headers={"Authorization": f"Bearer {agent['token']}"},
        timeout=10,
    )
    if r is None:
        return _err_json("upstream agent request failed", 502, detail=err, tried=tried)
    return jsonify(r.json() if r.ok else {"ok": False, "error": "upstream agent error"}), r.status_code


@app.route("/api/agents/<agent_id>/log/stream", methods=["GET"])
def agents_log_stream(agent_id: str):
    """Admin-gated SSE proxy to the agent's /agent/log/stream. Streams
    bytes verbatim — no payload mutation."""
    deny = _require_admin()
    if deny is not None:
        return deny
    data = agent_registry.load_agents()
    agent = data["agents"].get(agent_id)
    if not agent or not agent.get("bind_url") or not agent.get("token"):
        return jsonify({"ok": False, "error": "unknown agent or missing token"}), 404

    urls = agent_registry.agent_callback_urls(agent)
    if not urls:
        return jsonify({"ok": False, "error": "no callback URL recorded"}), 502

    if not stream_pool.POOL.try_acquire():
        return jsonify({"ok": False,
                        "error": "manager at stream capacity; retry shortly"}), 503
    slot_handed = False
    try:
        last_err = None
        for base in urls:
            full = f"{base}/agent/log/stream"
            try:
                upstream = requests.get(
                    full,
                    headers={"Authorization": f"Bearer {agent['token']}"},
                    stream=True, timeout=(10, 60),  # agent keepalive 15s; reap a silent stream
                    **agent_registry.agent_tls_kwargs(full),
                )
                resp = app.response_class(
                    proxies.thread_pumped(upstream, "/agent/log/stream"),
                    mimetype=upstream.headers.get("Content-Type", "text/event-stream"),
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "X-Proxied-To": f"{agent['agent_id'][:8]}@{agent.get('hostname','?')}",
                    },
                    status=upstream.status_code,
                )
                resp.call_on_close(stream_pool.POOL.release)
                slot_handed = True
                return resp
            except Exception as e:
                last_err = f"{full}: {type(e).__name__}: {e}"
                continue
        return _err_json("all callback URLs failed", 502, detail=last_err)
    finally:
        if not slot_handed:
            stream_pool.POOL.release()


def _maybe_rewrite_sse_frame(frame: bytes, latest_v: "str | None") -> bytes:
    """Parse an SSE frame; if it carries a `done` JSON payload without a
    `version_to` field, inject the manager's latest_agent_version so the
    admin tab can render before-and-after even on older agents whose code
    didn't include that field. All other frames pass through verbatim."""
    if not latest_v:
        return frame
    if b'"stage": "done"' not in frame and b'"stage":"done"' not in frame:
        return frame
    out_lines = []
    for line in frame.split(b"\n"):
        if line.startswith(b"data:"):
            payload = line[5:].strip()
            try:
                d = json.loads(payload)
                if isinstance(d, dict) and d.get("stage") == "done" and not d.get("version_to"):
                    d["version_to"] = latest_v
                    line = b"data: " + json.dumps(d).encode()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        out_lines.append(line)
    return b"\n".join(out_lines)


@app.route("/api/agents/<agent_id>/self-update", methods=["POST"])
def agents_self_update(agent_id: str):
    """Admin-gated SSE proxy to the agent's /agent/self-update endpoint.
    The agent runs `install.sh --update --from-self-update` (git pull +
    redeploy code + venv refresh, no systemd unit refresh, no service
    restart) and streams its stdout/stderr back. After the install
    succeeds, the agent SIGTERMs itself; systemd Restart=always brings
    the new code up. Browser sees the SSE stream end naturally."""
    deny = _require_admin()
    if deny is not None:
        return deny
    data = agent_registry.load_agents()
    agent = data["agents"].get(agent_id)
    if not agent or not agent.get("bind_url") or not agent.get("token"):
        return jsonify({"ok": False, "error": "unknown agent or missing token"}), 404

    urls = agent_registry.agent_callback_urls(agent)
    if not urls:
        return jsonify({"ok": False, "error": "no callback URL recorded"}), 502

    if not stream_pool.POOL.try_acquire():
        return jsonify({"ok": False,
                        "error": "manager at stream capacity; retry shortly"}), 503
    slot_handed = False
    try:
        last_err = None
        for base in urls:
            full = f"{base}/agent/self-update"
            try:
                upstream = requests.post(
                    full,
                    headers={"Authorization": f"Bearer {agent['token']}"},
                    stream=True, timeout=(10, 60),  # agent emits a keepalive ≤10s during quiet pip phases
                    **agent_registry.agent_tls_kwargs(full),
                )
                # Inject the manager's latest_agent_version into the SSE
                # stream so the frontend sees version_to even on agents
                # whose code predates that field. Parses each frame, mutates
                # `done` payloads, also synthesizes a version_to line right
                # after the agent's version_before line so it shows mid-stream.
                latest_v = _latest_agent_version()
                def _gen():
                    buf = b""
                    injected_line = False
                    try:
                        for chunk in upstream.iter_content(chunk_size=None):
                            if not chunk:
                                continue
                            buf += chunk
                            while b"\n\n" in buf:
                                frame, buf = buf.split(b"\n\n", 1)
                                yield _maybe_rewrite_sse_frame(frame, latest_v) + b"\n\n"
                                if (not injected_line and latest_v
                                        and b"version_before" in frame):
                                    synth = json.dumps({"line": f"version_to:     {latest_v}"})
                                    yield f"data: {synth}\n\n".encode()
                                    injected_line = True
                        if buf:
                            yield _maybe_rewrite_sse_frame(buf, latest_v)
                    except requests.exceptions.RequestException:
                        pass  # upstream idle past read timeout / dropped — free the worker
                    finally:
                        upstream.close()
                log.info("self-update proxy → agent:%s host=%s",
                         agent["agent_id"][:8], agent.get("hostname"))
                resp = app.response_class(
                    _gen(),
                    mimetype=upstream.headers.get("Content-Type", "text/event-stream"),
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "X-Proxied-To": f"{agent['agent_id'][:8]}@{agent.get('hostname','?')}",
                    },
                    status=upstream.status_code,
                )
                resp.call_on_close(stream_pool.POOL.release)
                slot_handed = True
                return resp
            except Exception as e:
                last_err = f"{full}: {type(e).__name__}: {e}"
                continue
        return _err_json("all callback URLs failed", 502, detail=last_err)
    finally:
        if not slot_handed:
            stream_pool.POOL.release()




# OpenClaw analytics — the `/api/openclaw/analytics` route and every
# helper that feeds it (session-file parsing, per-agent aggregation,
# cross-agent merges, flow/task/delivery analytics, anomaly detection,
# trend computation) plus the 4 OPENCLAW_* path constants and 5 caches
# moved to openclaw.py (PR M5). Wired above (line ~2926) via
# `openclaw.register_routes(app, ctx)`, alongside the auth /
# agent_registry / terminal / proxies wire-ups.


# Alarm engine proxy (/api/alarm/*) + the /ws/alarm 426 stub moved to
# proxies.py (PR M4). The real WS bridge — _maybe_start_alarm_ws_proxy
# below — stays here; it's a standalone websockets-server thread, not a
# Flask route.


# Manager /health — answered without auth (path is in auth.AUTH_OPEN_PATHS). Used
# by external monitors and the dashboard's own probes; returning a tiny JSON
# body keeps load balancers / curl checks happy and stops the 404 log spam.
@app.route("/health", methods=["GET"])
def manager_health():
    return jsonify({
        "status": "ok",
        "version": __version__,
        "uptime_s": round(time.time() - _manager_startup_ts, 1),
    })


# Alarm-engine frontend serving (/alarm/<path>) + _ae_ws_url_for_browser +
# _inject_alarm_ws_url + _ALARM_FRONTEND_DIR + mimetypes init moved to
# proxies.py (PR M4). Main's index() route still needs the browser-dialable
# WS URL — it now reads it via proxies.ae_ws_url_for_browser().


# ─────────────────────────────────────────────────────────────────────
# Manager-side TLS server bring-up
# Defined here (before __main__) because the main block calls them.
# ─────────────────────────────────────────────────────────────────────
def _ensure_manager_server_cert() -> None:
    """Generate (or refresh) data/manager-tls.{crt,key} signed by the
    internal CA. SAN includes the manager's hostname + the IP a
    typical agent would dial. Re-issues automatically when within 30
    days of expiry."""
    crt_path = DATA_DIR / "manager-tls.crt"
    key_path = DATA_DIR / "manager-tls.key"

    need_new = True
    if crt_path.is_file() and key_path.is_file():
        try:
            from cryptography import x509 as _x509
            cur = _x509.load_pem_x509_certificate(crt_path.read_bytes())
            remaining = (cur.not_valid_after_utc - datetime.now(timezone.utc)).days
            need_new = remaining < 30
            # Force a reissue if SAN is missing localhost/127.0.0.1 —
            # earlier versions of this function omitted those, breaking
            # `curl https://localhost:5443` verification.
            try:
                ext = cur.extensions.get_extension_for_oid(
                    _x509.ObjectIdentifier("2.5.29.17")  # SAN
                )
                san = ext.value  # SubjectAlternativeName
                has_localhost   = any(getattr(n, "value", None) == "localhost"
                                      for n in san if isinstance(n, _x509.DNSName))
                has_loopback_ip = any(str(getattr(n, "value", "")) == "127.0.0.1"
                                      for n in san if isinstance(n, _x509.IPAddress))
                if not (has_localhost and has_loopback_ip):
                    log.info("  Manager TLS cert: missing localhost/127.0.0.1 SAN — reissuing")
                    need_new = True
            except Exception:
                # If SAN can't be parsed, safer to reissue than to keep.
                need_new = True
            # Also force-reissue if the cert lacks the AuthorityKeyIdentifier
            # extension — OpenSSL 3.x (Python 3.13+) rejects such certs
            # at chain verification time. See _pki.AKI_FIX_TS for context.
            try:
                cur.extensions.get_extension_for_oid(
                    _x509.ObjectIdentifier("2.5.29.35")  # authorityKeyIdentifier
                )
            except _x509.ExtensionNotFound:
                log.info("  Manager TLS cert: missing AuthorityKeyIdentifier — reissuing")
                need_new = True
            except Exception:
                need_new = True
            # Force-reissue if the cert was issued before our last PKI
            # bump (e.g., earlier today's cert may have a stale SAN IP
            # from gethostbyname(hostname) → 127.0.1.1). Cheaper than
            # decoding the full SAN.
            with best_effort("manager cert: check PKI-fix reissue"):
                _, _, _pki_for_aki = _pki_ensure_ca()
                if cur.not_valid_before_utc < _pki_for_aki.AKI_FIX_TS:
                    log.info("  Manager TLS cert: issued before PKI fix ts — reissuing")
                    need_new = True
            if not need_new:
                log.info("  Manager TLS cert: valid for %d more days", remaining)
        except Exception as e:
            log.warning("manager cert parse failed; reissuing: %s", e)
            need_new = True

    if not need_new:
        return

    ca_cert, ca_key, _pki = _pki_ensure_ca()
    import socket
    host = socket.gethostname()
    # gethostbyname often returns 127.0.1.1 on Debian/Ubuntu, which
    # doesn't help agents dialing from elsewhere on the LAN. Detect
    # the routable LAN IP by opening a UDP socket — connect() doesn't
    # send anything, just resolves the OS's routing decision so
    # getsockname() returns the source address the kernel would use.
    routable_ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            # Any address outside the loopback works as a probe; the
            # kernel picks the interface based on its routing table.
            s.connect(("1.1.1.1", 80))
            routable_ip = s.getsockname()[0]
    except OSError:
        pass
    # Collect every IP we know — loopback + routable + hostname-resolved
    # — into the SAN so curl from localhost, agents from the LAN, and
    # any operator-typed URL all validate.
    extra_ips = ["127.0.0.1"]
    if routable_ip and routable_ip not in extra_ips:
        extra_ips.append(routable_ip)
    try:
        h_ip = socket.gethostbyname(host)
        if h_ip and h_ip not in extra_ips:
            extra_ips.append(h_ip)
    except OSError:
        pass
    cert_pem, key_pem = _pki.sign_agent_cert(
        ca_cert, ca_key,
        agent_id="llm-systems-manager",
        hostname=host,
        ip_san=routable_ip,
        extra_dns_sans=["localhost"],
        extra_ip_sans=extra_ips,
    )
    log.info("  Manager TLS cert: SAN IPs = %s", ", ".join(extra_ips))
    # Atomic-rename so a partial write doesn't leave us serving a
    # half-baked PEM.
    for path, content, mode in (
        (key_path, key_pem, 0o600),
        (crt_path, cert_pem, 0o644),
    ):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content)
        os.chmod(tmp, mode)
        tmp.replace(path)
    log.info("  Manager TLS cert: issued (%s/%s)", crt_path.name, key_path.name)


def _ensure_ae_server_cert() -> None:
    """When [alarm_engine].tls_enabled, issue data/ae-tls.{crt,key} signed by
    the internal CA so the alarm engine can serve HTTPS. SAN covers localhost,
    127.0.0.1, the manager's routable IP, and the host parsed from
    alarm_engine_url — so it validates whether the AE is co-located or remote.
    On a split install the operator copies these two files to the AE host's
    data/ dir. No-op when TLS is disabled; re-issues within 30 days of expiry."""
    if not bool(settings.alarm_engine.tls_enabled):
        return
    # The AE reads its cert from its OWN data dir (llm-systems-alarm-engine/data).
    # Co-located: write straight there. Split (that dir doesn't exist on this
    # host): write to the manager's DATA_DIR as the copy-source for the admin.
    _ae_data = Path(__file__).resolve().parents[2] / "llm-systems-alarm-engine" / "data"
    target_dir = _ae_data if _ae_data.is_dir() else DATA_DIR
    crt_path = target_dir / "ae-tls.crt"
    key_path = target_dir / "ae-tls.key"
    # Parse alarm_engine_url early so the SAN audit below can confirm the
    # existing cert covers whatever host the manager (and the agents) will
    # dial. Without this, an operator who changes alarm_engine_url to a new
    # hostname keeps using the old cert (still > 30 days valid), the
    # manager's wss probe fails with "Hostname mismatch", and the WS proxy /
    # AE→manager outbound silently 502s. Mirrors the SAN audit in
    # _ensure_manager_server_cert.
    import socket
    from urllib.parse import urlparse
    ae_host = urlparse(_alarm_engine_url or "").hostname or ""

    need_new = True
    if crt_path.is_file() and key_path.is_file():
        try:
            from cryptography import x509 as _x509
            cur = _x509.load_pem_x509_certificate(crt_path.read_bytes())
            remaining = (cur.not_valid_after_utc - datetime.now(timezone.utc)).days
            need_new = remaining < 30
            if not need_new:
                # Audit the SAN against today's alarm_engine_url + the
                # baseline localhost/127.0.0.1 entries. Missing any of them
                # means the cert was issued before the current URL was wired
                # up; reissue rather than wait for natural expiry.
                try:
                    ext = cur.extensions.get_extension_for_oid(
                        _x509.ObjectIdentifier("2.5.29.17")  # SAN
                    )
                    san = ext.value
                    dns_names = {getattr(n, "value", None) for n in san
                                 if isinstance(n, _x509.DNSName)}
                    ip_names  = {str(getattr(n, "value", "")) for n in san
                                 if isinstance(n, _x509.IPAddress)}
                    missing: list[str] = []
                    if "localhost"   not in dns_names: missing.append("DNS:localhost")
                    if "127.0.0.1"   not in ip_names:  missing.append("IP:127.0.0.1")
                    if ae_host:
                        # ae_host could be either a DNS name or an IP literal.
                        # Classify by ipaddress.ip_address() — same logic the
                        # signer below uses to bucket it.
                        is_ip = False
                        try:
                            import ipaddress as _ip
                            _ip.ip_address(ae_host)
                            is_ip = True
                        except ValueError:
                            pass
                        if is_ip and ae_host not in ip_names:
                            missing.append(f"IP:{ae_host}")
                        elif not is_ip and ae_host not in dns_names:
                            missing.append(f"DNS:{ae_host}")
                    if missing:
                        log.info(
                            "  Alarm-engine TLS cert: SAN missing %s — reissuing "
                            "(operator must re-copy to AE host on split install)",
                            ", ".join(missing),
                        )
                        need_new = True
                    else:
                        log.info("  Alarm-engine TLS cert: valid for %d more days", remaining)
                except Exception:
                    log.info("  Alarm-engine TLS cert: SAN parse failed — reissuing")
                    need_new = True
        except Exception as e:
            log.warning("alarm-engine cert parse failed; reissuing: %s", e)
            need_new = True
    if not need_new:
        return
    routable_ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect(("1.1.1.1", 80))
            routable_ip = s.getsockname()[0]
    except OSError:
        pass
    dns_sans = ["localhost"]
    ip_sans = ["127.0.0.1"]
    if routable_ip and routable_ip not in ip_sans:
        ip_sans.append(routable_ip)
    # ae_host is whatever agents will dial for the AE (co-located = manager
    # IP, split = the remote AE host) — already parsed at the top of the
    # function for the SAN audit.
    if ae_host:
        try:
            import ipaddress as _ip
            _ip.ip_address(ae_host)
            if ae_host not in ip_sans:
                ip_sans.append(ae_host)
        except ValueError:
            if ae_host not in dns_sans:
                dns_sans.append(ae_host)

    ca_cert, ca_key, _pki = _pki_ensure_ca()
    cert_pem, key_pem = _pki.sign_agent_cert(
        ca_cert, ca_key,
        agent_id="llm-systems-alarm-engine",
        hostname=ae_host or socket.gethostname(),
        ip_san=routable_ip,
        extra_dns_sans=dns_sans,
        extra_ip_sans=ip_sans,
    )
    for path, content, mode in (
        (key_path, key_pem, 0o600),
        (crt_path, cert_pem, 0o644),
    ):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content)
        os.chmod(tmp, mode)
        tmp.replace(path)
    # Log the full write path so the operator can find the files (and so the
    # "co-located vs split" branch is self-evident). The split-install warning
    # uses the actual write target — wrote-to-AE-dir = co-located, wrote-to-
    # manager-dir = the AE host doesn't share this filesystem (the prior URL-
    # hostname-string check was wrong: a hostname like "myhost" doesn't match
    # "localhost"/IP even when the AE is right here).
    log.info("  Alarm-engine TLS cert: issued %s (SAN dns=%s ip=%s)",
             crt_path, dns_sans, ip_sans)
    if target_dir == DATA_DIR:
        log.warning("  Alarm engine appears remote (%s data/ dir missing on this host) — "
                    "copy %s and %s to that host's llm-systems-alarm-engine/data/ dir "
                    "for it to serve TLS", ae_host or "alarm engine", crt_path.name, key_path.name)


def _set_listen_keepalive(sock) -> None:
    """TCP keepalive + user-timeout on the listening socket; accepted client
    sockets inherit these on Linux. A vanished SSE client (laptop sleep, Wi-Fi
    drop — no clean FIN) is then dropped in ~60s instead of hours, so the held
    stream slot + upstream agent connection free instead of leaking to the cap."""
    import socket as _sock
    try:
        sock.setsockopt(_sock.SOL_SOCKET, _sock.SO_KEEPALIVE, 1)
        for _name, _val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            _opt = getattr(_sock, _name, None)
            if _opt is not None:
                sock.setsockopt(_sock.IPPROTO_TCP, _opt, _val)
        _uto = getattr(_sock, "TCP_USER_TIMEOUT", 18)  # Linux opt 18
        sock.setsockopt(_sock.IPPROTO_TCP, _uto, 60000)
        log.info("  TCP keepalive enabled on listener (idle 30s, user-timeout 60s)")
    except (OSError, AttributeError) as e:
        log.warning("TCP keepalive setup skipped: %s", e)


def _cheroot_serve_with_keepalive(srv) -> None:
    """Cheroot start() = prepare()+serve(); split it so keepalive sockopts land
    on the bound listener before the accept loop runs."""
    srv.prepare()
    try:
        _set_listen_keepalive(srv.socket)
    except Exception as e:
        log.warning("listener keepalive skipped: %s", e)
    _cheroot_servers.append(srv)
    srv.serve()


def _push_stream_metrics(points: "list[dict]") -> None:
    """POST manager_streams/agent_streams MetricPoints to the AE ingest so the
    numbers are alertable via the rule UI. Best-effort; never raises."""
    if not _alarm_engine_url or not points:
        return
    headers = {}
    tok = (getattr(settings.alarm_engine, "ingest_token", "") or "").strip()
    if tok and tok != "REPLACE_ME":
        headers["Authorization"] = f"Bearer {tok}"
    try:
        _ae_session.post(_alarm_engine_url.rstrip("/") + "/api/alarm/metrics/batch",
                         json={"metrics": points}, headers=headers, timeout=5)
    except Exception as e:
        log.debug("stream metric push failed: %s", e)


def _maybe_start_manager_tls_server() -> None:
    """When MANAGER_TLS_PORT is set, start a second werkzeug server on
    that port serving the same Flask app over HTTPS. The cert is
    auto-issued from the internal CA, persisted to
    data/manager-tls.{crt,key}, and rotated whenever it's within 30
    days of expiry."""
    # Source: env override (MANAGER_TLS_PORT) → [manager].tls_port in TOML.
    # tls_port=0 disables HTTPS.
    tls_port_env = os.environ.get("MANAGER_TLS_PORT", "").strip()
    if tls_port_env:
        try:
            tls_port = int(tls_port_env)
        except ValueError:
            log.warning("MANAGER_TLS_PORT=%r is not a valid port; skipping TLS", tls_port_env)
            return
    else:
        tls_port = int(settings.manager.tls_port or 0)
    if tls_port <= 0:
        log.info("  Manager TLS: disabled (set [manager].tls_port or MANAGER_TLS_PORT to enable)")
        return

    try:
        _ensure_manager_server_cert()
    except Exception as e:
        log.exception("manager server cert generation failed; skipping HTTPS: %s", e)
        return

    crt = DATA_DIR / "manager-tls.crt"
    key = DATA_DIR / "manager-tls.key"

    def _serve_tls() -> None:
        from cheroot.wsgi import Server as _CherootServer
        from cheroot.ssl.builtin import BuiltinSSLAdapter
        try:
            srv = _CherootServer(("0.0.0.0", tls_port), app,
                                 numthreads=int(getattr(settings.manager, "http_threads", 64) or 64))
            srv.ssl_adapter = BuiltinSSLAdapter(str(crt), str(key))
            log.info("  Manager TLS: listening on https://0.0.0.0:%d", tls_port)
            _cheroot_serve_with_keepalive(srv)
        except Exception as e:
            log.exception("manager TLS server crashed: %s", e)

    import threading as _threading
    t = _threading.Thread(target=_serve_tls, name="manager-tls-server", daemon=True)
    t.start()


def _maybe_start_alarm_ws_proxy() -> None:
    """Standalone websockets server (separate daemon thread, own asyncio loop)
    that bridges browser → alarm-engine WS. Needed because Cheroot WSGI can't
    speak WS, so we can't proxy on the main port. Browsers hit /ws/alarm on
    this port; the proxy opens upstream ws/wss to the AE (verifying its
    internal-CA-signed cert when AE TLS is on) and pipes frames bidirectionally.

    Disabled by default ([manager].ws_proxy_port = 0); enable it when AE TLS
    is on so browsers don't have to trust the internal CA directly. Serves wss
    when [manager].tls_port>0 (reuses manager-tls.{crt,key}), else ws."""
    # getattr() guards upgraded deploys whose local config/unified_config.py
    # predates these fields (file is gitignored; update.sh doesn't re-render).
    ws_port = int(getattr(settings.manager, "ws_proxy_port", 0) or 0)
    if ws_port <= 0:
        log.info("  WS proxy:    disabled (set [manager].ws_proxy_port to enable)")
        return
    if not _alarm_engine_url:
        log.warning("  WS proxy:    skipped (no alarm engine URL configured)")
        return
    import threading as _threading

    def _run() -> None:
        import asyncio
        import ssl
        import websockets
        from urllib.parse import urlsplit

        # Upstream AE URL → ws/wss
        ae_parts = urlsplit(_alarm_engine_url)
        ae_scheme = "wss" if ae_parts.scheme == "https" else "ws"
        ae_port = ae_parts.port or (443 if ae_scheme == "wss" else 80)
        ae_ws_url = f"{ae_scheme}://{ae_parts.hostname}:{ae_port}/ws"
        ae_ssl: "ssl.SSLContext | None" = None
        if ae_scheme == "wss":
            ae_ssl = ssl.create_default_context(cafile=_AE_CA_PATH)

        # Always serve plain ws on the proxy port. manager-tls.{crt,key} is
        # signed by the internal CA, so terminating wss here would just push
        # the trust problem back onto the browser — exactly what this proxy
        # exists to avoid. Operators wanting wss end-to-end should front this
        # port with a real-CA-cert reverse proxy (nginx/Caddy/Traefik).
        serve_ssl: "ssl.SSLContext | None" = None
        log.info("  WS proxy:    serving ws://0.0.0.0:%d → %s", ws_port, ae_ws_url)

        async def _pipe(src, dst) -> None:
            with best_effort("ws proxy: pipe frames", log=log):
                async for msg in src:
                    await dst.send(msg)

        async def _handler(client_ws) -> None:
            # websockets v13+ passes the path on client_ws.request.path
            req_path = getattr(getattr(client_ws, "request", None), "path", "/")
            if not req_path.startswith("/ws/alarm"):
                await client_ws.close(code=1008, reason="unknown path")
                return
            try:
                async with websockets.connect(ae_ws_url, ssl=ae_ssl, open_timeout=4) as up:
                    await asyncio.gather(
                        _pipe(client_ws, up),
                        _pipe(up, client_ws),
                        return_exceptions=True,
                    )
            except Exception as e:
                log.warning("WS proxy: upstream connect failed: %s", e)
                with best_effort("ws proxy: close client after upstream fail", log=log):
                    await client_ws.close(code=1011, reason="upstream unavailable")

        async def _serve() -> None:
            async with websockets.serve(_handler, "0.0.0.0", ws_port, ssl=serve_ssl):
                await asyncio.Future()  # run forever

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_serve())
        except Exception:
            log.exception("WS proxy: server crashed")

    _threading.Thread(target=_run, name="alarm-ws-proxy", daemon=True).start()


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m {s}s"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _log_shutdown_banner() -> None:
    """Final summary line for the operator — uptime, agent counts, SSE
    subscribers, and any currently-active alerts pulled from the AE.
    Best-effort: each lookup is guarded so a partial-init crash doesn't
    bury the real error under stat-collection tracebacks."""
    uptime = _fmt_uptime(time.time() - _startup_ts)
    agents: list = []
    approved_n = 0
    primary_llama_id = ""
    primary_lms_id = ""
    with best_effort("shutdown banner: agent registry stats"):
        agents = list(((agent_registry.load_agents().get("agents") or {})).values())
        approved_n = sum(1 for a in agents if a.get("status") == "approved")
        pl = agent_registry.primary_agent("llama")
        pm = agent_registry.primary_agent("lms")
        primary_llama_id = (pl or {}).get("agent_id", "")[:8]
        primary_lms_id   = (pm or {}).get("agent_id", "")[:8]

    try:
        sse_n = provider_state.STORE.total_subscriber_count()
    except Exception:
        sse_n = 0

    # Best-effort active-alerts probe. Short timeout — if the AE is
    # shutting down at the same time we don't want to block the manager's
    # own exit waiting on it. Tracks reachability separately so we can
    # distinguish "zero alerts" from "AE didn't answer."
    active_alerts: list = []
    alert_counts: dict = {}
    ae_reachable = False
    if _alarm_engine_url:
        with best_effort("shutdown banner: probe active alerts"):
            base = _alarm_engine_url.rstrip("/")
            r1 = _ae_session.get(
                f"{base}/api/alarm/alerts/?include_closed=false&limit=20",
                timeout=1.5,
            )
            r2 = _ae_session.get(f"{base}/api/alarm/alerts/counters", timeout=1.5)
            if r1.ok and r2.ok:
                ae_reachable = True
                payload = r1.json()
                active_alerts = payload if isinstance(payload, list) else (payload.get("alerts") or [])
                alert_counts = r2.json() or {}

    log.info("=" * 60)
    log.info(f"LLM Systems Manager {__version__} shutting down")
    log.info(f"  Uptime:         {uptime}")
    log.info(f"  Agents:         registered={len(agents)} approved={approved_n}"
             f" primary_llama={primary_llama_id or '—'} primary_lms={primary_lms_id or '—'}")
    log.info(f"  SSE subs:       llama-state={sse_n}")
    if not ae_reachable:
        log.info("  Alerts:         (alarm engine unreachable)")
    else:
        log.info(f"  Alerts:         total={alert_counts.get('total', 0)} "
                 f"by_status={dict(alert_counts.get('by_status', {}))} "
                 f"by_severity={dict(alert_counts.get('by_severity', {}))}")
        if active_alerts:
            log.info(f"  Active alerts ({len(active_alerts)}):")
            for a in active_alerts:
                scope = a.get("source_host") or "—"
                log.info(
                    f"    • [{a.get('severity', '?'):<8}] {a.get('rule_name') or a.get('rule_id') or '?'}"
                    f" — {scope}:{a.get('metric_source')}/{a.get('metric_name')}"
                    f" (value={a.get('current_value')}, threshold={a.get('threshold_value')},"
                    f" status={a.get('status')}, since={a.get('created_at')})"
                )
        else:
            log.info("  Active alerts:  none")
    log.info("=" * 60)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info(f"LLM Systems Manager {__version__} starting")
    log.info(f"  Config:       {CONFIG_PATH or '(none — using built-in defaults)'}")
    log.info(f"  InfluxDB:     http://{settings.influxdb.host}:{settings.influxdb.port} "
             f"(org={settings.influxdb.org})")
    log.info(f"  SQLite:       {DB_PATH} (model_benchmarks only)")
    log.info("  llama.cpp:    via primary llama agent")
    log.info("  LM Studio:    via primary lms agent")
    log.info(f"  Alarm Engine: {_alarm_engine_url}")
    log.info(f"  Poll interval: {settings.manager.fast_poll_interval}s (active) / "
             f"{settings.manager.poll_interval}s (idle)")
    log.info(f"  Listening on: http://{settings.manager.host}:{settings.manager.port}")
    # Heal split installs where the operator approved the only
    # llama/lms-capable agent before the auto-promote-on-approval
    # behaviour existed. If there's exactly one approved capability
    # holder and no primary set (and, for llama, no pool), promote it.
    # Multi-agent cases are left alone — operator picks via admin tab.
    try:
        with agent_registry.agents_lock:
            _data = agent_registry.load_agents()
            _glob = _data.setdefault("global", {})
            _agents_map = _data.get("agents") or {}
            _dirty = False
            for kind in ("llama", "lms"):
                if _glob.get(f"default_{kind}_id") or _glob.get(f"primary_{kind}_id"):
                    continue
                if kind == "llama" and _glob.get("llama_pool"):
                    continue
                _candidates = [a for a in _agents_map.values()
                               if a.get("status") == "approved"
                               and (a.get("capabilities") or {}).get(kind)]
                if len(_candidates) == 1:
                    # Lockstep default_+primary_ — same as agent_registry's
                    # approve auto-promote, so default_agent_id_for never lags.
                    agent_registry._set_provider_default(
                        _glob, kind, _candidates[0]["agent_id"])
                    _dirty = True
                    log.warning("auto-promoted agent:%s host=%s to primary %s "
                                "(single approved %s-capable agent, no prior primary)",
                                _candidates[0]["agent_id"][:8],
                                _candidates[0].get("hostname"), kind, kind)
            if _dirty:
                agent_registry.save_agents(_data)
    except Exception as _e:
        log.warning("primary backfill failed: %s", _e)

    for kind in ("llama", "lms"):
        try:
            agent = agent_registry.primary_agent(kind)
        except Exception:
            agent = None
        if agent:
            log.info(f"  Primary {kind}:  agent:{agent['agent_id'][:8]} "
                     f"host={agent.get('hostname')} bind={agent.get('bind_url')}")
        else:
            log.info(f"  Primary {kind}:  (none set — admin tab → Agents → Primary)")
    logging.getLogger("waitress").setLevel(logging.INFO)
    log.info("=" * 60)

    def _wait_for_ae_ready(base: str, deadline_s: float = 120.0) -> tuple[bool, int, float]:
        """Block until the alarm engine reports healthy on /health, or
        until deadline_s elapses. Returns (ready, attempts, elapsed_s)."""
        t_start = time.perf_counter()
        deadline = t_start + deadline_s
        attempt = 0
        while time.perf_counter() < deadline:
            attempt += 1
            with best_effort("wait-for-AE: health probe", log=log):
                r = _ae_session.get(f"{base}/health", timeout=3)
                if r.status_code == 200:
                    d = r.json()
                    comps = d.get("components") or {}
                    if (d.get("status") == "ok"
                            and comps.get("cache") == "active"
                            and "connected" in str(comps.get("influxdb", ""))):
                        return True, attempt, time.perf_counter() - t_start
            time.sleep(2.0)
        return False, attempt, time.perf_counter() - t_start

    def _history_ring_warmup() -> None:
        """Background warm-up of the history ring. Runs off the main
        thread so Flask binds to its port without waiting for this."""
        global _history_rows
        if not _alarm_engine_url:
            log.warning("No alarm-engine URL configured; history ring will not pre-fill")
            return
        _base = _alarm_engine_url.rstrip("/")
        ready, _hp_attempts, _hp_elapsed = _wait_for_ae_ready(_base, deadline_s=120.0)
        if not ready:
            log.critical(
                "History ring warm-up: alarm engine /health never reported "
                "ready after %.1fs (%d probes). The refresher will retry "
                "every %ds; first page load will show no history.",
                _hp_elapsed, _hp_attempts, int(HISTORY_REFRESH_INTERVAL_S),
            )
            return
        log.info(
            "Alarm engine ready in %.1fs (%d probe(s)); starting "
            "history ring fan-out fill",
            _hp_elapsed, _hp_attempts,
        )
        # 10 attempts × rising backoffs ≈ 5 minutes total. Generous
        # because the AE's rollup task or InfluxDB may need extra
        # time after /health flips green.
        _WARM_BACKOFFS = (3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 90.0)
        try:
            _t_fill = time.perf_counter()
            warm_rows: list = []
            final_attempt = 0
            for final_attempt, backoff in enumerate(_WARM_BACKOFFS, start=1):
                warm_rows = _build_history_rows(
                    HISTORY_WINDOW_MINUTES, HISTORY_FETCH_LIMIT,
                )
                if warm_rows:
                    break
                if final_attempt < len(_WARM_BACKOFFS):
                    log.warning(
                        "History ring fill: 0 rows from alarm engine "
                        "(attempt %d/%d, retrying in %.0fs)",
                        final_attempt, len(_WARM_BACKOFFS), backoff,
                    )
                    time.sleep(backoff)
            _fill_elapsed = time.perf_counter() - _t_fill
            if warm_rows:
                with _history_lock:
                    _history_rows = warm_rows
                log.info(
                    "History ring filled: %d rows in %.1fs "
                    "(%d attempt(s); target window=%d min)",
                    len(warm_rows), _fill_elapsed, final_attempt,
                    HISTORY_WINDOW_MINUTES,
                )
            else:
                # Distinguish "AE has zero metrics at all" (expected on a
                # fresh install before any agent has been approved) from
                # "AE has data but our query keys don't match" (a real
                # config drift). One probe of the cache decides.
                ae_has_any_data = False
                with best_effort("history warmup: probe AE metrics", log=log):
                    _probe = _ae_session.get(
                        f"{_base}/api/alarm/metrics", timeout=5,
                    )
                    if _probe.ok:
                        _j = _probe.json() or {}
                        ae_has_any_data = bool(_j.get("metrics") or _j)
                if not ae_has_any_data:
                    log.warning(
                        "History ring empty: alarm engine has no metrics yet "
                        "(no agents have pushed data). The refresher will "
                        "back-fill once the first sample arrives. Approve "
                        "an agent from the Admin tab to start collection."
                    )
                else:
                    log.critical(
                        "History ring CRITICAL: fill failed after %d "
                        "attempts and %.1fs even though alarm engine "
                        "reported ready AND has metric data. Check "
                        "_HISTORY_LEGACY_FIELD_MAP for stale (source, "
                        "metric_name) names (curl %s/api/alarm/metrics).",
                        len(_WARM_BACKOFFS), _fill_elapsed, _base,
                    )
        except Exception as _e:
            log.critical(
                "History ring CRITICAL: warm-up raised %r — "
                "refresher will retry but first paint will be blank",
                _e,
            )

    import threading as _threading_warmup
    _threading_warmup.Thread(
        target=_history_ring_warmup,
        name="history-ring-warmup",
        daemon=True,
    ).start()
    log.info("History ring warm-up dispatched to background thread; Flask binding now")

    # Start the 60-minute history ring-buffer refresher. Daemon thread so it
    # doesn't block shutdown.
    _threading.Thread(
        target=_history_refresher_loop,
        name="history-refresher",
        daemon=True,
    ).start()

    # Per-provider offline-edge sweep — fires the True→False latch transition
    # for any agent whose last_seen exceeds the provider's online_threshold_s.
    # Closes the pre-PR1 gap where LMS offline logs only fired if a browser
    # was polling /api/lmstudio/metrics.
    _threading.Thread(
        target=_offline_sweep_loop,
        name="provider-offline-sweep",
        daemon=True,
    ).start()

    # Live SSE-stream + connection health → Manager-tab card + alarm-engine
    # metrics (manager_streams/agent_streams). Reads the live _cheroot_servers
    # list, so worker counts populate as each server binds.
    stream_health.configure(_cheroot_servers, _push_stream_metrics, _HOSTNAME)
    _threading.Thread(
        target=stream_health.loop, name="stream-health", daemon=True,
    ).start()

    # optionally serve HTTPS on a second port using the
    # manager's own server cert (signed by the internal CA). Defaults
    # off; flip MANAGER_TLS_PORT to enable. Plain HTTP on 5000 stays
    # up either way
    _maybe_start_manager_tls_server()
    # Standalone WS proxy thread for /ws/alarm — no-op unless ws_proxy_port set.
    try:
        _maybe_start_alarm_ws_proxy()
    except Exception as _e:
        log.warning("WS proxy startup failed: %s", _e)
    # Standalone aiohttp SSE daemon for /api/llama-state/stream (#110) — no-op
    # unless [manager].stream_proxy_port is set and aiohttp is installed.
    try:
        sse_daemon.start(
            port=int(getattr(settings.manager, "stream_proxy_port", 5445) or 0),
            lifetime_s=float(getattr(settings.manager, "stream_proxy_lifetime_s", 600.0) or 600.0),
            keepalive_s=float(getattr(settings.manager, "stream_keepalive_s", 8.0) or 8.0),
            secret=_manager_secret(),
            snapshot_fn=_build_llama_state_payload,
            is_shutting_down=lambda: _shutting_down,
        )
    except Exception as _e:
        log.warning("SSE daemon startup failed: %s", _e)
    # Issue the alarm-engine TLS cert (no-op unless [alarm_engine].tls_enabled).
    # Co-located AE reads data/ae-tls.* directly; split installs copy them over.
    try:
        _ensure_ae_server_cert()
    except Exception as _e:
        log.warning("alarm-engine cert issuance failed: %s", _e)

    # Warn when alarm_engine_url is a non-self hostname (split install): agents
    # must resolve it — the co-located rewrite only helps when the AE is here.
    with best_effort("startup: split-install hostname warning", log=log):
        from urllib.parse import urlparse as _urlparse
        import ipaddress as _ipaddr
        _ae_h = (_urlparse(_alarm_engine_url or "").hostname or "").lower()
        if _ae_h and _ae_h not in _SELF_HOSTS:
            try:
                _ipaddr.ip_address(_ae_h)
            except ValueError:
                log.warning(
                    "alarm_engine_url host %r is a hostname on a split install — every "
                    "agent must resolve it (DNS or /etc/hosts). Prefer an IP in "
                    "[alarm_engine].alarm_engine_url so agents need no resolution "
                    "(the AE TLS cert SAN auto-covers the IP).", _ae_h)

    from cheroot.wsgi import Server as _CherootServer
    _http_srv = _CherootServer(
        (settings.manager.host, settings.manager.port), app,
        numthreads=int(getattr(settings.manager, "http_threads", 64) or 64),
    )
    log.info("  Manager HTTP: listening on http://%s:%d (%d worker threads)",
             settings.manager.host, settings.manager.port,
             int(getattr(settings.manager, "http_threads", 64) or 64))

    # systemd stop sends SIGTERM. Without a handler Python raises
    # SystemExit straight through cheroot's accept loop and the shutdown
    # banner never gets a chance to run. Catch the signal, print the
    # banner first (so it's always in the journal), drain SSE clients
    # so cheroot.stop() can join its worker threads, then arm a hard
    # backstop — cheroot's graceful stop waits indefinitely on any
    # still-open response stream (typically a browser EventSource with
    # no client-side close trigger), which used to time systemd out
    # at 90 s and escalate to SIGKILL.
    def _graceful_stop(signum, _frame):
        global _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        log.info("received signal %d, shutting down", signum)
        try:
            _log_shutdown_banner()
        except Exception as e:
            log.warning("shutdown banner failed: %s", e)
        # Flush the agent registry once so the final in-memory last_heartbeat
        # lands on disk — the per-beat write is throttled, so post-restart
        # liveness would otherwise read stale.
        try:
            agent_registry.save_agents(agent_registry.load_agents())
        except Exception as e:
            log.warning("registry flush on shutdown failed: %s", e)
        # Wake every SSE generator so its queue.get() returns and the
        # `while not _shutting_down` guard exits the loop.
        with best_effort("graceful stop: wake SSE subscribers", log=log):
            provider_state.STORE.wake_all(None)
        # Stop the SSE daemon (its subscribers were already woken by wake_all).
        with best_effort("graceful stop: stop sse daemon", log=log):
            sse_daemon.stop()
        # Hard backstop: if cheroot.stop() can't drain within 3 s
        # (typically because of long-lived SSE / log-tail streams that
        # don't honor _shutting_down), exit the process so systemd
        # doesn't have to escalate. The registry was flushed above; the
        # OS cleanup on exit covers the rest (no other on-disk state).
        def _force_exit():
            log.warning("forced exit after stop timeout")
            os._exit(0)
        threading.Timer(3.0, _force_exit).start()
        try:
            _http_srv.stop()
        except Exception as e:
            log.warning("HTTP server stop failed: %s", e)
    try:
        signal.signal(signal.SIGTERM, _graceful_stop)
    except (ValueError, AttributeError):
        pass

    try:
        _cheroot_serve_with_keepalive(_http_srv)
    except KeyboardInterrupt:
        # Ctrl+C also funnels through the same handler for a single
        # consistent shutdown path.
        _graceful_stop(signal.SIGINT, None)
