"""Live SSE-stream + connection health for the Manager tab.

Aggregates the manager stream pool, Cheroot worker threads + queue backlog, TCP
connection counts, and each agent's /status streams into one snapshot for the
Manager-tab card, and pushes the numbers as `manager_streams` / `agent_streams`
metric sources to the alarm engine so they're thresholdable in the rule UI.
"""
from __future__ import annotations

import logging
import threading
import time

import stream_pool  # type: ignore[import-not-found]  # sibling
import agent_registry  # type: ignore[import-not-found]  # sibling
import sse_daemon  # type: ignore[import-not-found]  # sibling; lazy-aiohttp, safe to import
from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling
from config.unified_config import settings  # type: ignore[import-not-found]

log = logging.getLogger("llm-systems-manager.stream_health")

_servers: list = []        # cheroot servers, appended as they bind
_ae_post = None            # callable(list[dict]) -> None
_hostname = ""
_cache: dict = {"ts": 0.0, "data": {}}
_lock = threading.Lock()


def configure(servers: list, ae_post, hostname: str) -> None:
    """servers is a live list the caller appends to as Cheroot binds, so later
    appends are visible here without re-configuring."""
    global _servers, _ae_post, _hostname
    _servers = servers
    _ae_post = ae_post
    _hostname = hostname or ""


def _manager_local() -> dict:
    sp = stream_pool.POOL.stats()
    total = idle = backlog = 0
    for srv in list(_servers):
        with best_effort("stream_health: read cheroot server stats", log=log):
            total += int(getattr(srv, "numthreads", 0) or 0)
            tp = getattr(srv, "requests", None)
            if tp is not None:
                idle += int(getattr(tp, "idle", 0) or 0)
                backlog += int(tp.qsize())
    browser = agent_conn = 0
    try:
        import psutil
        ports = {int(getattr(settings.manager, "port", 5000) or 5000),
                 int(getattr(settings.manager, "tls_port", 0) or 0)}
        ports.discard(0)
        for c in psutil.net_connections("tcp"):
            if c.status != psutil.CONN_ESTABLISHED:
                continue
            if c.laddr and getattr(c.laddr, "port", None) in ports:
                browser += 1
            if c.raddr and getattr(c.raddr, "port", None) == 8082:
                agent_conn += 1
    except Exception as e:
        log.debug("connection count failed: %s", e)
    return {
        "pool": sp,
        "worker_threads": total,
        "worker_threads_busy": max(0, total - idle),
        "worker_threads_idle": idle,
        "worker_backlog": backlog,
        "browser_connections": browser,
        "agent_connections": agent_conn,
        "sse_daemon_streams": sse_daemon.active_count(),
        "sse_daemon_running": sse_daemon.is_running(),
    }


def _agents_stream_state() -> list:
    out = []
    try:
        data = agent_registry.load_agents()
    except Exception:
        return out
    for aid, agent in (data.get("agents") or {}).items():
        if agent.get("status") != "approved" or not agent.get("token"):
            continue
        host = agent.get("hostname") or aid[:8]
        r, _tried, _err = agent_registry.agent_request(
            "GET", agent, "/status",
            headers={"Authorization": f"Bearer {agent['token']}"}, timeout=4)
        if r is None:
            out.append({"hostname": host, "agent_id": aid, "reachable": False})
            continue
        try:
            s = (r.json() or {}).get("streams") or {}
        except Exception:
            s = {}
        out.append({"hostname": host, "agent_id": aid, "reachable": True,
                    "active": s.get("active"), "peak": s.get("peak"),
                    "refusals": s.get("refusals"), "cap": s.get("cap"),
                    "worker_threads_busy": s.get("worker_threads_busy"),
                    "terminal_sessions": s.get("terminal_sessions")})
    return out


def collect() -> dict:
    snap = _manager_local()
    snap["agents"] = _agents_stream_state()
    snap["hostname"] = _hostname
    snap["ts"] = time.time()
    return snap


def snapshot() -> dict:
    with _lock:
        if _cache["data"]:
            return dict(_cache["data"])
    return collect()


def _to_points(snap: dict) -> list:
    pts: list = []

    def add(source, name, val, host, unit=""):
        if val is None:
            return
        try:
            v = float(val)
        except (TypeError, ValueError):
            return
        pts.append({"source": source, "metric_name": name, "value": v,
                    "unit": unit, "hostname": host})

    pool = snap.get("pool", {})
    h = snap.get("hostname") or _hostname
    add("manager_streams", "stream_active", pool.get("active"), h)
    add("manager_streams", "stream_peak", pool.get("peak"), h)
    add("manager_streams", "stream_refusals", pool.get("refusals"), h)
    add("manager_streams", "stream_limit", pool.get("limit"), h)
    add("manager_streams", "worker_threads_busy", snap.get("worker_threads_busy"), h)
    add("manager_streams", "worker_backlog", snap.get("worker_backlog"), h)
    add("manager_streams", "browser_connections", snap.get("browser_connections"), h)
    add("manager_streams", "agent_connections", snap.get("agent_connections"), h)
    add("manager_streams", "sse_daemon_streams", snap.get("sse_daemon_streams"), h)
    add("manager_streams", "sse_daemon_running", snap.get("sse_daemon_running"), h)
    for a in snap.get("agents", []):
        if not a.get("reachable"):
            continue
        ah = a.get("hostname")
        add("agent_streams", "stream_active", a.get("active"), ah)
        add("agent_streams", "stream_peak", a.get("peak"), ah)
        add("agent_streams", "stream_refusals", a.get("refusals"), ah)
        add("agent_streams", "terminal_sessions", a.get("terminal_sessions"), ah)
    return pts


def loop(interval: float = 15.0) -> None:
    while True:
        try:
            snap = collect()
            with _lock:
                _cache["data"] = snap
                _cache["ts"] = snap["ts"]
            if _ae_post is not None:
                pts = _to_points(snap)
                if pts:
                    _ae_post(pts)
        except Exception as e:
            log.warning("stream-health loop error: %s", e)
        time.sleep(interval)
