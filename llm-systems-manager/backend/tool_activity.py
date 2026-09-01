"""tool_activity: which agents are currently running a Tools-tab job.

Records a run when the manager proxies its start, then confirms or expires the
record from a background probe of the agent's /<provider>/tools/state endpoint.
Report Card jobs run in this process, so they are read straight from
report_card. Consumed by GET /api/tools/activity (#775) and the Autopilot
reconciler (#776).
"""
from __future__ import annotations

import threading
import time as _time

from _best_effort import best_effort

TOOLS = ("benchmark", "autotune")

# Probe cache lifetime, the window a just-started run is trusted unprobed, and
# how long a record survives while the agent cannot confirm it.
PROBE_TTL_S = 5.0
PROBE_TIMEOUT_S = 2.0
START_GRACE_S = 5.0
UNCONFIRMED_MAX_S = 60.0

# _PROBE state values: None = never answered, False = agent can't answer
# (predates the endpoint, or unreachable), dict = {tool: bool}.
_LOCK = threading.RLock()
_RUNS: "dict[tuple[str, str], dict]" = {}
_PROBE: "dict[tuple[str, str], dict]" = {}
_INFLIGHT: "set[tuple[str, str]]" = set()

_UNSET = object()

_agent_for = None
_agent_call = None
_reportcard_active = None


def configure(agent_for=_UNSET, agent_call=_UNSET, reportcard_active=_UNSET) -> None:
    """Wire the agent resolver, the agent HTTP caller, and the report-card
    active-job lister; keeps this module free of manager imports."""
    global _agent_for, _agent_call, _reportcard_active
    if agent_for is not _UNSET:
        _agent_for = agent_for
    if agent_call is not _UNSET:
        _agent_call = agent_call
    if reportcard_active is not _UNSET:
        _reportcard_active = reportcard_active


def reset() -> None:
    with _LOCK:
        _RUNS.clear()
        _PROBE.clear()
        _INFLIGHT.clear()


def note_start(agent_id: str, provider: str, tool: str, now: "float | None" = None) -> None:
    """Record that `tool` just started on `agent_id`."""
    if not agent_id or tool not in TOOLS:
        return
    with _LOCK:
        _RUNS[(agent_id, tool)] = {"provider": provider or "llama",
                                   "at": now if now is not None else _time.time()}


def note_end(agent_id: str, tool: str) -> None:
    """Drop the record for a run the manager knows has ended."""
    with _LOCK:
        _RUNS.pop((agent_id, tool), None)


def _fetch_state(agent_id: str, provider: str):
    """One /<provider>/tools/state call; False when the agent can't answer."""
    if not (_agent_for and _agent_call):
        return False
    with best_effort("tool-activity probe"):
        agent = _agent_for(agent_id)
        if agent:
            resp = _agent_call("GET", agent, f"/{provider}/tools/state",
                               timeout=PROBE_TIMEOUT_S)
            if resp is not None and resp.status_code == 200:
                body = resp.json() or {}
                return {"benchmark": bool(body.get("bench_active")),
                        "autotune": bool(body.get("autotune_active"))}
    return False


def _refresh(key: "tuple[str, str]", at: float) -> None:
    agent_id, provider = key
    try:
        state = _fetch_state(agent_id, provider)
        with _LOCK:
            _PROBE[key] = {"at": at, "state": state}
    finally:
        with _LOCK:
            _INFLIGHT.discard(key)


def _probe(agent_id: str, provider: str, now: float, sync: bool = False):
    """Last known state for one agent+provider, refreshed off the caller's
    thread so a slow agent can't stall a reconciler tick or a dashboard poll."""
    key = (agent_id, provider)
    with _LOCK:
        hit = _PROBE.get(key)
        stale = not hit or now - hit["at"] >= PROBE_TTL_S
        start = stale and key not in _INFLIGHT
        if start:
            _INFLIGHT.add(key)
    if start:
        if sync:
            _refresh(key, now)
        else:
            threading.Thread(target=_refresh, args=(key, now), daemon=True,
                             name=f"tool-activity-{agent_id[:8]}").start()
    with _LOCK:
        hit = _PROBE.get(key)
    return hit["state"] if hit else None


def _sweep(now: float, sync: bool = False) -> "dict[tuple[str, str], dict]":
    """Confirm or expire every recorded run; returns the survivors."""
    with _LOCK:
        pending = dict(_RUNS)
    for (agent_id, tool), rec in pending.items():
        age = now - rec["at"]
        if age < START_GRACE_S:
            continue
        state = _probe(agent_id, rec["provider"], now, sync=sync)
        if isinstance(state, dict):
            if not state.get(tool):
                note_end(agent_id, tool)
        elif age > UNCONFIRMED_MAX_S:
            # Never confirmed — an agent too old for the probe, or unreachable.
            # Drop it rather than pin a run indicator on every dashboard.
            note_end(agent_id, tool)
    with _LOCK:
        return dict(_RUNS)


def snapshot(now: "float | None" = None, sync: bool = False) -> dict:
    """{tool: bool} for the whole fleet plus the busy agents behind it."""
    now = now if now is not None else _time.time()
    runs = _sweep(now, sync=sync)
    tools = {t: False for t in TOOLS}
    agents: "dict[str, list]" = {}
    for (agent_id, tool) in runs:
        tools[tool] = True
        agents.setdefault(agent_id, []).append(tool)
    rc_agents = []
    if _reportcard_active:
        with best_effort("tool-activity report card"):
            rc_agents = list(_reportcard_active() or [])
    for agent_id in rc_agents:
        agents.setdefault(agent_id, []).append("reportcard")
    out = dict(tools)
    out["reportcard"] = bool(rc_agents)
    out["agents"] = {k: sorted(set(v)) for k, v in agents.items()}
    return out


def busy_agents(now: "float | None" = None) -> "set[str]":
    """Agent ids running any tool job right now."""
    return set(snapshot(now).get("agents") or {})
