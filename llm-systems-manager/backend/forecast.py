"""Forecast (#1031): the scheduled run job, the findings store, dedup and clearing, alerts and the routes."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import forecast_checks
import forecast_effort
import jobs

log = logging.getLogger("llm-systems-manager.forecast")

KIND = "forecast_run"
TITLE = "Forecast run"
ACTOR = "forecast"
PERIODS = {"1h": 3600.0, "6h": 21600.0, "12h": 43200.0, "daily": 86400.0, "weekly": 604800.0}
BUCKETS = (86400.0, 3 * 86400.0, 7 * 86400.0, 30 * 86400.0)
STATES = ("pending", "ok", "collecting", "failed", "off")
DIGEST_ID = "weekly_digest"
RESOLVED_MAX = 200  # cleared and dismissed rows the page gets to page through
CLEAN_RUNS = 2
REALERT_RUNS = 2
RESTORE_RUNS = 3
RUN_SCAN = 20
SWEEP_DAYS = 90
MAX_RUN_S = 1800.0
TURN_DEADLINE = 0.8       # share of MAX_RUN_S after which no new per-check turn starts
DAY = 86400.0
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RANK = {s: i for i, s in enumerate(forecast_checks.SEVERITIES)}


def rank(severity: Any) -> int:
    return _RANK.get(str(severity or ""), 0)


def init_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS forecast_findings (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, check_id TEXT NOT NULL, host TEXT, subject TEXT,
            severity TEXT NOT NULL, summary TEXT, detail TEXT, since REAL, predicted_at REAL, rate REAL, unit TEXT,
            confidence TEXT, suggested_action TEXT, graph TEXT, verified TEXT, status TEXT NOT NULL,
            first_seen REAL, last_seen REAL, last_reported REAL, clean_runs INTEGER NOT NULL DEFAULT 0,
            thread_id TEXT, alert_id TEXT, dismissed_by TEXT, resolved REAL,
            gate_streak INTEGER NOT NULL DEFAULT 0, alert_severity TEXT, tower_note TEXT);
        CREATE INDEX IF NOT EXISTS idx_forecast_findings_status ON forecast_findings(status, check_id);
        CREATE TABLE IF NOT EXISTS forecast_runs (
            id TEXT PRIMARY KEY, started REAL, finished REAL, mode TEXT, checks TEXT, found INTEGER,
            reported INTEGER, message TEXT, digest_at REAL, digest TEXT, tier TEXT, tier_reason TEXT,
            model TEXT, tok_s REAL, timed_out INTEGER, tower_calls INTEGER, digest_discarded INTEGER);
        CREATE INDEX IF NOT EXISTS idx_forecast_runs_started ON forecast_runs(started);
    """)
    have = {r[1] for r in conn.execute("PRAGMA table_info(forecast_runs)").fetchall()}
    for name, kind in (("digest", "TEXT"), ("tier", "TEXT"), ("tier_reason", "TEXT"), ("model", "TEXT"),
                       ("tok_s", "REAL"), ("timed_out", "INTEGER"), ("tower_calls", "INTEGER"),
                       ("digest_discarded", "INTEGER")):
        if name not in have:
            conn.execute(f"ALTER TABLE forecast_runs ADD COLUMN {name} {kind}")
    found = {r[1] for r in conn.execute("PRAGMA table_info(forecast_findings)").fetchall()}
    if "gate_streak" not in found:
        conn.execute("ALTER TABLE forecast_findings ADD COLUMN gate_streak INTEGER NOT NULL DEFAULT 0")
    if "alert_severity" not in found:
        conn.execute("ALTER TABLE forecast_findings ADD COLUMN alert_severity TEXT")
        conn.execute("UPDATE forecast_findings SET alert_severity = severity WHERE alert_id IS NOT NULL")
    if "tower_note" not in found:
        conn.execute("ALTER TABLE forecast_findings ADD COLUMN tower_note TEXT")
    conn.commit()


def _dump(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, default=str)


def _load(text: Any) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def bucket(predicted_at: Optional[float], now: float) -> int:
    """Index of the time-to-impact band; len(BUCKETS) beyond the last band or with no date."""
    if predicted_at is None:
        return len(BUCKETS)
    left = float(predicted_at) - float(now)
    for i, edge in enumerate(BUCKETS):
        if left < edge:
            return i
    return len(BUCKETS)


def _local(ts: float, tz_offset_s: float) -> datetime:
    return datetime.fromtimestamp(float(ts) + float(tz_offset_s or 0.0), tz=timezone.utc)


def _iso_week(ts: float, tz_offset_s: float) -> str:
    """The local ISO week the weekly digest names itself after, e.g. 2026-W38."""
    cal = _local(ts, tz_offset_s).isocalendar()
    return f"{cal[0]}-W{cal[1]:02d}"


def _hm(text, default: "tuple[int, int]") -> "tuple[int, int]":
    """Hour and minute of an HH:MM setting, else the default."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(text or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else default


@dataclass(frozen=True)
class Schedule:
    """When the periodic run fires: a preset or custom period, the weekly day, and the local time of day."""
    every: str = "daily"
    hours: int = 8
    day: str = "mon"
    at: str = "03:00"

    @classmethod
    def of(cls, cfg) -> "Schedule":
        every = str(getattr(cfg, "every", "daily") or "daily")
        day = str(getattr(cfg, "run_day", "mon") or "mon")
        h, m = _hm(getattr(cfg, "at", "03:00"), (3, 0))
        return cls(every if every in PERIODS or every == "custom" else "daily",
                   max(1, min(720, int(getattr(cfg, "every_hours", 8) or 8))),
                   day if day in WEEKDAYS else "mon", f"{h:02d}:{m:02d}")

    @property
    def period_s(self) -> float:
        return self.hours * 3600.0 if self.every == "custom" else PERIODS[self.every]

    def spec(self) -> dict:
        """The identity a periodic job carries; parts a period does not use are left out."""
        out = {"every": self.every, "at": self.at}
        if self.every == "custom":
            out["hours"] = self.hours
        if self.every == "weekly":
            out["day"] = self.day
        return out


def next_slot(now: float, sched: Schedule, tz_offset_s: float = 0.0) -> float:
    """Next start after now: weekly on the local day and time, otherwise every period counted from that local time."""
    off = float(tz_offset_s or 0.0)
    here = _local(now, off)
    h, m = _hm(sched.at, (3, 0))
    if sched.every == "weekly":
        slot = here.replace(hour=h, minute=m, second=0, microsecond=0)
        slot += timedelta(days=(WEEKDAYS.index(sched.day) - slot.weekday()) % 7)
        if slot <= here:
            slot += timedelta(days=7)
        return slot.timestamp() - off
    period = sched.period_s
    anchor = h * 3600.0 + m * 60.0
    local = float(now) + off
    return anchor + (math.floor((local - anchor) / period) + 1) * period - off


def digest_slot(now: float, day: str, at: str, tz_offset_s: float = 0.0) -> float:
    """Most recent local weekday and time slot at or before now."""
    off = float(tz_offset_s or 0.0)
    here = _local(now, off)
    h, m = _hm(at, (8, 0))
    slot = here.replace(hour=h, minute=m, second=0, microsecond=0)
    want = WEEKDAYS.index(str(day or "mon")) if str(day or "mon") in WEEKDAYS else 0
    slot -= timedelta(days=(slot.weekday() - want) % 7)
    if slot > here:
        slot -= timedelta(days=7)
    return slot.timestamp() - off


def schedule_match(row: dict, sched: Schedule) -> bool:
    """True when a live periodic job already carries this schedule."""
    spec = row.get("spec") or {}
    want = sched.spec()
    return float(row.get("period_s") or 0.0) == sched.period_s and all(spec.get(k) == v for k, v in want.items())


DRIFT_S = 300.0


def drifted(row: dict, sched: Schedule, now: float, tz_offset_s: float = 0.0) -> bool:
    """True when a queued job's next run has slipped more than DRIFT_S off its aligned slot."""
    if row.get("next_run") is None:
        return False
    return abs(float(row["next_run"]) - next_slot(now, sched, tz_offset_s)) > DRIFT_S


def order(rows: "list[dict]") -> "list[dict]":
    """Critical first, then the nearest predicted date; findings without a date come last."""
    return sorted(rows, key=lambda r: (-rank(r.get("severity")),
                                       float("inf") if r.get("predicted_at") is None else float(r["predicted_at"]),
                                       float(r.get("first_seen") or 0.0)))


class Store:
    """Findings and run rows in manager.db; the factory hands back a connection for the calling thread."""
    KEYS = ("id", "fingerprint", "check_id", "host", "subject", "severity", "summary", "detail", "since",
            "predicted_at", "rate", "unit", "confidence", "suggested_action", "graph", "verified", "status",
            "first_seen", "last_seen", "last_reported", "clean_runs", "thread_id", "alert_id", "dismissed_by",
            "resolved", "gate_streak", "alert_severity", "tower_note")
    RUN_KEYS = ("id", "started", "finished", "mode", "checks", "found", "reported", "message", "digest_at", "digest",
                "tier", "tier_reason", "model", "tok_s", "timed_out", "tower_calls", "digest_discarded")

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]):
        self._conn = conn_factory
        self._lock = threading.RLock()

    def _row(self, r) -> dict:
        row = dict(zip(self.KEYS, r))
        row["graph"] = _load(row["graph"])
        return row

    def _rows(self, where: str = "1=1", params: tuple = (), orderby: str = "first_seen, id",
              limit: Optional[int] = None) -> "list[dict]":
        sql = f"SELECT {', '.join(self.KEYS)} FROM forecast_findings WHERE {where} ORDER BY {orderby}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            return [self._row(r) for r in self._conn().execute(sql, params).fetchall()]

    def upsert(self, row: dict, only: "Optional[tuple[str, ...]]" = None) -> Optional[dict]:
        """Inserts a finding, or updates the row with the same fingerprint; None when `only` no longer matches."""
        with self._lock:
            cur = self.find(row["fingerprint"])
            if cur is not None:
                if not self.set(cur["id"], only=only, **{k: v for k, v in row.items() if k not in ("id", "fingerprint")}):
                    return None
                return self.get(cur["id"]) or cur
            full = {k: row.get(k) for k in self.KEYS}
            full["id"] = row.get("id") or uuid.uuid4().hex[:16]
            full["status"] = full["status"] or "open"
            full["clean_runs"] = int(full["clean_runs"] or 0)
            full["gate_streak"] = int(full["gate_streak"] or 0)
            c = self._conn()
            c.execute(f"INSERT INTO forecast_findings ({', '.join(self.KEYS)}) "
                      f"VALUES ({', '.join('?' * len(self.KEYS))})",
                      tuple(_dump(full[k]) if k == "graph" else full[k] for k in self.KEYS))
            c.commit()
            return self.get(full["id"]) or full

    def set(self, fid: str, *, only: "Optional[tuple[str, ...]]" = None, **cols) -> bool:
        """Sets the given columns; with `only`, only while the row's status is one of those."""
        sets = {k: (_dump(v) if k == "graph" else v) for k, v in cols.items() if k in self.KEYS and k != "id"}
        if not sets:
            return False
        guard, params = "", ()
        if only:
            guard = f" AND status IN ({', '.join('?' * len(only))})"
            params = tuple(only)
        with self._lock:
            c = self._conn()
            n = c.execute(f"UPDATE forecast_findings SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?{guard}",
                          (*sets.values(), fid, *params)).rowcount
            c.commit()
        return n == 1

    def get(self, fid: str) -> Optional[dict]:
        rows = self._rows("id=?", (fid,))
        return rows[0] if rows else None

    def find(self, fingerprint: str) -> Optional[dict]:
        rows = self._rows("fingerprint=?", (fingerprint,))
        return rows[0] if rows else None

    def open(self) -> "list[dict]":
        return self._rows("status='open'")

    def cleared(self, limit: int = 20) -> "list[dict]":
        return self._rows("status IN ('cleared','dismissed')", (), orderby="resolved DESC, id", limit=limit)

    def unclosed(self) -> "list[dict]":
        """Resolved findings whose alert has not been closed yet."""
        return self._rows("status IN ('cleared','dismissed') AND alert_id IS NOT NULL")

    def add_run(self, row: dict) -> dict:
        full = {k: row.get(k) for k in self.RUN_KEYS}
        full["id"] = row.get("id") or uuid.uuid4().hex[:16]
        with self._lock:
            c = self._conn()
            c.execute(f"INSERT OR REPLACE INTO forecast_runs ({', '.join(self.RUN_KEYS)}) "
                      f"VALUES ({', '.join('?' * len(self.RUN_KEYS))})",
                      tuple(_dump(full[k]) if k == "checks" else full[k] for k in self.RUN_KEYS))
            c.commit()
        return full

    def recent_runs(self, limit: int = 20) -> "list[dict]":
        """The newest run rows first, for the tier memory."""
        with self._lock:
            rows = self._conn().execute(f"SELECT {', '.join(self.RUN_KEYS)} FROM forecast_runs "
                                        "ORDER BY started DESC, rowid DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(zip(self.RUN_KEYS, r)) for r in rows]

    def last_run(self) -> Optional[dict]:
        with self._lock:
            r = self._conn().execute(f"SELECT {', '.join(self.RUN_KEYS)} FROM forecast_runs "
                                     "ORDER BY started DESC, rowid DESC LIMIT 1").fetchone()
        if r is None:
            return None
        row = dict(zip(self.RUN_KEYS, r))
        row["checks"] = _load(row["checks"]) or {}
        return row

    def sweep(self, days: int = SWEEP_DAYS, now: Optional[float] = None) -> int:
        """Drops cleared and dismissed findings and run rows older than `days`."""
        cutoff = (time.time() if now is None else float(now)) - max(1, int(days)) * DAY
        with self._lock:
            c = self._conn()
            n = c.execute("DELETE FROM forecast_findings WHERE status IN ('cleared','dismissed') "
                          "AND COALESCE(resolved, last_seen, 0) < ?", (cutoff,)).rowcount
            n += c.execute("DELETE FROM forecast_runs WHERE started < ?", (cutoff,)).rowcount
            c.commit()
        return int(n)


class Forecast:
    """Runs the checks as a job, stores and de-duplicates findings, raises and closes their alerts."""
    def __init__(self, service: jobs.Service, store: Store, *, cfg: Callable[[], Any],
                 data_factory: Callable[[float, float], Any], alert_post: Callable[[dict], Optional[str]],
                 alert_close: Callable[[str], bool], audit: Optional[Callable[[dict], None]] = None,
                 tower_pass: Optional[Callable] = None, model_signals: Optional[Callable[[str], dict]] = None,
                 now: Callable[[], float] = time.time,
                 tz_offset_s: Callable[[], float] = lambda: 0, checks: Optional[list] = None):
        self._svc, self._store, self._cfg = service, store, cfg
        self._data, self._post, self._close = data_factory, alert_post, alert_close
        self._audit, self._tower, self._now, self._tz = audit, tower_pass, now, tz_offset_s
        self._signals = model_signals
        self._checks = list(checks) if checks is not None else list(forecast_checks.CHECKS)
        self._submit_lock = threading.Lock()
        self._progress: Optional[dict] = None
        if service.kind(KIND) is None:
            service.register(jobs.Kind(KIND, TITLE, run=self._run, exclusive=lambda spec: ["forecast"],
                                       resume="requeue", max_run_s=MAX_RUN_S))

    # ── schedule ──
    def ensure_schedule(self) -> None:
        """Keeps exactly one periodic run job matching the current period and hour; cancels it while Forecast is off."""
        cfg = self._cfg()
        with self._submit_lock:
            live = [r for r in self._svc.list(status="live", kind=KIND) if (r.get("spec") or {}).get("periodic")]
            if not bool(getattr(cfg, "enabled", False)):
                for row in live:
                    self._svc.cancel(row["id"], actor=ACTOR, message="forecast is off")
                return
            sched = Schedule.of(cfg)
            tz = float(self._tz() or 0.0)
            matched = [r for r in live if schedule_match(r, sched)]
            keep = matched[0] if matched else None
            if keep is not None and keep["status"] != "running" and drifted(keep, sched, self._now(), tz):
                log.debug("forecast schedule drifted off its %s slot; resubmitting", sched.every)
                keep = None
            for row in live:
                if row is keep or row["status"] == "running":
                    continue
                self._svc.cancel(row["id"], actor=ACTOR, message="the forecast schedule changed")
            if keep is not None or any(r["status"] == "running" for r in live):
                return
            self._svc.submit(KIND, {"periodic": True, **sched.spec()}, source="system",
                             period_s=sched.period_s, not_before=next_slot(self._now(), sched, tz))

    def run_now(self, user: str, role: str) -> dict:
        """Queues a one-shot run; 409 while Forecast is off or a run is already live."""
        if not bool(getattr(self._cfg(), "enabled", False)):
            raise jobs.JobError("Forecast is off", 409)
        with self._submit_lock:
            for row in self._svc.list(status="live", kind=KIND):
                if not (row.get("spec") or {}).get("periodic") or row["status"] == "running":
                    raise jobs.JobError("A forecast run is already in progress", 409)
            return self._svc.submit(KIND, {"periodic": False}, user=user, role=role, source="ui")

    def _run(self, job) -> jobs.Outcome:
        try:
            out = self.run_once(job.cancelled)
        finally:
            self._progress = None
        return jobs.ok(result=out, message=f"{out['found']} found, {out['reported']} new")

    def _step(self, **cols) -> None:
        """Replaces the live run's progress with a copy carrying these columns."""
        self._progress = {**(self._progress or {}), **cols}

    # ── the run ──
    def run_once(self, cancelled: Callable[[], bool] = lambda: False) -> dict:
        """One pass: detect, Tower's part, merge, then clear the findings that no longer fire."""
        cfg = self._cfg()
        started = self._now()
        tz = float(self._tz() or 0.0)
        window_s = float(getattr(cfg, "window_days", 14) or 14) * DAY
        disabled = set(getattr(cfg, "checks_disabled", None) or [])
        states = {c.id: {"state": "pending", "have_days": None, "min_days": float(c.min_days), "found": 0}
                  for c in self._checks}
        last = self._store.last_run()
        slot = digest_slot(started, getattr(cfg, "digest_day", "mon"), getattr(cfg, "digest_at", "08:00"), tz)
        was = (last or {}).get("digest_at")
        digest_due = was is not None and float(was) < slot
        data = self._data(started, window_s)
        profile, tier_reason, model = self._effort(last)
        day = _local(started, tz).strftime("%Y-%m-%d")
        self._begin_run(profile)
        if not profile.model_only:
            self._clear_model_findings()
        self._clear_digests(week=_iso_week(started, tz))
        todo = [c.id for c in self._checks if c.id not in disabled and (c.id != DIGEST_ID or digest_due)]
        self._progress = {"started": started, "total": len(todo), "done": 0, "stage": "checks", "check": None}
        mode, found, reported = "code", 0, 0
        seen: "dict[str, set]" = {}
        touched: "list[dict]" = []
        for c in self._checks:
            if cancelled() or not bool(getattr(self._cfg(), "enabled", True)):
                log.debug("forecast run stopped before check %s", c.id)
                break
            if c.id in disabled or (c.id == DIGEST_ID and not digest_due):
                states[c.id]["state"] = "off"
                continue
            self._step(stage="checks", check=c.title, done=sum(1 for i in todo if states[i]["state"] != "pending"))
            try:
                rows = [f.to_row() for f in (c.detect(data) or [])]
            except forecast_checks.Collecting as e:
                states[c.id].update(state="collecting", have_days=e.have_days, min_days=e.min_days)
                continue
            except Exception as e:  # noqa: BLE001 — one check never stops the run
                log.warning("forecast check %s failed: %s: %s", c.id, type(e).__name__, e)
                states[c.id]["state"] = "failed"
                continue
            merged, thread_id, talked = None, None, False
            if (profile.investigate and rows and bool(getattr(c, "tower", False))
                    and self._now() - started < TURN_DEADLINE * MAX_RUN_S):
                self._step(stage="investigating")
                thread_id = self._start_thread(f"Forecast {day} · {c.title}")
                merged = self._tower_pass(c, data, rows, thread_id)
                talked = thread_id is not None
                thread_id = thread_id if self._end_thread(thread_id) else None
            if merged is None:
                merged = [(r, "code") for r in rows]
            elif any(str(v) != "code" for _r, v in merged):
                mode = "tower"
            states[c.id].update(state="ok", found=len(merged))
            fingerprints = set()
            for row, verified in merged:
                stored, was_reported = self.merge(row, verified, thread_id=thread_id,
                                                  forget_thread=talked and thread_id is None)
                fingerprints.add(stored["fingerprint"])
                found += 1
                reported += 1 if was_reported else 0
                if stored.get("status") == "open":
                    touched.append(stored)
                    if str(stored.get("check_id")) == DIGEST_ID:
                        self._clear_digests(keep=stored["fingerprint"])
            seen[c.id] = fingerprints
        self._step(stage="closing", check=None, done=len(todo))
        cleared = self._clear(states, seen)
        self._retry_closes()
        digest = None
        stopped = cancelled() or not bool(getattr(self._cfg(), "enabled", True))
        if profile.tier != "off" and touched and not stopped:
            self._step(stage="analysis")
            digest = self._digest(touched)
            caused = self._causes(touched) if profile.per_finding_cause else 0
            if digest or caused:
                mode = "tower"
        finished = self._now()
        counts = {s: sum(1 for v in states.values() if v["state"] == s) for s in STATES}
        summary = {"mode": mode, "checks": states, "found": found, "reported": reported}
        ran_digest = (states.get(DIGEST_ID) or {}).get("state") in ("ok", "collecting", "failed")
        self._store.add_run({"started": started, "finished": finished, "mode": mode, "checks": states,
                             "found": found, "reported": reported, "message": f"{found} found, {reported} new",
                             "digest_at": slot if (ran_digest or was is None) else was, "digest": digest,
                             "tier": profile.tier, "tier_reason": tier_reason, "model": model,
                             "tok_s": self._tower_tok_s(), "timed_out": 1 if self._tower_timed_out() else 0,
                             "tower_calls": self._tower_calls(),
                             "digest_discarded": 1 if (digest is None and self._digest_discarded()) else 0})
        self._store.sweep(SWEEP_DAYS, now=finished)
        self._log({"actor": ACTOR, "action": "forecast.run", "target": mode, "ok": True,
                   "detail": {**counts, "found": found, "reported": reported, "cleared": cleared,
                              "ms": int(max(0.0, finished - started) * 1000)}})
        log.debug("forecast run %s: %d checks, %d found, %d new, %d cleared", mode, len(states), found, reported, cleared)
        self._progress = None
        return summary

    def merge(self, finding_row: dict, verified: str, thread_id: Optional[str] = None,
              forget_thread: bool = False) -> "tuple[dict, bool]":
        """Upserts one finding by fingerprint; reports it when it is new, worse, nearer or re-opened. Tower's note
        is rewritten every run, like the detail; `forget_thread` drops a conversation id whose thread was not kept."""
        now = self._now()
        fingerprint = finding_row.get("fingerprint") or "{}|{}|{}".format(
            finding_row.get("check"), finding_row.get("host") or "-", finding_row.get("subject") or "-")
        cur = self._store.find(fingerprint)
        severity = str(finding_row.get("severity") or "info")
        row = {"fingerprint": fingerprint, "check_id": finding_row.get("check"), "host": finding_row.get("host"),
               "subject": finding_row.get("subject"), "severity": severity, "summary": finding_row.get("summary"),
               "detail": finding_row.get("detail"), "since": finding_row.get("since"),
               "predicted_at": finding_row.get("predicted_at"), "rate": finding_row.get("rate"),
               "unit": finding_row.get("unit"), "confidence": finding_row.get("confidence"),
               "suggested_action": finding_row.get("suggested_action"), "graph": finding_row.get("graph"),
               "verified": verified, "last_seen": now, "clean_runs": 0, "tower_note": finding_row.get("tower_note")}
        if thread_id is not None or forget_thread:
            row["thread_id"] = thread_id
        if cur is None:
            row.update(status="open", first_seen=now)
            return self._report(self._store.upsert(row), True)
        reported = False
        if cur["status"] == "cleared":
            row.update(status="open", first_seen=now, resolved=None, gate_streak=0)
            reported = True
            # Clears the stored alert only once it is closed; a failed close keeps it on the row.
            if not cur.get("alert_id") or self._close_alert(cur):
                row.update(alert_id=None, alert_severity=None, last_reported=None)
        elif cur["status"] == "dismissed":
            if rank(severity) > rank(cur["severity"]):
                row.update(status="open", resolved=None, gate_streak=0)
                reported = True
                if not cur.get("alert_id") or self._close_alert(cur):
                    row.update(alert_id=None, alert_severity=None, last_reported=None)
        else:
            reported = (rank(severity) > rank(cur["severity"])
                        or bucket(finding_row.get("predicted_at"), now) < bucket(cur["predicted_at"], now))
        stored = self._store.upsert(row, only=(cur["status"],))
        if stored is None:
            log.debug("forecast finding %s changed during the run; merging it again next run", fingerprint)
            return self._store.find(fingerprint) or cur, False
        return self._report(stored, reported)

    def dismiss(self, fid: str, user: str) -> bool:
        """Marks an open finding dismissed and closes its alert."""
        row = self._store.get(fid)
        if row is None or row["status"] != "open":
            return False
        if not self._store.set(fid, only=("open",), status="dismissed", dismissed_by=user or "", resolved=self._now()):
            return False
        self._resolve_alert(row, ("dismissed",))
        self._log({"actor": user or ACTOR, "action": "forecast.dismiss", "target": row["fingerprint"], "ok": True,
                   "detail": {"check": row["check_id"], "host": row["host"], "severity": row["severity"]}})
        return True

    # ── views ──
    def view(self) -> dict:
        """Payload of GET /api/forecast."""
        cfg = self._cfg()
        last = self._store.last_run()
        states = (last or {}).get("checks") or {}
        live = self._svc.list(status="live", kind=KIND)
        periodic = next((r for r in live if (r.get("spec") or {}).get("periodic")), None)
        tier = (last or {}).get("tier")
        run = self._run_view(live)
        return {"enabled": bool(getattr(cfg, "enabled", False)), "mode": (last or {}).get("mode"),
                "run": run, "last_result": (last or {}).get("message"),
                "digest": (last or {}).get("digest"),
                "tower": None if tier in (None, "off") else {"tier": tier, "reason": (last or {}).get("tier_reason"),
                                                             "model": (last or {}).get("model"),
                                                             "digest_discarded": bool((last or {}).get("digest_discarded"))},
                "last_run": (last or {}).get("finished"), "next_run": (periodic or {}).get("next_run"),
                "running": run is not None,
                "window_days": int(getattr(cfg, "window_days", 14) or 14),
                "checks": [self._check_view(c, states.get(c.id)) for c in self._checks],
                "findings": [self._finding_view(r) for r in order(self._store.open())],
                "cleared": [self._finding_view(r) for r in self._store.cleared(RESOLVED_MAX)]}

    def _run_view(self, live: "list[dict]") -> Optional[dict]:
        """The run in flight: queued (a Run now waiting to start) or running with its progress; None when idle."""
        if any(r["status"] == "running" for r in live):
            p = dict(self._progress or {})
            started = p.get("started")
            return {"state": "running", "stage": p.get("stage") or "checks", "check": p.get("check"),
                    "done": int(p.get("done") or 0), "total": int(p.get("total") or 0),
                    "elapsed_s": None if started is None else max(0.0, self._now() - float(started))}
        if any(not (r.get("spec") or {}).get("periodic") for r in live):
            return {"state": "queued"}
        return None

    def tower_view(self) -> dict:
        """Trimmed open findings and check states for the Tower `forecast` read tool."""
        cfg = self._cfg()
        last = self._store.last_run()
        states = (last or {}).get("checks") or {}
        tz = float(self._tz() or 0.0)
        return {"enabled": bool(getattr(cfg, "enabled", False)), "last_run": (last or {}).get("finished"),
                "findings": [{"host": r["host"], "check": self._title(r["check_id"]), "severity": r["severity"],
                              "summary": r["summary"],
                              "predicted": None if r["predicted_at"] is None else _local(r["predicted_at"], tz).strftime("%Y-%m-%d"),
                              "verified": r["verified"]} for r in order(self._store.open())],
                "checks": [{"title": c.title, "state": (states.get(c.id) or {}).get("state", "pending")}
                           for c in self._checks]}

    def _check_view(self, check, state: Optional[dict]) -> dict:
        state = state or {}
        return {"id": check.id, "title": check.title, "state": state.get("state") or "pending",
                "have_days": state.get("have_days"), "min_days": state.get("min_days", float(check.min_days)),
                "found": int(state.get("found") or 0)}

    def _finding_view(self, row: dict) -> dict:
        """One finding as the API exposes it; fingerprint, clean_runs, last_reported and alert_id stay internal."""
        out = {k: row[k] for k in ("id", "host", "subject", "severity", "summary", "detail", "since", "predicted_at",
                                   "rate", "unit", "confidence", "suggested_action", "graph", "verified", "status",
                                   "first_seen", "last_seen", "resolved", "thread_id", "dismissed_by", "tower_note")}
        return {**out, "check": row["check_id"], "title": self._title(row["check_id"])}

    def _title(self, check_id: Any) -> str:
        for c in self._checks:
            if c.id == check_id:
                return c.title
        return forecast_checks.TITLES.get(str(check_id), str(check_id))

    # ── clearing and alerts ──
    def _clear(self, states: dict, seen: dict) -> int:
        """Counts a clean run for every open finding whose check ran ok without it; clears it at CLEAN_RUNS."""
        now = self._now()
        cleared = 0
        for row in self._store.open():
            if (states.get(row["check_id"]) or {}).get("state") != "ok":
                continue
            if row["fingerprint"] in seen.get(row["check_id"], ()):
                continue
            runs = int(row["clean_runs"] or 0) + 1
            if runs < CLEAN_RUNS:
                self._store.set(row["id"], only=("open",), clean_runs=runs)
                continue
            if not self._store.set(row["id"], only=("open",), clean_runs=runs, status="cleared", resolved=now):
                continue
            self._resolve_alert(row, ("cleared",))
            cleared += 1
        return cleared

    def _wants_alert(self, row: dict) -> bool:
        cfg = self._cfg()
        return (bool(getattr(cfg, "alerts", False))
                and rank(row["severity"]) >= rank(getattr(cfg, "alert_min_severity", "warning"))
                and str(row["confidence"]) != "low" and str(row["verified"]) != "model")

    def _report(self, row: dict, reported: bool) -> "tuple[dict, bool]":
        """Posts the alert when the gates allow it; a post that leaves no id at all is retried next run.
        A finding that no longer passes the gates has its alert closed instead."""
        if row["status"] != "open":
            return row, reported
        if not self._wants_alert(row):
            return self._drop_alert(row), reported
        row = self._bump_streak(row)
        streak_ok = int(row["gate_streak"] or 0) >= REALERT_RUNS
        if row["alert_id"]:
            # A rise above the severity the alert was posted at closes it and raises a new one.
            if rank(row["severity"]) <= rank(row["alert_severity"] or row["severity"]):
                return row, reported
            if not self._close_alert(row) or not self._store.set(row["id"], only=("open",), alert_id=None,
                                                                 alert_severity=None):
                return row, reported
            row = {**row, "alert_id": None, "alert_severity": None}
        elif row["last_reported"] is not None and not streak_ok:
            # An alert dropped earlier is raised again only after REALERT_RUNS runs inside the gates.
            return row, reported
        elif not reported and not streak_ok:
            return row, reported
        payload = {"name": f"Forecast: {self._title(row['check_id'])}", "source": "forecast",
                   "metric": f"{row['check_id']}/{forecast_checks.slug(row['subject'])}",
                   "host": row["host"], "severity": row["severity"], "value": row["rate"] or 0, "threshold": 0,
                   "message": row["summary"]}
        try:
            alert_id = self._post(payload)
        except Exception as e:  # noqa: BLE001 — the finding stands without its alert
            log.warning("forecast alert post failed: %s: %s", type(e).__name__, e)
            return row, reported
        if not alert_id and not row["alert_id"]:
            return row, reported
        if not self._store.set(row["id"], only=("open",), alert_id=alert_id or row["alert_id"],
                               alert_severity=row["severity"], last_reported=self._now()):
            return row, reported
        return self._store.get(row["id"]) or row, reported

    def _bump_streak(self, row: dict) -> dict:
        """Counts one more run in which this finding passed the alert gates."""
        n = int(row.get("gate_streak") or 0) + 1
        if not self._store.set(row["id"], only=("open",), gate_streak=n):
            return row
        return {**row, "gate_streak": n}

    def _drop_alert(self, row: dict) -> dict:
        """Forgets an open finding's run of passing gates and closes its alert; `last_reported` is kept, so a
        finding whose gates flip back and forth cannot re-post every run."""
        cols: dict = {}
        if int(row.get("gate_streak") or 0):
            cols["gate_streak"] = 0
        if row.get("alert_id") and self._close_alert(row):
            cols["alert_id"] = None
            cols["alert_severity"] = None
        if not cols or not self._store.set(row["id"], only=("open",), **cols):
            return row
        return self._store.get(row["id"]) or row

    def _close_alert(self, row: dict) -> bool:
        """True when the row carries no alert or its alert was closed; False leaves the id for the next run."""
        if not row.get("alert_id"):
            return True
        try:
            return bool(self._close(row["alert_id"]))
        except Exception as e:  # noqa: BLE001 — the finding is resolved either way
            log.warning("forecast alert close failed: %s: %s", type(e).__name__, e)
            return False

    def _resolve_alert(self, row: dict, statuses: tuple) -> None:
        """Closes a resolved finding's alert and forgets it; a failed close is retried next run."""
        if row.get("alert_id") and self._close_alert(row):
            self._store.set(row["id"], only=statuses, alert_id=None, alert_severity=None)

    def _retry_closes(self) -> int:
        """Second try at the alerts of findings that were cleared or dismissed while the alarm engine was down."""
        n = 0
        for row in self._store.unclosed():
            if self._close_alert(row) and self._store.set(row["id"], only=("cleared", "dismissed"),
                                                          alert_id=None, alert_severity=None):
                n += 1
        return n

    # ── tower ──
    def _effort(self, last: Optional[dict]) -> "tuple[Any, Optional[str], Optional[str]]":
        """The profile this run works at, its reason and the model it will use; the off profile whenever
        there is no eligible pass."""
        off = (forecast_effort.PROFILES["off"], None, None)
        if self._tower is None:
            return off
        try:
            if hasattr(self._tower, "eligible") and not self._tower.eligible():
                return off
            model = self._tower.model_id() if hasattr(self._tower, "model_id") else None
        except Exception as e:  # noqa: BLE001 — an unreadable Tower state means code only
            log.debug("forecast tower eligibility failed: %s", type(e).__name__)
            return off
        signals = self._model_signals(model)
        same = model is not None and str((last or {}).get("model") or "") == str(model)
        if same and (last or {}).get("tok_s"):
            signals["tok_s"] = (last or {}).get("tok_s")
        profile, reason = forecast_effort.pick(getattr(self._cfg(), "tower_effort", "auto"), signals,
                                               self._timeout_cap(model))
        if profile.tier == "off":
            return forecast_effort.PROFILES["off"], reason, model
        log.debug("forecast tower tier=%s", profile.tier)
        return profile, reason, model

    def _timeout_cap(self, model: Optional[str]) -> Optional[str]:
        """The tier that last timed out on this model, while the cap is still in force: it lifts after
        RESTORE_RUNS runs that actually called the model without timing out, and a model change clears it."""
        if not model:
            return None
        try:
            rows = self._store.recent_runs(RUN_SCAN)
        except Exception as e:  # noqa: BLE001 — an unreadable history means no cap
            log.debug("forecast run history read failed: %s", type(e).__name__)
            return None
        good = 0
        for row in rows:
            tier = str(row.get("tier") or "")
            if tier in ("", "off"):
                continue
            if str(row.get("model") or "") != str(model):
                return None
            if row.get("timed_out"):
                return None if good >= RESTORE_RUNS else tier
            # Only a run that actually called the model counts towards lifting the cap.
            if int(row.get("tower_calls") or 0) >= 1:
                good += 1
        return None

    def _model_signals(self, model: Optional[str]) -> dict:
        """The Tower model's measured quality; every key None when nothing can be read."""
        blank = {"score_pct": None, "size_b": None, "tool_grade": None, "tok_s": None}
        if self._signals is None or not model:
            return blank
        try:
            got = self._signals(str(model)) or {}
        except Exception as e:  # noqa: BLE001 — unreadable signals simply pick the lowest tier
            log.debug("forecast model signals failed: %s", type(e).__name__)
            return blank
        return {**blank, **{k: got.get(k) for k in blank}}

    def _begin_run(self, profile) -> None:
        """Pins the run's profile on the pass, so a mid-run settings change cannot split the two."""
        fn = getattr(self._tower, "begin_run", None) if self._tower is not None else None
        if fn is None:
            return
        try:
            fn(profile)
        except Exception as e:  # noqa: BLE001 — the echo filter is not worth a failed run
            log.debug("forecast tower begin_run failed: %s", type(e).__name__)

    def _tower_tok_s(self) -> Optional[float]:
        try:
            return self._tower.measured_tok_s() if hasattr(self._tower, "measured_tok_s") else None
        except Exception as e:  # noqa: BLE001 — a missing sample only costs the next tier hint
            log.debug("forecast tower speed read failed: %s", type(e).__name__)
            return None

    def _tower_calls(self) -> int:
        try:
            return int(self._tower.model_calls()) if hasattr(self._tower, "model_calls") else 0
        except Exception as e:  # noqa: BLE001 — an unreadable count only costs the next tier hint
            log.debug("forecast tower call count read failed: %s", type(e).__name__)
            return 0

    def _digest_discarded(self) -> bool:
        try:
            return bool(self._tower.digest_discarded()) if hasattr(self._tower, "digest_discarded") else False
        except Exception as e:  # noqa: BLE001 — an unreadable flag only hides the hint
            log.debug("forecast tower digest flag read failed: %s", type(e).__name__)
            return False

    def _tower_timed_out(self) -> bool:
        try:
            return bool(self._tower.timed_out()) if hasattr(self._tower, "timed_out") else False
        except Exception as e:  # noqa: BLE001 — an unreadable flag means no lowering next run
            log.debug("forecast tower timeout read failed: %s", type(e).__name__)
            return False

    def _clear_model_findings(self) -> int:
        """Clears every open finding only Tower saw; code and Explain runs never keep them."""
        now = self._now()
        n = 0
        for row in self._store.open():
            if str(row.get("verified") or "") != "model":
                continue
            if self._store.set(row["id"], only=("open",), status="cleared", resolved=now):
                n += 1
        if n:
            log.debug("forecast cleared %d model-only findings", n)
        return n

    def _titled(self, rows: "list[dict]") -> "list[dict]":
        return [{**r, "title": self._title(r["check_id"])} for r in rows]

    def _digest(self, touched: "list[dict]") -> Optional[str]:
        """One direct call tying the run's findings together; None when Tower wrote nothing usable."""
        fn = getattr(self._tower, "digest", None) if self._tower is not None else None
        if fn is None:
            return None
        try:
            return fn(self._titled(touched)) or None
        except Exception as e:  # noqa: BLE001 — the measured findings stand without Tower
            log.warning("forecast digest call failed: %s: %s", type(e).__name__, e)
            return None

    def _causes(self, touched: "list[dict]") -> int:
        """A likely cause appended to the detail of the findings Tower has not already explained."""
        fn = getattr(self._tower, "causes", None) if self._tower is not None else None
        if fn is None:
            return 0
        rows = [r for r in touched if str(r.get("verified") or "") != "tower+code"]
        if not rows:
            return 0
        try:
            out = fn(self._titled(rows)) or {}
        except Exception as e:  # noqa: BLE001 — the measured findings stand without Tower
            log.warning("forecast cause call failed: %s: %s", type(e).__name__, e)
            return 0
        written = 0
        for fid, text in out.items():
            cols = {k: v for k, v in (text or {}).items() if k == "detail"}
            if cols and self._store.set(str(fid), only=("open",), verified="tower+code", tower_note=None, **cols):
                written += 1
            elif not cols and (text or {}).get("tower_note"):
                self._store.set(str(fid), only=("open",), tower_note=str(text["tower_note"])[:40])
        log.debug("forecast causes written %d of %d findings", written, len(rows))
        return written

    def _clear_digests(self, keep: Optional[str] = None, week: Optional[str] = None) -> int:
        """Clears open weekly digest rows other than `keep`; with `week`, only those from another week."""
        now = self._now()
        n = 0
        for row in self._store.open():
            if str(row["check_id"]) != DIGEST_ID or row["fingerprint"] == keep:
                continue
            if week is not None and str(row["subject"] or "") == week:
                continue
            if self._store.set(row["id"], only=("open",), status="cleared", resolved=now):
                self._resolve_alert(row, ("cleared",))
                n += 1
        if n:
            log.debug("forecast cleared %d superseded weekly digest rows", n)
        return n

    def _tower_pass(self, check, data, rows: "list[dict]", thread_id) -> "Optional[list[tuple[dict, str]]]":
        if self._tower is None:
            return None
        try:
            return self._tower(check, data, rows, thread_id)
        except Exception as e:  # noqa: BLE001 — a Tower failure falls back to the code text
            log.warning("forecast tower pass for %s failed: %s: %s", check.id, type(e).__name__, e)
            return None

    def _start_thread(self, label: str) -> Optional[str]:
        fn = getattr(self._tower, "start_thread", None) if self._tower is not None else None
        if fn is None:
            return None
        try:
            return fn(label)
        except Exception as e:  # noqa: BLE001 — the run does not need a thread
            log.warning("forecast thread start failed: %s: %s", type(e).__name__, e)
            return None

    def _end_thread(self, thread_id: Optional[str]) -> bool:
        """Ends a check's thread; True while the conversation is kept and can be opened later."""
        fn = getattr(self._tower, "end_thread", None) if self._tower is not None else None
        if fn is None or thread_id is None:
            return False
        try:
            return bool(fn(thread_id))
        except Exception as e:  # noqa: BLE001 — the run is already recorded
            log.warning("forecast thread end failed: %s: %s", type(e).__name__, e)
            return False

    def _log(self, event: dict) -> None:
        if self._audit is None:
            return
        try:
            self._audit(event)
        except Exception as e:  # noqa: BLE001 — an audit failure never breaks a run
            log.warning("forecast audit failed: %s: %s", type(e).__name__, e)


def register_routes(app, forecast: Forecast, *, role_of: Callable[[], Optional[str]],
                    user_of: Callable[[], str]) -> None:
    """GET /api/forecast for any session; POST /api/forecast/run and /api/forecast/<fid>/dismiss for operators."""
    from flask import g, jsonify

    def _who() -> "tuple[Optional[str], str]":
        role = role_of()
        return role, ((user_of() or "") if role else "")

    @app.route("/api/forecast")
    def forecast_state():
        role, _ = _who()
        if role is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return jsonify({"ok": True, **forecast.view()})

    @app.route("/api/forecast/run", methods=["POST"])
    def forecast_run():
        role, user = _who()
        if role is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if role not in ("operator", "admin"):
            return jsonify({"ok": False, "error": "operator role required"}), 403
        try:
            row = forecast.run_now(user, role)
        except jobs.JobError as e:
            return jsonify({"ok": False, "error": e.message}), e.status
        g._audit_extra = {"job_id": row["id"], "kind": row["kind"]}
        return jsonify({"ok": True, "job_id": row["id"]}), 202

    @app.route("/api/forecast/<fid>/dismiss", methods=["POST"])
    def forecast_dismiss(fid):
        role, user = _who()
        if role is None:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        if role not in ("operator", "admin"):
            return jsonify({"ok": False, "error": "operator role required"}), 403
        if not forecast.dismiss(fid[:32], user):
            return jsonify({"ok": False, "error": "not found"}), 404
        g._audit_extra = {"finding": fid[:32]}
        return jsonify({"ok": True})
