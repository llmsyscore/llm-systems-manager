"""Update all agents (#1423): a manager-side job that updates agents one at a time; the Admin page only watches it."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

import jobs

KIND = "agents.update_all"
TITLE = "Update all agents"
PER_AGENT_S = 600.0
VERIFY_S = 120.0
VERIFY_POLL_S = 3.0
LOG_MAX = 5000
KEEP_LOGS = 3
NOT_ADMIN = "only an admin can update agents"
_VERSION_LINE = re.compile(r"^\s*version_(before|to):", re.I)


def plan(agents: Iterable[dict], latest: Optional[str], update_available: Callable[[Any, Any], bool]) -> dict:
    """Agents behind `latest`: {'todo': [...], 'skipped': [...]} as {agent_id, name, version}; disabled ones are skipped."""
    todo, skipped = [], []
    for a in agents:
        if not isinstance(a, dict) or not update_available(a.get("version"), latest):
            continue
        item = {"agent_id": a.get("agent_id") or "", "name": a.get("hostname") or str(a.get("agent_id") or "")[:8],
                "version": a.get("version") or "?"}
        (skipped if a.get("status") == "disabled" else todo).append(item)
    return {"todo": todo, "skipped": skipped}


def frame_line(msg: dict) -> "Optional[tuple[str, str]]":
    """One agent self-update payload as (text, level) for the output window; None for lines it hides."""
    if msg.get("line"):
        line = str(msg["line"])
        return None if _VERSION_LINE.match(line) else (line, "")
    if msg.get("blank"):
        return ("", "blank")
    stage = msg.get("stage")
    if stage == "done":
        head = "✓ done" if msg.get("ok") else "✗ done"
        rc = msg.get("rc")
        tail = f" — {msg['msg']}" if msg.get("msg") else ""
        return (f"{head} (rc={'?' if rc is None else rc}){tail}", "ok" if msg.get("ok") else "err")
    if stage:
        return (f"── {stage}: {msg.get('msg') or ''}", "stage")
    return None


class UpdateAll:
    """Registers the job kind and keeps each job's output in memory for the Admin page to read."""

    def __init__(self, service, *, load_agents: Callable[[], dict], latest_version: Callable[[], Optional[str]],
                 update_available: Callable[[Any, Any], bool], open_stream: Callable[[dict], Iterable[dict]],
                 record_update: Callable[[str, dict, bool], None],
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self._svc, self._load, self._latest, self._update_available = service, load_agents, latest_version, update_available
        self._open, self._record, self._now, self._sleep = open_stream, record_update, now, sleep
        self._logs: "dict[str, list]" = {}
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        service.register(jobs.Kind(KIND, TITLE, run=self._run, resume="fail", exclusive=lambda spec: [KIND],
                                   max_run_s=lambda spec: PER_AGENT_S * max(1, len(spec.get("todo") or [])) + 60.0,
                                   label=lambda spec: f"{TITLE} ({len(spec.get('todo') or [])})"))

    # ── output ──
    def _log(self, job_id: str, text: str, level: str = "") -> None:
        with self._lock:
            lines = self._logs.setdefault(job_id, [])
            if len(lines) < LOG_MAX:
                lines.append({"t": datetime.fromtimestamp(self._now()).strftime("%H:%M:%S"), "text": text, "level": level})

    def lines(self, job_id: str, after: int = 0) -> "tuple[list, int]":
        with self._lock:
            lines = self._logs.get(job_id) or []
            after = max(0, min(int(after), len(lines)))
            return list(lines[after:]), len(lines)

    def _start_log(self, job_id: str) -> None:
        with self._lock:
            self._logs.setdefault(job_id, [])
            for old in list(self._logs)[:-KEEP_LOGS]:
                self._logs.pop(old, None)

    # ── start / read ──
    def live(self) -> Optional[dict]:
        rows = self._svc.list("live", kind=KIND, limit=1)
        return rows[0] if rows else None

    def latest(self) -> Optional[dict]:
        rows = self._svc.list("all", kind=KIND, limit=1)
        return rows[0] if rows else None

    def start(self, *, user: str, role: str) -> "tuple[Optional[dict], Optional[str], dict]":
        """(row, error, plan); error when a run is already going or nothing needs an update."""
        with self._start_lock:
            latest = self._latest()
            p = plan((self._load().get("agents") or {}).values(), latest, self._update_available)
            if self.live() is not None:
                return None, "an Update all run is already going", p
            if not latest or not p["todo"]:
                return None, ("only disabled agents need an update" if p["skipped"] else "all agents are already up to date"), p
            row = self._svc.submit(KIND, {"latest": latest, **p}, user=user, role=role, source="ui")
            self._start_log(row["id"])
            return row, None, p

    # ── the job ──
    def _run(self, job) -> jobs.Outcome:
        if job.role != "admin":
            return jobs.fail(NOT_ADMIN, alert=False)
        spec = job.spec or {}
        latest, todo = str(spec.get("latest") or ""), list(spec.get("todo") or [])
        log = lambda text, level="": self._log(job.id, text, level)  # noqa: E731
        results = [{"name": a["name"], "state": "skipped"} for a in spec.get("skipped") or []]
        for r in results:
            log(f"– {r['name']}: disabled, skipped")
        for i, item in enumerate(todo):
            name = item["name"]
            if job.cancelled():
                results += [{"name": rest["name"], "state": "skipped"} for rest in todo[i:]]
                log("stopped by the operator; the rest are left as they are", "err")
                break
            self._svc.set_state(job.id, {"index": i + 1, "total": len(todo), "current": name, "results": results})
            if i:
                log("", "blank")
            log(f"── [{i + 1}/{len(todo)}] {name}: {item.get('version') or '?'} → {latest}", "stage")
            res = self._one(job, item, log)
            if res["ok"] is False and res["retryable"]:
                log(f"retrying {name} once — the download failed", "stage")
                res = self._one(job, item, log)
            ok_agent, bad_build = res["ok"] is True, False
            if ok_agent and not res["no_restart"]:
                log(f"waiting for {name} to come back on {latest}…")
                ok_agent = self._await_version(job, item["agent_id"], latest)
                bad_build = not ok_agent
                log(f"✓ {name} is back on {latest}" if ok_agent else f"✗ {name} did not report {latest} within 2 minutes",
                    "ok" if ok_agent else "err")
            elif ok_agent:
                log(f"✓ {name} already up to date; no restart needed", "ok")
            results.append({"name": name, "state": "updated" if ok_agent else "failed"})
            if bad_build:
                log(f"stopping: {name} did not come back on {latest}, so the rest are left as they are", "err")
                results += [{"name": rest["name"], "state": "skipped"} for rest in todo[i + 1:]]
                break
        summary = self._summary(results)
        log("", "blank")
        log(summary, "err" if any(r["state"] == "failed" for r in results) else "ok")
        return jobs.finish(result={"results": results}, message=summary)

    @staticmethod
    def _summary(results: list) -> str:
        pick = lambda s: [r["name"] for r in results if r["state"] == s]  # noqa: E731
        failed, skipped = pick("failed"), pick("skipped")
        return (f"── Update all finished: {len(pick('updated'))} updated"
                + (f", failed: {', '.join(failed)}" if failed else "")
                + (f", skipped: {', '.join(skipped)}" if skipped else ""))

    def _one(self, job, item: dict, log: Callable[..., None]) -> dict:
        """One self-update stream: {ok, no_restart, retryable}; ok None when the stream ended without a result."""
        out = {"ok": None, "no_restart": False, "retryable": False}
        agent = (self._load().get("agents") or {}).get(item["agent_id"])
        if not agent:
            log(f"✗ {item['name']} is no longer registered", "err")
            out["ok"] = False
            return out
        try:
            for msg in self._open(agent):
                if not isinstance(msg, dict):
                    continue
                shown = frame_line(msg)
                if shown is not None:
                    log(*shown)
                if msg.get("stage") == "done":
                    text = str(msg.get("msg") or "")
                    out["ok"] = msg.get("ok") is True
                    out["retryable"] = msg.get("retryable") is True or (
                        msg.get("retryable") is None and text == "tarball download failed")
                    out["no_restart"] = (msg.get("ok") is True and msg.get("restart_eta_s") is None
                                         and "no restart" in text.lower())
        except Exception as e:  # noqa: BLE001 — a dial failure fails this agent, not the run
            log(f"✗ request failed: {e}", "err")
            out["ok"] = False
        if out["ok"] is None:
            log("stream ended without a `done` frame", "err")
        try:
            self._record(job.user or "", item, out["ok"] is True)
        except Exception:  # noqa: BLE001 — the update stands even when the audit write fails
            pass
        return out

    def _await_version(self, job, agent_id: str, target: str) -> bool:
        t0 = self._now()
        while self._now() - t0 < VERIFY_S:
            self._sleep(VERIFY_POLL_S)
            a = (self._load().get("agents") or {}).get(agent_id) or {}
            if a.get("version") == target:
                return True
        return False
