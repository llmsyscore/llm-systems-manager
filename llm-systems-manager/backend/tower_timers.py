"""Tower timers (#1029): a read repeated on an interval, its samples kept, one report turn in the thread when done."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

import jobs
import tower_tools

log = logging.getLogger("tower")

MIN_EVERY_S = tower_tools.TIMER_MIN_EVERY_S
MAX_SPAN_S = tower_tools.TIMER_MAX_SPAN_S
MAX_SAMPLES = tower_tools.TIMER_MAX_SAMPLES
MAX_PER_USER = 3
MAX_TOTAL = 8
SAMPLE_CHARS = 600
REPORT_CHARS = 8000
REPORT_RETRY_S = 600.0
RECENT_S = 120.0
POLL_S = 2.0
EARLY_FAIL_TICKS = 3
TOOL_NAME = "schedule"
TICK_TOOL = "timer"
METRICS = tower_tools.TIMER_METRICS
METRIC_UNIT = {"cpu_pct": "%", "ram_pct": "%", "gpu_pct": "%", "gpu_temp_c": "C", "watts": "W"}
KIND = "tower_timer"
_NOT_POLLABLE = frozenset({TOOL_NAME, "wait_until", "help", "support"})
_CANCELLED = "cancelled by the operator"
REPORT_PREFIX = "⏱ Timer finished: "


def _hms(ts: float) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")


def local(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else None


def pick_path(obj: Any, path: str) -> Any:
    """The value at a dotted path (list indexes as numbers); None when any step is missing."""
    cur = obj
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fleet_names(registry: dict) -> str:
    """Comma-separated hostnames from hosts_overview, or "" when it is unavailable."""
    return ", ".join(tower_tools.fleet_hosts(registry))


def _host_known(allowed: dict, registry: dict, host: str) -> Optional[str]:
    """None when host_detail resolves `host`; else an error naming the fleet (or skips when host_detail is unavailable)."""
    tool = allowed.get("host_detail") if isinstance(allowed, dict) else None
    if tool is None:
        return None
    result, ok = tower_tools.run_tool(tool, {"host": host, "section": "summary"})
    if ok and isinstance(result, dict) and not result.get("error"):
        return None
    names = _fleet_names(registry)
    return f"unknown host: {host}; hosts are {names}" if names else f"unknown host: {host}"


def timer_spec(args: dict, registry: dict, cfg: Any, role: str) -> "tuple[Optional[dict], Optional[str]]":
    """Validates schedule args into a spec: label, every_s, times and either host+metric or tool+args(+pick)."""
    a = args if isinstance(args, dict) else {}
    note: "list[str]" = []
    every = int(a["every_s"]) if a.get("every_s") is not None else 60
    if every < MIN_EVERY_S:
        note.append(f"interval raised to {MIN_EVERY_S} s")
        every = MIN_EVERY_S
    every = min(every, MAX_SPAN_S)
    if a.get("for_s"):
        times = max(1, int(a["for_s"]) // every)
    else:
        times = max(1, int(a.get("times") or 1))
    cap = max(1, min(MAX_SAMPLES, MAX_SPAN_S // every))
    if times > cap:
        note.append(f"ticks capped at {cap}")
        times = cap
    label = " ".join(str(a.get("label") or "").split())[:60]
    metric, host = str(a.get("metric") or "").strip(), str(a.get("host") or "").strip()
    spec: dict = {"every_s": every, "times": times}
    allowed = {t.name: t for t in tower_tools.catalog(registry, cfg, role)}
    if metric:
        if metric not in METRICS:
            return None, f"metric must be one of {', '.join(METRICS)}"
        if not host or "," in host or host.lower() == "all":
            return None, "metric needs one host"
        if "host_detail" not in allowed:
            return None, "host_detail cannot be polled"
        err = _host_known(allowed, registry, host)
        if err:
            return None, err
        spec.update({"kind": "metric", "host": host, "metric": metric})
        label = label or f"{metric} on {host}"
    else:
        name = str(a.get("tool") or "").strip()
        if not name:
            return None, "give host and metric, or tool and args"
        tool = allowed.get(name)
        if tool is None or tool.kind != "read" or name in _NOT_POLLABLE:
            return None, f"{name} cannot be polled"
        targs, err = tower_tools.validate_args(tool, a.get("args") if isinstance(a.get("args"), dict) else {})
        if err:
            return None, f"{name}: {err}"
        thost = targs.get("host")
        if isinstance(thost, str) and thost and "," not in thost and thost.lower() != "all":
            err = _host_known(allowed, registry, thost)
            if err:
                return None, err
        pick = str(a.get("pick") or "").strip()[:80]
        spec.update({"kind": "tool", "tool": name, "args": targs, "pick": pick})
        tgt = targs.get("host") or targs.get("model") or targs.get("path") or ""
        label = label or (name.replace("_", " ") + (f" on {tgt}" if tgt else ""))
    spec["label"] = label
    if note:
        spec["note"] = "; ".join(note)
    return spec, None


def timer_view(row: dict, now: Optional[float] = None) -> dict:
    """The drawer's view of one timer row."""
    now = now or time.time()
    return {"id": row["id"], "thread_id": row["thread_id"], "label": row["label"], "status": row["status"],
            "every_s": row["every_s"], "times": row["times"], "count": len(row["samples"]),
            "next_in_s": max(0, int(row["next_tick"] - now)) if row.get("next_tick") and row["status"] in ("queued", "running") else None,
            "left_s": max(0, int(row["ends"] - now)) if row.get("ends") and row["status"] in ("queued", "running") else None,
            "ends": row.get("ends"), "run_id": row.get("run_id"), "message": row.get("message"),
            "created": row.get("created"), "resolved": row.get("resolved")}


def samples_result(row: dict, status: str = "done") -> dict:
    """The timer tool row's content: the samples with times, numeric stats and a series for the graph, capped."""
    spec, samples = row["spec"], list(row["samples"])
    vals = [s["value"] for s in samples if _num(s.get("value"))]
    out: dict = {"ok": status == "done", "label": row["label"], "status": status, "every_s": row["every_s"],
                 "ticks": len(samples), "planned": row["times"], "errors": sum(1 for s in samples if "error" in s)}
    if spec["kind"] == "metric":
        out.update({"host": spec["host"], "metric": spec["metric"], "unit": METRIC_UNIT.get(spec["metric"], "")})
    else:
        out.update({"tool": spec["tool"], "args": spec["args"], **({"pick": spec["pick"]} if spec.get("pick") else {})})
    if samples:
        out["started"], out["ended"] = local(samples[0]["t"]), local(samples[-1]["t"])
    if vals:
        out.update({"min": round(min(vals), 2), "avg": round(sum(vals) / len(vals), 2), "max": round(max(vals), 2),
                    "series": [[round(float(s["t"])), s["value"]] for s in samples if _num(s.get("value"))]})
    rows = [{"time": _hms(s["t"]), **({"value": s["value"]} if "value" in s else {"error": s.get("error")})} for s in samples]
    out["samples"] = rows
    while len(json.dumps(out, default=str)) > REPORT_CHARS and len(rows) > 1:
        rows = rows[1:]
        out["samples"] = rows
        out["shown"] = f"last {len(rows)} of {len(samples)}"
    return out


def report_request(row: dict) -> "tuple[str, dict]":
    """(user text, prelude) for the report turn: the short line the thread shows and the samples the model gets."""
    spec = row["spec"]
    result = samples_result(row)
    n, every = result["ticks"], row["every_s"]
    span = n * every
    when = f"{n} tick{'s' if n != 1 else ''} every {every} s" + (f" over {span // 60} min" if span >= 120 else "")
    ask = ("Report each tick with its time and value, then min, average, max and the trend; draw a text bar chart "
           "of the values." if spec["kind"] == "metric" else
           "Summarise what the samples show and what changed between them.")
    text = f"{REPORT_PREFIX}{row['label']} · {when}. {ask}"
    prelude = {"name": TICK_TOOL, "args": {"label": row["label"], "timer_id": row["id"]}, "result": result,
               "summary": f"timer · {row['label']} · {n} tick{'s' if n != 1 else ''}"}
    return text, prelude


def timer_row(job: dict) -> dict:
    """A job row in the legacy timer shape the views, reports and routes read."""
    spec, state = job.get("spec") or {}, job.get("state") or {}
    res = job.get("result") or {}
    status = job["status"]
    if status == "queued" and state.get("phase") == "reporting":
        status = "reporting"
    elif status in ("queued", "running") and state.get("samples"):
        status = "running"
    elif status == "running":
        status = "queued"
    return {"id": job["id"], "thread_id": job.get("thread_id"), "user": job.get("user"), "role": job.get("role"),
            "label": job.get("label"), "spec": spec, "every_s": int(spec.get("every_s") or 0), "times": int(spec.get("times") or 1),
            "samples": list(state.get("samples") or []), "status": status, "created": job.get("created"),
            "next_tick": job.get("next_run"), "ends": state.get("ends"), "resolved": job.get("resolved"),
            "message": job.get("message") if status in ("failed", "cancelled") else None,
            "run_id": (res.get("run_id") if isinstance(res, dict) else None) or state.get("run_id")}


class Timers:
    """Schedules timers as `tower_timer` jobs and runs their ticks; `runs` is attached after the run registry exists."""
    def __init__(self, service, *, store, registry_factory: Callable[[], dict], cfg: Callable[[], Any],
                 audit: Optional[Callable[[dict], None]] = None, now: Callable[[], float] = time.time):
        self._svc, self._store = service, store
        self._registry_factory, self._cfg, self._audit, self._now = registry_factory, cfg, audit, now
        self.runs = None
        self._lock = threading.Lock()
        service.register(jobs.Kind(KIND, "Tower timer", run=self._run, on_finish=self._finished,
                                   resume="requeue", max_run_s=120.0, label=lambda spec: spec.get("label") or "timer"))

    def _log_audit(self, action: str, row: dict, ok: bool, detail: Optional[dict] = None) -> None:
        if self._audit is None:
            return
        try:
            self._audit({"action": action, "actor": f"timer via {row['user']}", "target": row["label"], "ok": ok,
                         "detail": {"timer_id": row["id"], "thread_id": row["thread_id"], "spec": row["spec"],
                                    "every_s": row["every_s"], "times": row["times"], **(detail or {})}})
        except Exception as e:  # noqa: BLE001 — an audit failure never breaks a timer
            log.warning("tower timer audit failed: %s: %s", type(e).__name__, e)

    def schedule(self, *, thread_id: str, user: str, role: str, args: dict) -> dict:
        """The schedule tool's result: the queued timer job, or a message saying why not."""
        spec, err = timer_spec(args, self._registry_factory(), self._cfg(), role)
        if err:
            return {"ok": False, "message": err}
        now = self._now()
        with self._lock:
            live = self._svc.list("live", kind=KIND, limit=jobs.LIST_MAX)
            if sum(1 for t in live if t["user"] == user) >= MAX_PER_USER:
                return {"ok": False, "message": f"you already have {MAX_PER_USER} timers running; cancel one first"}
            if len(live) >= MAX_TOTAL:
                return {"ok": False, "message": f"{MAX_TOTAL} timers are already running on this manager; try again later"}
            job = self._svc.submit(KIND, spec, user=user, role=role, source="tower", label=spec["label"],
                                   not_before=now + spec["every_s"], thread_id=thread_id)
        ends = now + spec["every_s"] * spec["times"]
        self._svc.set_state(job["id"], {"samples": [], "phase": "sampling", "ends": ends})
        self._store.touch_thread(thread_id, now)
        self._log_audit("tower.timer.schedule", timer_row(self._svc.get(job["id"]) or job), True)
        log.debug("tower timer %s scheduled user=%s every=%s times=%s", job["id"], user, spec["every_s"], spec["times"])
        plural = "s" if spec["times"] != 1 else ""
        out = {"ok": True, "timer_id": job["id"], "label": spec["label"], "every_s": spec["every_s"], "times": spec["times"],
               "first_tick": local(now + spec["every_s"]), "ends": local(ends),
               "message": f"scheduled: {spec['label']}, every {spec['every_s']} s, {spec['times']} tick{plural}; "
                          "the report arrives in this conversation when it is done"}
        if spec.get("note"):
            out["note"] = spec["note"]
        return out

    def list(self, user: str) -> "list[dict]":
        """The user's live timers plus the ones resolved in the last RECENT_S, newest first."""
        now = self._now()
        rows = self._svc.list("live", kind=KIND, user=user, since=now - RECENT_S, limit=20)
        return [timer_view(timer_row(r), now) for r in rows]

    def live_count(self, user: str) -> int:
        return len(self._svc.list("live", kind=KIND, user=user, limit=jobs.LIST_MAX))

    def cancel(self, tid: str, user: str) -> "tuple[Optional[dict], Optional[tuple[int, str]]]":
        job = self._svc.get(tid)
        if not job or job["kind"] != KIND or job["user"] != user:
            return None, (404, "unknown timer")
        if job["status"] not in jobs.LIVE:
            return None, (409, "not live")
        out = self._svc.cancel(tid, actor=user, message=_CANCELLED)
        if out is None:
            return None, (409, "not live")
        return timer_row(out), None

    def cancel_thread(self, thread_id: str) -> int:
        """Cancels every live timer of a deleted conversation."""
        return self._svc.cancel_where(KIND, thread_id=thread_id, message="the conversation was deleted", actor="tower")

    def _run(self, job) -> jobs.Outcome:
        """One tick: a sample while sampling, the report turn once every tick is in."""
        cfg = self._cfg()
        row = timer_row(job.row())
        state = dict(job.state or {})
        samples = list(state.get("samples") or [])
        if not bool(getattr(cfg, "enabled", False)):
            return jobs.fail("Tower was turned off", alert=False)
        if not self._store.thread_user(job.thread_id):
            return jobs.fail("the conversation was deleted", alert=False)
        if state.get("phase") == "reporting":
            return self._report(job, row, state)
        registry = self._registry_factory()
        allowed = {t.name for t in tower_tools.catalog(registry, cfg, job.role)}
        spec = job.spec
        name = "host_detail" if spec["kind"] == "metric" else spec["tool"]
        if TOOL_NAME not in allowed or name not in allowed:
            return jobs.fail(f"{name} is no longer available")
        now = self._now()
        if self.runs is not None and not self.runs.count_tick(job.user):
            samples.append({"t": now, "error": "rate limited"})
        else:
            value, err = self._read(registry[name], spec)
            samples.append({"t": now, "value": value} if err is None else {"t": now, "error": err})
        state["samples"] = samples
        if len(samples) >= EARLY_FAIL_TICKS and all("error" in s for s in samples):
            self._svc.set_state(job.id, state)
            return jobs.fail(f"every tick failed: {samples[-1]['error']}", alert=False)
        if len(samples) < row["times"]:
            return jobs.again(row["every_s"], state=state, message=f"{len(samples)} of {row['times']} ticks")
        state["phase"] = "reporting"
        return jobs.again(POLL_S, state=state, message="reporting")

    def _report(self, job, row: dict, state: dict) -> jobs.Outcome:
        """Starts the report turn; a busy user or missing model is retried until REPORT_RETRY_S past the end."""
        now = self._now()
        if self.runs is None:
            return jobs.again(POLL_S, state=state)
        text, prelude = report_request(row)
        rid, err = self.runs.start(user=job.user, role=job.role, thread_id=job.thread_id, text=text, page={},
                                   actor=f"timer via {job.user}", prelude=prelude)
        if err is None:
            # The LIVE-guarded write is the race check: a timer cancelled meanwhile stops its own report run.
            if not self._svc.set_state(job.id, {**state, "run_id": rid}) or job.cancelled():
                self.runs.stop(rid, job.user)
                return jobs.fail("cancelled while the report started")
            self._log_audit("tower.timer.complete", row, True, {"run_id": rid, "samples": len(row["samples"])})
            return jobs.finish(result={"run_id": rid, "samples": len(row["samples"])}, message="reported", state=state)
        if now - float(state.get("ends") or now) > REPORT_RETRY_S:
            return jobs.fail(f"could not report: {err[1]}")
        return jobs.again(POLL_S, state=state, message=f"waiting to report: {err[1]}")

    def _finished(self, job) -> None:
        """Failed and cancelled timers leave their samples in a live thread; only a failure adds an audit row."""
        row = timer_row(job.row())
        if job.status in ("failed", "cancelled") and self._store.thread_user(job.thread_id):
            self._note_thread(row, job.status)
        if job.status == "failed":
            self._log_audit("tower.timer.failed", row, False, {"message": job.message})
        rid = (job.state or {}).get("run_id")
        if job.status == "cancelled" and rid and self.runs is not None:
            self.runs.stop(rid, job.user)

    def _note_thread(self, row: dict, status: str) -> None:
        """Leaves the samples in the thread as a timer tool row when no report turn will carry them."""
        try:
            result = samples_result(row, status)
            result["message"] = row.get("message") or status
            self._store.add_message(row["thread_id"], "tool", json.dumps(result, default=str), tool_name=TICK_TOOL,
                                    tool_args=json.dumps({"label": row["label"], "timer_id": row["id"]}), tool_ok=False, tool_ms=0)
        except Exception as e:  # noqa: BLE001 — the timer row still records the outcome
            log.warning("tower timer note failed: %s: %s", type(e).__name__, e)

    def _read(self, tool, spec: dict) -> "tuple[Any, Optional[str]]":
        if spec["kind"] == "metric":
            result, ok = tower_tools.run_tool(tool, {"host": spec["host"], "section": "summary"})
            if not ok or not isinstance(result, dict) or result.get("error"):
                return None, (result.get("error") if isinstance(result, dict) else None) or "read failed"
            v = result.get(spec["metric"])
            if not _num(v):
                return None, f"{spec['metric']} not reported"
            return round(float(v), 2), None
        result, ok = tower_tools.run_tool(tool, dict(spec["args"]))
        if not ok:
            return None, (result.get("error") if isinstance(result, dict) else None) or "read failed"
        if spec.get("pick"):
            v = pick_path(result, spec["pick"])
            if v is None:
                return None, f"{spec['pick']} not in the result"
            return v if isinstance(v, (int, float, str, bool)) else tower_tools.cap_result(v, SAMPLE_CHARS), None
        return tower_tools.cap_result(result, SAMPLE_CHARS), None
