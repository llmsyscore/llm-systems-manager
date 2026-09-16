"""Tower timers (#1029): a read repeated on an interval, its samples kept, one report turn in the thread when done."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

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
TOOL_NAME = "schedule"
TICK_TOOL = "timer"
METRICS = tower_tools.TIMER_METRICS
METRIC_UNIT = {"cpu_pct": "%", "ram_pct": "%", "gpu_pct": "%", "gpu_temp_c": "C", "watts": "W"}
LIVE = ("queued", "running", "reporting")
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


class Timers:
    """Schedules, ticks and reports timers; `runs` is attached after the run registry exists."""
    def __init__(self, store, *, registry_factory: Callable[[], dict], cfg: Callable[[], Any],
                 audit: Optional[Callable[[dict], None]] = None, now: Callable[[], float] = time.time):
        self._store = store
        self._registry_factory, self._cfg, self._audit, self._now = registry_factory, cfg, audit, now
        self.runs = None
        self._lock = threading.Lock()

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
        """The schedule tool's result: the queued timer, or a message saying why not."""
        spec, err = timer_spec(args, self._registry_factory(), self._cfg(), role)
        if err:
            return {"ok": False, "message": err}
        now = self._now()
        with self._lock:
            live = self._store.live_timers()
            if sum(1 for t in live if t["user"] == user) >= MAX_PER_USER:
                return {"ok": False, "message": f"you already have {MAX_PER_USER} timers running; cancel one first"}
            if len(live) >= MAX_TOTAL:
                return {"ok": False, "message": f"{MAX_TOTAL} timers are already running on this manager; try again later"}
            tid = self._store.create_timer(thread_id, user, role, spec, now)
        row = self._store.get_timer(tid)
        self._log_audit("tower.timer.schedule", row, True)
        log.debug("tower timer %s scheduled user=%s every=%s times=%s", tid, user, spec["every_s"], spec["times"])
        out = {"ok": True, "timer_id": tid, "label": spec["label"], "every_s": spec["every_s"], "times": spec["times"],
               "first_tick": local(row["next_tick"]), "ends": local(row["ends"]),
               "message": f"scheduled: {spec['label']}, every {spec['every_s']} s, {spec['times']} tick{'s' if spec['times'] != 1 else ''}; "
                          "the report arrives in this conversation when it is done"}
        if spec.get("note"):
            out["note"] = spec["note"]
        return out

    def list(self, user: str) -> "list[dict]":
        """The user's live timers plus the ones resolved in the last RECENT_S, newest first."""
        now = self._now()
        return [timer_view(r, now) for r in self._store.user_timers(user, now - RECENT_S)]

    def live_count(self, user: str) -> int:
        return sum(1 for t in self._store.live_timers() if t["user"] == user)

    def cancel(self, tid: str, user: str) -> "tuple[Optional[dict], Optional[tuple[int, str]]]":
        row = self._store.get_timer(tid)
        if not row or row["user"] != user:
            return None, (404, "unknown timer")
        if row["status"] not in LIVE:
            return None, (409, "not live")
        now = self._now()
        if not self._store.update_timer(tid, status="cancelled", resolved=now, message=_CANCELLED, next_tick=None, only_live=True):
            return None, (409, "not live")
        row = self._store.get_timer(tid)
        self._note_thread(row, "cancelled")
        return row, None

    def _note_thread(self, row: dict, status: str) -> None:
        """Leaves the samples in the thread as a timer tool row when no report turn will carry them."""
        try:
            result = samples_result(row, status)
            result["message"] = row.get("message") or status
            self._store.add_message(row["thread_id"], "tool", json.dumps(result, default=str), tool_name=TICK_TOOL,
                                    tool_args=json.dumps({"label": row["label"], "timer_id": row["id"]}), tool_ok=False, tool_ms=0)
        except Exception as e:  # noqa: BLE001 — the timer row still records the outcome
            log.warning("tower timer note failed: %s: %s", type(e).__name__, e)

    def _finish(self, row: dict, status: str, message: str) -> None:
        """Ends a live timer; a timer the operator cancelled meanwhile is left as it is."""
        now = self._now()
        if not self._store.update_timer(row["id"], status=status, resolved=now, message=message, next_tick=None, only_live=True):
            return
        row = self._store.get_timer(row["id"]) or {**row, "status": status, "message": message}
        if status == "failed":
            self._note_thread(row, status)
        self._log_audit(f"tower.timer.{status}", row, status == "done", {"message": message})
        log.debug("tower timer %s %s: %s", row["id"], status, message)

    def tick(self) -> None:
        """Samples every due timer once and tries to start the report turn of every finished one."""
        now = self._now()
        retry = self._store.timers_by_status("reporting")
        for row in self._store.due_timers(now):
            try:
                self._sample(row, now)
            except Exception as e:  # noqa: BLE001 — one timer's failure never stops the others
                log.warning("tower timer %s tick failed: %s: %s", row["id"], type(e).__name__, e)
                self._finish(row, "failed", "tick failed")
        for row in retry:
            try:
                self._try_report(row, now)
            except Exception as e:  # noqa: BLE001
                log.warning("tower timer %s report failed: %s: %s", row["id"], type(e).__name__, e)
                self._finish(row, "failed", "report failed")

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

    def _sample(self, row: dict, now: float) -> None:
        cfg = self._cfg()
        if not bool(getattr(cfg, "enabled", False)):
            self._finish(row, "failed", "Tower was turned off")
            return
        if not self._store.thread_user(row["thread_id"]):
            self._finish(row, "failed", "the conversation was deleted")
            return
        registry = self._registry_factory()
        allowed = {t.name for t in tower_tools.catalog(registry, cfg, row["role"])}
        spec = row["spec"]
        name = "host_detail" if spec["kind"] == "metric" else spec["tool"]
        if TOOL_NAME not in allowed or name not in allowed:
            self._finish(row, "failed", f"{name} is no longer available")
            return
        samples = list(row["samples"])
        if self.runs is not None and not self.runs.count_tick(row["user"]):
            samples.append({"t": now, "error": "rate limited"})
        else:
            value, err = self._read(registry[name], spec)
            samples.append({"t": now, "value": value} if err is None else {"t": now, "error": err})
        if len(samples) >= row["times"]:
            self._store.update_timer(row["id"], samples=samples, status="reporting", next_tick=None)
            self._try_report({**row, "samples": samples, "status": "reporting"}, now)
            return
        nxt = float(row["next_tick"]) + row["every_s"]
        if nxt <= now:
            nxt = now + row["every_s"]
        self._store.update_timer(row["id"], samples=samples, status="running", next_tick=nxt)

    def _try_report(self, row: dict, now: float) -> None:
        """Starts the report turn; a busy user or missing model is retried each tick until REPORT_RETRY_S past the end."""
        if self.runs is None:
            return
        if not self._store.thread_user(row["thread_id"]):
            self._finish(row, "failed", "the conversation was deleted")
            return
        live = self._store.get_timer(row["id"])
        if not live or live["status"] != "reporting":
            return
        text, prelude = report_request(row)
        rid, err = self.runs.start(user=row["user"], role=row["role"], thread_id=row["thread_id"], text=text, page={},
                                   actor=f"timer via {row['user']}", prelude=prelude)
        if err is None:
            if not self._store.update_timer(row["id"], status="done", run_id=rid, resolved=now, message=None, only_live=True):
                self.runs.stop(rid, row["user"])
                log.debug("tower timer %s cancelled while its report started; run %s stopped", row["id"], rid)
                return
            self._log_audit("tower.timer.complete", row, True, {"run_id": rid, "samples": len(row["samples"])})
            log.debug("tower timer %s reported run=%s", row["id"], rid)
            return
        if now - float(row["ends"] or now) > REPORT_RETRY_S:
            self._finish(row, "failed", f"could not report: {err[1]}")


def start_thread(timers: Timers, shutting_down: Callable[[], bool]):
    """Daemon loop ticking the timers every POLL_S; None under pytest."""
    if "pytest" in sys.modules:
        return None

    def _loop():
        while not shutting_down():
            try:
                timers.tick()
            except Exception as e:  # noqa: BLE001 — the loop outlives any one tick
                log.warning("tower timers tick failed: %s: %s", type(e).__name__, e)
            time.sleep(POLL_S)

    t = threading.Thread(target=_loop, name="tower-timers", daemon=True)
    t.start()
    return t
