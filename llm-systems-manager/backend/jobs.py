"""Job service (#915): one ledger and one dispatcher for scheduled and queued manager work."""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional, Union

log = logging.getLogger("llm-systems-manager.jobs")

STATUSES = ("queued", "running", "done", "failed", "cancelled")
LIVE = ("queued", "running")
SOURCES = ("tower", "ui", "api", "system")
POLL_S = 1.0
LEASE_S = 30.0
SWEEP_EVERY_S = 86400.0
FAILED_WINDOW_S = 86400.0
LIST_MAX = 100
CANCELLED = "cancelled by the operator"
RESTARTED = "manager restarted"
TIMED_OUT = "timed out"


class JobError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = str(message)
        self.status = status


@dataclass
class Outcome:
    verb: str
    state: Optional[dict] = None
    result: Any = None
    message: Optional[str] = None
    next_in: Optional[float] = None
    retry_in: Optional[float] = None
    alert: bool = True


def ok(state: Optional[dict] = None, result: Any = None, message: str = "done") -> Outcome:
    return Outcome("ok", state=state, result=result, message=message)


def again(next_in: float, state: Optional[dict] = None, message: Optional[str] = None) -> Outcome:
    return Outcome("again", state=state, message=message, next_in=float(next_in))


def finish(result: Any = None, message: str = "done", state: Optional[dict] = None) -> Outcome:
    return Outcome("finish", state=state, result=result, message=message)


def fail(message: str, retry_in: Optional[float] = None, alert: bool = True) -> Outcome:
    """A failed run; `alert=False` records the failure without paging (operator-initiated stops)."""
    return Outcome("fail", message=str(message), retry_in=None if retry_in is None else float(retry_in), alert=alert)


@dataclass
class Kind:
    name: str
    title: str
    run: Callable[["Job"], Outcome]
    validate: Optional[Callable[[dict, dict], "tuple[Optional[dict], Optional[str]]"]] = None
    label: Optional[Callable[[dict], str]] = None
    exclusive: Optional[Callable[[dict], list]] = None
    on_finish: Optional[Callable[["Job"], None]] = None
    on_cancel: Optional[Callable[["Job"], None]] = None
    resume: str = "requeue"
    max_run_s: Union[float, Callable[[dict], float]] = 3600.0
    api: bool = False

    def limit_s(self, spec: dict) -> float:
        v = self.max_run_s(spec) if callable(self.max_run_s) else self.max_run_s
        return float(v or 0) or 3600.0


class Job:
    """Read-only view of a row for kind hooks: every column as an attribute, plus cancelled()."""
    def __init__(self, row: dict, cancelled: Callable[[], bool] = lambda: False):
        self._row, self._cancelled = dict(row), cancelled

    def __getattr__(self, key: str) -> Any:
        try:
            return self._row[key]
        except KeyError:
            raise AttributeError(key) from None

    def cancelled(self) -> bool:
        return bool(self._cancelled())

    def row(self) -> dict:
        return dict(self._row)


def local(ts: Any) -> Optional[str]:
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, (int, float)) and ts else None


def init_table(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT, user TEXT, role TEXT, source TEXT,
            spec TEXT, state TEXT, status TEXT NOT NULL, not_before REAL, period_s REAL, runs_left INTEGER,
            next_run REAL, last_run REAL, started REAL, run_count INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, lease REAL, exclusive TEXT, result TEXT, message TEXT,
            thread_id TEXT, created REAL NOT NULL, resolved REAL);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, next_run);
    """)
    conn.commit()


class Store:
    KEYS = ("id", "kind", "label", "user", "role", "source", "spec", "state", "status", "not_before", "period_s",
            "runs_left", "next_run", "last_run", "started", "run_count", "attempts", "lease", "exclusive", "result",
            "message", "thread_id", "created", "resolved")
    JSON = ("spec", "state", "exclusive", "result")

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]):
        self._conn = conn_factory
        self._lock = threading.RLock()

    @property
    def lock(self) -> Any:
        """The store's RLock, so a caller can read and guard-update a row in one critical section."""
        return self._lock

    def _row(self, r) -> dict:
        row = dict(zip(self.KEYS, r))
        row["spec"] = json.loads(row["spec"] or "{}")
        row["state"] = json.loads(row["state"] or "{}")
        row["exclusive"] = json.loads(row["exclusive"] or "[]")
        row["result"] = json.loads(row["result"]) if row["result"] else None
        return row

    def _enc(self, k: str, v: Any) -> Any:
        if k not in self.JSON:
            return v
        if v is None and k == "result":
            return None
        return json.dumps(v if v is not None else ([] if k == "exclusive" else {}), default=str)

    def insert(self, row: dict) -> None:
        with self._lock:
            c = self._conn()
            c.execute(f"INSERT OR REPLACE INTO jobs ({', '.join(self.KEYS)}) VALUES ({', '.join('?' * len(self.KEYS))})",
                      tuple(self._enc(k, row.get(k)) for k in self.KEYS))
            c.commit()

    def rows(self, where: str = "1=1", params: tuple = (), order: str = "created DESC", limit: Optional[int] = None) -> "list[dict]":
        sql = f"SELECT {', '.join(self.KEYS)} FROM jobs WHERE {where} ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            return [self._row(r) for r in self._conn().execute(sql, params).fetchall()]

    def get(self, job_id: str) -> Optional[dict]:
        rows = self.rows("id=?", (job_id,))
        return rows[0] if rows else None

    def update(self, job_id: str, *, only: Optional[tuple] = None, **fields) -> bool:
        """Sets the given columns; with `only`, only while the status is one of those."""
        sets = {k: self._enc(k, v) for k, v in fields.items() if k in self.KEYS and k != "id"}
        if not sets:
            return False
        guard, params = "", []
        if only:
            guard = f" AND status IN ({', '.join('?' * len(only))})"
            params = list(only)
        with self._lock:
            c = self._conn()
            n = c.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?{guard}",
                          (*sets.values(), job_id, *params)).rowcount
            c.commit()
        return bool(n)

    def due(self, now: float) -> "list[dict]":
        return self.rows("status='queued' AND next_run IS NOT NULL AND next_run <= ?", (now,), order="next_run, created")

    def running(self) -> "list[dict]":
        return self.rows("status='running'", (), order="started")

    def summary(self, now: float) -> dict:
        with self._lock:
            c = self._conn()
            q = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
            r = c.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
            f = c.execute("SELECT COUNT(*) FROM jobs WHERE status='failed' AND resolved >= ?", (now - FAILED_WINDOW_S,)).fetchone()[0]
            nxt = c.execute("SELECT MIN(next_run) FROM jobs WHERE status='queued'").fetchone()[0]
        return {"queued": int(q), "running": int(r), "failed_24h": int(f), "next_due": nxt}

    def sweep(self, cutoff: float) -> int:
        with self._lock:
            c = self._conn()
            n = c.execute("DELETE FROM jobs WHERE status IN ('done','failed','cancelled') AND resolved < ?", (cutoff,)).rowcount
            c.commit()
        return int(n)


class Service:
    """Registers kinds, keeps the ledger, and ticks the dispatcher; `inline` runs handlers in the calling thread."""
    def __init__(self, store: Store, *, cfg: Callable[[], Any], alert: Optional[Callable[[dict], bool]] = None,
                 audit: Optional[Callable[[dict], None]] = None, now: Callable[[], float] = time.time, inline: bool = False):
        self._store, self._cfg, self._alert, self._audit, self._now, self._inline = store, cfg, alert, audit, now, inline
        self._kinds: "dict[str, Kind]" = {}
        self._workers: "dict[str, dict]" = {}
        self._cancel: "dict[str, threading.Event]" = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    # ── kinds ──
    def register(self, kind: Kind) -> Kind:
        self._kinds[kind.name] = kind
        return kind

    def kind(self, name: str) -> Optional[Kind]:
        return self._kinds.get(name)

    def kinds(self) -> "list[Kind]":
        return list(self._kinds.values())

    # ── ledger ──
    def submit(self, kind: str, spec: dict, *, user: str = "", role: str = "", source: str = "system",
               label: Optional[str] = None, not_before: Optional[float] = None, period_s: Optional[float] = None,
               runs_left: Optional[int] = None, thread_id: Optional[str] = None) -> dict:
        k = self._kinds.get(kind)
        if k is None:
            raise JobError("unknown kind", 404)
        spec = spec if isinstance(spec, dict) else {}
        if k.validate is not None:
            spec, err = k.validate(spec, {"user": user, "role": role})
            if err:
                raise JobError(err)
        now = self._now()
        nb = float(not_before) if not_before is not None else now
        row = {"id": uuid.uuid4().hex[:16], "kind": kind, "label": (label or (k.label(spec) if k.label else k.title))[:80],
               "user": user or "", "role": role or "", "source": source if source in SOURCES else "system",
               "spec": spec, "state": {}, "status": "queued", "not_before": nb,
               "period_s": float(period_s) if period_s else None, "runs_left": int(runs_left) if runs_left is not None else None,
               "next_run": nb, "last_run": None, "started": None, "run_count": 0, "attempts": 0, "lease": None,
               "exclusive": list(k.exclusive(spec) or []) if k.exclusive else [], "result": None, "message": "queued",
               "thread_id": thread_id, "created": now, "resolved": None}
        self._store.insert(row)
        self._log("jobs.submit", row, True, actor=user or source)
        return row

    def get(self, job_id: str) -> Optional[dict]:
        return self._store.get(job_id)

    def set_state(self, job_id: str, state: dict) -> bool:
        """Replaces a row's state blob while the job is still queued or running."""
        return self._store.update(job_id, only=LIVE, state=state)

    def list(self, status: str = "live", kind: Optional[str] = None, user: Optional[str] = None,
             thread_id: Optional[str] = None, since: Optional[float] = None, limit: int = 50,
             order: str = "created DESC") -> "list[dict]":
        where, params = [], []
        if status == "live":
            where.append("status IN ('queued','running')" + (" OR resolved >= ?" if since is not None else ""))
            if since is not None:
                where[-1] = f"({where[-1]})"
                params.append(since)
        elif status != "all":
            where.append("status=?")
            params.append(status)
        for col, val in (("kind", kind), ("user", user), ("thread_id", thread_id)):
            if val is not None:
                where.append(f"{col}=?")
                params.append(val)
        return self._store.rows(" AND ".join(where) or "1=1", tuple(params), order=order,
                                limit=max(1, min(LIST_MAX, int(limit))))

    def recent(self, limit: int = 8) -> "list[dict]":
        """Live rows first (soonest next_run), then the newest resolved ones, `limit` in all."""
        live = self._store.rows("status IN ('queued','running')", (),
                                order="CASE status WHEN 'running' THEN 0 ELSE 1 END, next_run, created", limit=limit)
        rest = self._store.rows("status NOT IN ('queued','running')", (), order="resolved DESC", limit=max(0, limit - len(live)))
        return live + rest

    def summary(self) -> dict:
        return self._store.summary(self._now())

    def view(self, row: dict, *, role: Optional[str] = None, user: Optional[str] = None, detail: bool = False) -> dict:
        k = self._kinds.get(row["kind"])
        out = {key: row.get(key) for key in ("id", "kind", "label", "status", "user", "source", "created", "not_before", "next_run",
                                             "last_run", "started", "resolved", "run_count", "attempts", "message", "thread_id",
                                             "exclusive")}
        out["kind_title"] = k.title if k else row["kind"]
        for key in ("created", "not_before", "next_run", "last_run", "started", "resolved"):
            out[f"{key}_local"] = local(row.get(key))
        out["can_cancel"] = row["status"] in LIVE and (role == "admin" or (role == "operator" and bool(user) and row.get("user") == user))
        if detail:
            out.update({"spec": row.get("spec"), "state": row.get("state"), "result": row.get("result")})
        return out

    # ── cancel ──
    def cancel(self, job_id: str, *, actor: str = "", message: str = CANCELLED) -> Optional[dict]:
        now = self._now()
        with self._store.lock:
            row = self._store.get(job_id)
            if not row or row["status"] not in LIVE:
                return None
            if not self._store.update(job_id, only=LIVE, status="cancelled", resolved=now, message=message,
                                      next_run=None, lease=None):
                return None
        if row["status"] == "running":
            self._signal(job_id)
            self._hook(self._kinds.get(row["kind"]), "on_cancel", {**row, "status": "cancelled", "message": message})
        row = self._store.get(job_id) or row
        self._log("jobs.cancel", row, True, actor=actor or row.get("user") or "system", detail={"message": message})
        self._hook(self._kinds.get(row["kind"]), "on_finish", row)
        return row

    def cancel_where(self, kind: str, *, thread_id: Optional[str] = None, spec_match: Optional[dict] = None,
                     message: str = CANCELLED, actor: str = "") -> int:
        n = 0
        for row in self.list("live", kind=kind, thread_id=thread_id, limit=LIST_MAX):
            if spec_match and any(row["spec"].get(k) != v for k, v in spec_match.items()):
                continue
            if self.cancel(row["id"], actor=actor, message=message):
                n += 1
        return n

    # ── dispatcher ──
    def tick(self) -> int:
        """One pass: reap workers, time out overruns, refresh leases, start due jobs, sweep daily. Never raises."""
        now = self._now()
        started = 0
        try:
            self._reap()
            self._timeouts(now)
            self._leases(now)
            started = self._start_due(now)
            if now - self._last_sweep >= SWEEP_EVERY_S:
                self._last_sweep = now
                days = int(getattr(self._cfg(), "history_days", 30) or 30)
                self._store.sweep(now - max(1, days) * 86400.0)
        except Exception as e:  # noqa: BLE001 — the loop outlives any one tick
            log.warning("jobs tick failed: %s: %s", type(e).__name__, e)
        return started

    def recover(self) -> int:
        """Boot: rows left running follow their kind's resume policy; unknown kinds fail."""
        now = self._now()
        n = 0
        for row in self._store.running():
            k = self._kinds.get(row["kind"])
            n += 1
            if k is not None and k.resume == "requeue":
                self._store.update(row["id"], status="queued", next_run=now, started=None, lease=None, message=RESTARTED)
                continue
            self._resolve_failed(row, RESTARTED if k is not None else "kind not registered", now)
        return n

    def _start_due(self, now: float) -> int:
        live = {r["id"]: r for r in self._store.running()}
        with self._lock:
            worker_ids = list(self._workers)
        for jid in worker_ids:
            if jid not in live:
                row = self._store.get(jid)
                if row is not None:
                    live[jid] = row
        held = {key for r in live.values() for key in r["exclusive"]}
        count = len(live)
        workers = max(1, int(getattr(self._cfg(), "workers", 4) or 4))
        started = 0
        for row in self._store.due(now):
            clash = next((key for key in row["exclusive"] if key in held), None)
            if clash:
                waiting = f"waiting for {clash}"
                if row["message"] != waiting:
                    self._store.update(row["id"], only=("queued",), message=waiting)
                continue
            if count >= workers:
                break
            if not self._store.update(row["id"], only=("queued",), status="running", started=now, last_run=now,
                                      lease=now + LEASE_S, message="running"):
                continue
            held.update(row["exclusive"])
            count += 1
            started += 1
            self._launch(self._store.get(row["id"]) or row)
            after = self._store.get(row["id"])
            if after is None or after["status"] != "running":
                count -= 1
                held.difference_update(row["exclusive"])
        return started

    def _launch(self, row: dict) -> None:
        k = self._kinds.get(row["kind"])
        if k is None:
            self._apply(row["id"], fail("kind not registered"))
            return
        ev = threading.Event()
        with self._lock:
            self._cancel[row["id"]] = ev
        box: dict = {}

        def work():
            try:
                box["outcome"] = k.run(Job(row, ev.is_set))
            except Exception as e:  # noqa: BLE001 — a handler's exception is that job's failure
                log.warning("job %s (%s) raised %s: %s", row["id"], row["kind"], type(e).__name__, e)
                box["outcome"] = fail(type(e).__name__)
        if self._inline:
            work()
            self._apply(row["id"], box.get("outcome") or fail("no outcome"))
            return
        t = threading.Thread(target=work, name=f"job-{row['kind']}-{row['id'][:6]}", daemon=True)
        with self._lock:
            self._workers[row["id"]] = {"thread": t, "box": box}
        t.start()

    def _reap(self) -> None:
        with self._lock:
            done = [(jid, w) for jid, w in self._workers.items() if not w["thread"].is_alive()]
            for jid, _ in done:
                self._workers.pop(jid, None)
        for jid, w in done:
            self._apply(jid, w["box"].get("outcome") or fail("no outcome"))

    def _apply(self, job_id: str, outcome: Outcome) -> None:
        """Applies a run's outcome; a row that is no longer running (cancelled, timed out) ignores it."""
        row = self._store.get(job_id)
        if not row or row["status"] != "running":
            self._forget(job_id)
            return
        now = self._now()
        fields: dict = {"lease": None, "started": None}
        if outcome.state is not None:
            fields["state"] = outcome.state
        if outcome.message:
            fields["message"] = outcome.message
        verb = outcome.verb
        if verb == "ok":
            fields["run_count"] = row["run_count"] + 1
            left = row["runs_left"]
            if row["period_s"] and (left is None or left > 1):
                fields.update({"status": "queued", "next_run": max(float(row["last_run"] or now) + float(row["period_s"]), now),
                               "runs_left": None if left is None else left - 1})
            else:
                fields.update({"status": "done", "resolved": now, "result": outcome.result, "next_run": None})
        elif verb == "again":
            fields.update({"status": "queued", "next_run": now + float(outcome.next_in or 0), "run_count": row["run_count"] + 1})
        elif verb == "finish":
            fields.update({"status": "done", "resolved": now, "result": outcome.result, "next_run": None,
                           "run_count": row["run_count"] + 1})
        else:
            fields["attempts"] = row["attempts"] + 1
            if outcome.retry_in is not None:
                fields.update({"status": "queued", "next_run": now + outcome.retry_in})
            else:
                self._forget(job_id)
                self._resolve_failed({**row, "state": fields.get("state", row["state"])}, outcome.message or "failed", now,
                                     attempts=fields["attempts"], alert=outcome.alert)
                return
        self._store.update(job_id, only=("running",), **fields)
        self._forget(job_id)
        if fields.get("status") == "done":
            self._hook(self._kinds.get(row["kind"]), "on_finish", self._store.get(job_id) or row)

    def _resolve_failed(self, row: dict, message: str, now: float, attempts: Optional[int] = None,
                        alert: bool = True) -> None:
        fields = {"status": "failed", "resolved": now, "message": message, "next_run": None, "lease": None, "started": None}
        if attempts is not None:
            fields["attempts"] = attempts
        if "state" in row and row["state"] is not None:
            fields["state"] = row["state"]
        if not self._store.update(row["id"], only=("running",), **fields):
            return
        row = self._store.get(row["id"]) or {**row, **fields}
        k = self._kinds.get(row["kind"])
        if self._alert is not None and alert:
            try:
                self._alert({"name": f"Job failed: {row['label']}", "source": "jobs", "metric": f"jobs/{row['kind']}/{row['id']}",
                             "severity": "warning", "value": row["attempts"], "threshold": 0,
                             "message": f"{k.title if k else row['kind']} · {row['label']} · {message}"})
            except Exception as e:  # noqa: BLE001 — the ledger row stands without the alert
                log.warning("job failure alert failed: %s: %s", type(e).__name__, e)
        self._log("jobs.failed", row, False, actor=row.get("user") or "system", detail={"message": message})
        self._hook(k, "on_finish", row)

    def _timeouts(self, now: float) -> None:
        for row in self._store.running():
            k = self._kinds.get(row["kind"])
            limit = k.limit_s(row["spec"]) if k else 3600.0
            if row["started"] is None or now - float(row["started"]) <= limit:
                continue
            self._signal(row["id"])
            self._hook(k, "on_cancel", {**row, "message": TIMED_OUT})
            self._resolve_failed(row, TIMED_OUT, now)

    def _leases(self, now: float) -> None:
        """Refreshes a running row's lease only once less than two thirds of LEASE_S is left."""
        for row in self._store.running():
            lease = row["lease"]
            if lease is not None and float(lease) - now > 2 * LEASE_S / 3:
                continue
            self._store.update(row["id"], only=("running",), lease=now + LEASE_S)

    def _signal(self, job_id: str) -> None:
        with self._lock:
            ev = self._cancel.get(job_id)
        if ev is not None:
            ev.set()

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._cancel.pop(job_id, None)

    def _hook(self, k: Optional[Kind], name: str, row: dict) -> None:
        fn = getattr(k, name, None) if k else None
        if fn is None:
            return
        try:
            fn(Job(row))
        except Exception as e:  # noqa: BLE001 — a hook never breaks the dispatcher
            log.warning("job %s %s hook failed: %s: %s", row.get("id"), name, type(e).__name__, e)

    def _log(self, action: str, row: dict, ok_: bool, *, actor: str, detail: Optional[dict] = None) -> None:
        if self._audit is None:
            return
        try:
            self._audit({"action": action, "actor": actor, "target": row.get("label") or row.get("id"), "ok": ok_,
                         "detail": {"job_id": row.get("id"), "kind": row.get("kind"), "source": row.get("source"),
                                    "user": row.get("user"), **(detail or {})}})
        except Exception as e:  # noqa: BLE001 — an audit failure never breaks a job
            log.warning("jobs audit failed: %s: %s", type(e).__name__, e)


def register_routes(app, service: Service, *, role_of: Callable[[], Optional[str]], user_of: Callable[[], str]) -> None:
    """GET /api/jobs, GET /api/jobs/<id>, POST /api/jobs (api kinds, operator+), POST /api/jobs/<id>/cancel (owner or admin)."""
    from flask import g, jsonify, request as flask_request

    def _who() -> "tuple[Optional[str], str]":
        role = role_of()
        return role, (user_of() or "") if role else ""

    @app.route("/api/jobs")
    def jobs_list():
        role, user = _who()
        if role is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        status = (flask_request.args.get("status") or "live").strip()
        if status not in ("live", "all") + STATUSES:
            return jsonify({"ok": False, "error": "bad status"}), 400
        try:
            limit = int(flask_request.args.get("limit") or 50)
        except ValueError:
            limit = 50
        rows = service.list(status, kind=flask_request.args.get("kind") or None,
                            user=flask_request.args.get("user") or None, limit=limit)
        return jsonify({"ok": True, "jobs": [service.view(r, role=role, user=user) for r in rows],
                        "summary": service.summary()})

    @app.route("/api/jobs/<job_id>")
    def jobs_get(job_id):
        role, user = _who()
        if role is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        row = service.get(job_id[:32])
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "job": service.view(row, role=role, user=user, detail=True)})

    @app.route("/api/jobs", methods=["POST"])
    def jobs_submit():
        role, user = _who()
        if role is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if role not in ("operator", "admin"):
            return jsonify({"ok": False, "error": "operator role required"}), 403
        body = flask_request.get_json(silent=True) or {}
        k = service.kind(str(body.get("kind") or ""))
        if k is None:
            return jsonify({"ok": False, "error": "unknown kind"}), 404
        if not k.api:
            return jsonify({"ok": False, "error": "kind not submittable"}), 409
        try:
            nb = float(body["not_before"]) if body.get("not_before") is not None else None
            period = float(body["period_s"]) if body.get("period_s") is not None else None
            left = int(body["runs_left"]) if body.get("runs_left") is not None else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "not_before, period_s and runs_left must be numbers"}), 400
        try:
            row = service.submit(k.name, body.get("spec") or {}, user=user, role=role, source="api",
                                 label=(str(body.get("label")) if body.get("label") else None),
                                 not_before=nb, period_s=period, runs_left=left)
        except JobError as e:
            return jsonify({"ok": False, "error": e.message}), e.status
        g._audit_extra = {"job_id": row["id"], "kind": row["kind"], "label": row["label"]}
        return jsonify({"ok": True, "job": service.view(row, role=role, user=user)})

    @app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
    def jobs_cancel(job_id):
        role, user = _who()
        if role is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        row = service.get(job_id[:32])
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        if not service.view(row, role=role, user=user)["can_cancel"]:
            if row["status"] not in LIVE:
                return jsonify({"ok": False, "error": f"job is {row['status']}"}), 409
            return jsonify({"ok": False, "error": "not your job"}), 403
        out = service.cancel(row["id"], actor=user)
        if out is None:
            return jsonify({"ok": False, "error": f"job is {row['status']}"}), 409
        g._audit_extra = {"job_id": out["id"], "kind": out["kind"], "label": out["label"]}
        return jsonify({"ok": True, "job": service.view(out, role=role, user=user)})


def start_thread(service: Service, shutting_down: Callable[[], bool]):
    """Daemon loop ticking the service every POLL_S; None under pytest."""
    if "pytest" in sys.modules:
        return None

    def _loop():
        while not shutting_down():
            service.tick()
            time.sleep(POLL_S)

    t = threading.Thread(target=_loop, name="jobs-dispatcher", daemon=True)
    t.start()
    return t
