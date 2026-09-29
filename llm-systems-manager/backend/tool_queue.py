"""tool_queue (#897): Tools runs that wait for a busy host, as `tool_run` jobs on the job service."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import jobs

log = logging.getLogger(__name__)

KIND = "tool_run"
TITLE = "Tool run"
TOOLS = ("benchmark", "autotune", "quality")
FLAG = {"benchmark": "bench_active", "autotune": "autotune_active", "quality": "quality_active"}
LABEL = {"benchmark": "Benchmark", "autotune": "Autotune", "quality": "Quality guard", "reportcard": "Report Card"}
POLL_S = 3.0
SLICE_S = 0.5
PROBE_TIMEOUT_S = 2.0
START_GRACE_S = 10.0
UNREACHABLE_MAX_S = 60.0
MAX_RUN_S = 3 * 3600.0
MAX_RUN_CEIL_S = 12 * 3600.0
PRE_TIMEOUT_S = 30
REFUSAL = re.compile(r"in progress|already running", re.IGNORECASE)


class QueueFull(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = str(message)


def max_run_s(spec: dict) -> float:
    """Run ceiling: 3 h, or the autotune budget across its models plus 30 min, capped at 12 h."""
    body = (spec or {}).get("body")
    body = body if isinstance(body, dict) else {}
    models = body.get("model_ids")
    try:
        minutes = float(body.get("budget_min") or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    budget = 60.0 * minutes * max(1, len(models) if isinstance(models, list) else 1)
    return min(MAX_RUN_CEIL_S, max(MAX_RUN_S, budget + 1800.0))


@dataclass
class Deps:
    agent_for: Callable[[str], Optional[dict]]
    agent_call: Callable[..., Any]
    held_agents: Callable[[], set]
    holder_tool: Callable[[str], Optional[str]]
    hostname: Callable[[str], str]
    note_start: Callable[[str, str, str], None]
    max_queued: Callable[[], int]
    sleep: Callable[[float], None] = field(default=time.sleep)
    now: Callable[[], float] = field(default=time.time)


def model_of(body: dict) -> str:
    """The model a tool start names, whichever key its body uses."""
    b = body if isinstance(body, dict) else {}
    ids = b.get("model_ids")
    first = ids[0] if isinstance(ids, list) and ids else None
    return str(b.get("model_id") or first or b.get("model") or "")[:200]


def _short(model_id: str) -> str:
    return (model_id or "").split("/")[-1].split(":")[0] or "model"


def _json(resp) -> dict:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is an empty answer
        return {}
    return data if isinstance(data, dict) else {}


class Queue:
    """Registers the kind and answers the start routes' held / submit / snapshot questions."""
    def __init__(self, service: jobs.Service, deps: Deps):
        self.s, self.d = service, deps
        service.register(jobs.Kind(
            KIND, TITLE, run=self.run, validate=self._validate, label=self._label,
            exclusive=lambda spec: [f"perf:{spec.get('agent_id', '')}"],
            on_cancel=self.on_cancel, resume="requeue", max_run_s=max_run_s))

    # ── kind hooks ──
    @staticmethod
    def _validate(spec: dict, who: dict):
        tool = str(spec.get("tool") or "")
        provider = str(spec.get("provider") or "")
        if tool not in TOOLS:
            return None, "unknown tool"
        if not spec.get("agent_id") or not provider or not spec.get("path"):
            return None, "agent, provider and path are required"
        if tool == "quality" and provider != "llama":
            return None, "the quality guard runs on llama hosts only"
        out = {k: spec.get(k) for k in ("provider", "agent_id", "tool", "path", "cancel", "model_id")}
        if spec.get("pre"):
            out["pre"] = str(spec["pre"])
        out["body"] = spec.get("body") if isinstance(spec.get("body"), dict) else {}
        out["model_id"] = str(out.get("model_id") or model_of(out["body"]))
        return out, None

    def _label(self, spec: dict) -> str:
        return f"{LABEL.get(spec.get('tool'), 'Tool')} · {_short(spec.get('model_id') or '')} · {self.d.hostname(spec.get('agent_id') or '') or 'host'}"

    def run(self, job: jobs.Job) -> jobs.Outcome:
        spec, state = job.spec, dict(job.state or {})
        aid, provider, tool = spec["agent_id"], spec["provider"], spec["tool"]
        agent = self.d.agent_for(aid)
        if not agent:
            return jobs.fail("unknown agent")
        if not state.get("run_id"):
            if aid in set(self.d.held_agents() or ()):
                return jobs.again(POLL_S, message="waiting for the host")
            if self._agent_busy(agent, provider):
                return jobs.again(POLL_S, state=state, message="waiting for the host")
            if spec.get("pre") and not state.get("pre_done"):
                try:
                    self.d.agent_call("POST", agent, spec["pre"], timeout=PRE_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001 — best effort
                    log.warning("tool_run %s: pre-step %s failed: %s: %s", job.id, spec["pre"], type(e).__name__, e)
                state["pre_done"] = True
                self.s.set_state(job.id, state)
                self._nap(job)
            resp = self.d.agent_call("POST", agent, spec["path"], json=spec["body"], timeout=20)
            if resp is None:
                return jobs.fail("agent unreachable")
            data = _json(resp)
            err = str(data.get("error") or data.get("detail") or "")
            if resp.status_code != 200 or data.get("ok") is False:
                if REFUSAL.search(err):
                    return jobs.again(POLL_S, state=state, message="waiting for the host")
                return jobs.fail(err or f"HTTP {resp.status_code}")
            state = {"run_id": str(data.get("run_id") or "started"), "started": self.d.now(), "seen": False}
            self.s.set_state(job.id, state)
            if job.cancelled():
                # A cancel that landed while the start was in flight: stop the run it just began.
                self._post_cancel(agent, spec)
                return jobs.fail("cancelled", alert=False)
        else:
            # Re-entry after a restart: the run may have ended meanwhile; give the flag a fresh grace.
            state["started"] = self.d.now()
        self.d.note_start(aid, provider, tool)
        flag, seen = FLAG[tool], bool(state.get("seen"))
        started = float(state["started"])
        last_ok = self.d.now()
        while not job.cancelled():
            resp = self.d.agent_call("GET", agent, f"/{provider}/tools/state", timeout=PROBE_TIMEOUT_S)
            now = self.d.now()
            if resp is not None and resp.status_code == 200:
                last_ok = now
                active = bool(_json(resp).get(flag))
                if active and not seen:
                    seen = True
                    state["seen"] = True
                    self.s.set_state(job.id, state)
                elif not active and seen:
                    return jobs.finish({"run_id": state["run_id"]})
                elif not active and now - started > START_GRACE_S:
                    return jobs.fail("run did not start", alert=False)
            elif now - last_ok > UNREACHABLE_MAX_S:
                return jobs.fail("agent unreachable")
            self._nap(job)
        return jobs.fail("cancelled", alert=False)

    def _agent_busy(self, agent: dict, provider: str) -> bool:
        """True when the agent's tools/state reports any tool running; unreadable counts as not busy."""
        try:
            resp = self.d.agent_call("GET", agent, f"/{provider}/tools/state", timeout=PROBE_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return False
        if resp is None or resp.status_code != 200:
            return False
        data = _json(resp)
        return any(bool(data.get(f)) for f in FLAG.values())

    def _nap(self, job: jobs.Job) -> None:
        waited = 0.0
        while waited < POLL_S and not job.cancelled():
            self.d.sleep(SLICE_S)
            waited += SLICE_S

    def on_cancel(self, job: jobs.Job) -> None:
        state = getattr(job, "state", None) or {}
        spec = getattr(job, "spec", None) or {}
        if not state.get("run_id") or not spec.get("cancel"):
            return
        agent = self.d.agent_for(spec.get("agent_id") or "")
        if agent:
            self._post_cancel(agent, spec)

    def _post_cancel(self, agent: dict, spec: dict) -> None:
        if not spec.get("cancel"):
            return
        try:
            self.d.agent_call("POST", agent, spec["cancel"], timeout=10)
        except Exception:  # noqa: BLE001 — best effort
            pass

    # ── start-or-queue questions ──
    def rows_for(self, agent_id: str) -> "list[dict]":
        """Live tool_run rows for one host: running first, then queued by creation."""
        rows = [r for r in self.s.list("live", kind=KIND, limit=jobs.LIST_MAX) if r["spec"].get("agent_id") == agent_id]
        rows.sort(key=lambda r: (0 if r["status"] == "running" else 1, r["created"], r["id"]))
        return rows

    def _batch_holds(self, agent_id: str) -> bool:
        key = f"perf:{agent_id}"
        return any(key in (r.get("exclusive") or []) for r in self.s.list("running", kind="autotune_batch", limit=jobs.LIST_MAX))

    def held(self, agent_id: str) -> bool:
        return agent_id in set(self.d.held_agents() or ()) or bool(self.rows_for(agent_id)) or self._batch_holds(agent_id)

    def can_wait(self, agent: dict, provider: str) -> bool:
        """False for an agent whose tools/state cannot be read, so it is never queued against."""
        try:
            resp = self.d.agent_call("GET", agent, f"/{provider}/tools/state", timeout=PROBE_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return False
        return bool(resp is not None and resp.status_code == 200)

    def wait_for(self, agent_id: str) -> str:
        host = self.d.hostname(agent_id) or ""
        tool = self.d.holder_tool(agent_id)
        if not tool:
            running = [r for r in self.rows_for(agent_id) if r["status"] == "running"]
            tool = running[0]["spec"].get("tool") if running else ("batch" if self._batch_holds(agent_id) else None)
        if not tool:
            return "the run in progress"
        name = "Autotune batch" if tool == "batch" else LABEL.get(tool, tool)
        return f"{name} on {host}" if host else name

    def submit(self, *, provider: str, agent: dict, tool: str, path: str, cancel: str, body: dict,
               user: str, role: str, pre: Optional[str] = None) -> dict:
        aid = agent["agent_id"]
        queued = [r for r in self.rows_for(aid) if r["status"] == "queued"]
        cap = int(self.d.max_queued() or 5)
        if len(queued) >= cap:
            raise QueueFull(f"{cap} run{'s' if cap != 1 else ''} already queued on {self.d.hostname(aid) or 'this host'}")
        spec = {"provider": provider, "agent_id": aid, "tool": tool, "path": path, "cancel": cancel,
                "body": body, "model_id": model_of(body)}
        if pre:
            spec["pre"] = pre
        row = self.s.submit(KIND, spec, user=user or "", role=role or "", source="ui")
        return {"job_id": row["id"], "position": len(queued) + 1, "wait_for": self.wait_for(aid)}

    def snapshot(self) -> dict:
        """{agent_id: [{job_id, tool, provider, model_id, user, status, created, path}, …]} in FIFO order."""
        out: "dict[str, list]" = {}
        rows = self.s.list("live", kind=KIND, limit=jobs.LIST_MAX)
        rows.sort(key=lambda r: (0 if r["status"] == "running" else 1, r["created"], r["id"]))
        for r in rows:
            out.setdefault(r["spec"].get("agent_id") or "", []).append(
                {"job_id": r["id"], "tool": r["spec"].get("tool"), "provider": r["spec"].get("provider") or "",
                 "model_id": r["spec"].get("model_id") or "",
                 "user": r.get("user") or "", "status": r["status"], "created": r["created"],
                 "path": r["spec"].get("path") or ""})
        return out
