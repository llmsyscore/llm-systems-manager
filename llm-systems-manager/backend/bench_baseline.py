"""Pinned Live-benchmark baselines (#882): scheduled re-check on the same host
and an alarm-engine alert when decode t/s regresses."""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import bench_live

CHECK_POLL_S = 30.0
CHECK_MAX_WAIT_S = 7200.0
RETRY_S = 1800.0
MAX_ATTEMPTS = 3
MAX_BUSY = 12
AUTO_TRIGGERS = ("nightly", "build")

_log = logging.getLogger("bench_baseline")


def init_tables(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bench_baseline_checks (
            id              INTEGER PRIMARY KEY,
            baseline_run_id TEXT NOT NULL,
            run_id          TEXT,
            model_id        TEXT NOT NULL,
            agent_id        TEXT NOT NULL,
            ts              TEXT NOT NULL,
            trigger         TEXT NOT NULL,
            status          TEXT NOT NULL,
            gen_tps         REAL, base_tps REAL, delta_pct REAL,
            severity        TEXT, error TEXT, llama_build TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bench_baseline_checks ON bench_baseline_checks(baseline_run_id, id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bench_baseline_agents (
            agent_id    TEXT PRIMARY KEY,
            llama_build TEXT NOT NULL DEFAULT '',
            seen_ts     REAL NOT NULL DEFAULT 0
        )""")
    conn.execute("CREATE TABLE IF NOT EXISTS bench_baseline_state (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
    conn.commit()


def parse_hhmm(s: str) -> Optional[tuple[int, int]]:
    parts = str(s or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def slot_ts(now: float, hhmm: str, tz=None) -> Optional[float]:
    """Epoch of today's HH:MM (local unless tz); None when unparsable."""
    hm = parse_hhmm(hhmm)
    if hm is None:
        return None
    d = datetime.fromtimestamp(now, tz)
    return d.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0).timestamp()


def next_slot_ts(now: float, hhmm: str, tz=None) -> Optional[float]:
    s = slot_ts(now, hhmm, tz)
    if s is None:
        return None
    if s > now:
        return s
    d = datetime.fromtimestamp(s, tz) + timedelta(days=1)
    return d.timestamp()


def nightly_due(now: float, hhmm: str, last_auto_ts: Optional[float], tz=None, since: Optional[float] = None) -> bool:
    """True once today's slot has passed, was not yet checked, and is not older than the schedule."""
    s = slot_ts(now, hhmm, tz)
    if s is None or now < s or (since is not None and s < since):
        return False
    return last_auto_ts is None or last_auto_ts < s


def delta_pct(cur: Any, base: Any) -> Optional[float]:
    if not isinstance(cur, (int, float)) or not isinstance(base, (int, float)) or isinstance(cur, bool) or not base:
        return None
    return round((float(cur) - float(base)) / float(base) * 100.0, 1)


def severity(delta: Optional[float], pct: float) -> Optional[str]:
    if delta is None or pct <= 0:
        return None
    drop = -delta
    if drop >= 2 * pct:
        return "critical"
    return "warning" if drop >= pct else None


def _metric_tag(model_id: str) -> str:
    """Model id's last path segment, sanitised to [A-Za-z0-9_.:-], capped at 64 chars."""
    tail = str(model_id or "").rsplit("/", 1)[-1]
    tag = "".join(c if re.match(r"[A-Za-z0-9_.:-]", c) else "_" for c in tail)
    return tag[:64]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _parse_iso(s: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


_CHECK_COLS = "id, baseline_run_id, run_id, model_id, agent_id, ts, trigger, status, gen_tps, base_tps, delta_pct, severity, error, llama_build"


def _check_row(r) -> dict:
    return {"id": r[0], "baseline_run_id": r[1], "run_id": r[2], "model_id": r[3], "agent_id": r[4], "ts": r[5],
            "trigger": r[6], "status": r[7], "gen_tps": r[8], "base_tps": r[9], "delta_pct": r[10],
            "severity": r[11], "error": r[12], "llama_build": r[13]}


class Watcher:
    def __init__(self, *, db_path: str, cfg: Callable[[], dict], fleet_hosts: Callable[[], list],
                 run_on_agent: Callable[[str, dict], tuple], llama_build_of: Callable[[str], str],
                 alert: Callable[[dict], bool], push_metrics: Callable[[list], None],
                 now: Callable[[], float] = time.time, tz=None, log=None) -> None:
        self._db_path = db_path
        self._cfg, self._hosts, self._run, self._build_of = cfg, fleet_hosts, run_on_agent, llama_build_of
        self._alert, self._push, self._now, self._tz = alert, push_metrics, now, tz
        self._log = log or _log
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._active: dict[str, dict] = {}      # baseline run_id -> {run_id, started, trigger, build}
        self._pending: dict[str, dict] = {}     # baseline run_id -> {trigger, attempts, retry_at, build_from}
        with self._lock:
            init_tables(self._conn())

    def _conn(self):
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30.0)
            bench_live.init_table(conn)
            self._tls.conn = conn
        return conn

    # ── reads ──────────────────────────────────────────────────────────
    def _pinned(self) -> list[dict]:
        rows = self._conn().execute(
            f"SELECT {bench_live._COLS} FROM bench_live_runs WHERE baseline = 1 ORDER BY model_id, agent_id").fetchall()
        return [bench_live._row(r) for r in rows]

    def _last_check(self, baseline_run_id: str, auto_only: bool = False, settled_only: bool = False) -> Optional[dict]:
        q = f"SELECT {_CHECK_COLS} FROM bench_baseline_checks WHERE baseline_run_id = ?"
        args: list = [baseline_run_id]
        if auto_only:
            q += " AND trigger IN (?, ?)"
            args += list(AUTO_TRIGGERS)
        if settled_only:
            q += " AND status IN ('ok', 'regressed')"
        r = self._conn().execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return _check_row(r) if r else None

    def history(self, run_id: str, limit: int = 20) -> list[dict]:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        with self._lock:
            rows = self._conn().execute(
                f"SELECT {_CHECK_COLS} FROM bench_baseline_checks WHERE baseline_run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, max(1, min(limit, 200)))).fetchall()
        return [_check_row(r) for r in rows]

    def _record(self, b: dict, trigger: str, status: str, *, run_id=None, gen_tps=None, delta=None, sev=None,
                error=None, build="") -> dict:
        row = {"baseline_run_id": b["run_id"], "run_id": run_id, "model_id": b["model_id"], "agent_id": b["agent_id"],
               "ts": _iso(self._now()), "trigger": trigger, "status": status, "gen_tps": gen_tps,
               "base_tps": b.get("gen_tps"), "delta_pct": delta, "severity": sev, "error": error, "llama_build": build}
        self._conn().execute(
            "INSERT INTO bench_baseline_checks (baseline_run_id, run_id, model_id, agent_id, ts, trigger, status,"
            " gen_tps, base_tps, delta_pct, severity, error, llama_build) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["baseline_run_id"], row["run_id"], row["model_id"], row["agent_id"], row["ts"], row["trigger"],
             row["status"], row["gen_tps"], row["base_tps"], row["delta_pct"], row["severity"], row["error"], row["llama_build"]))
        self._conn().execute(
            "DELETE FROM bench_baseline_checks WHERE baseline_run_id = ? AND id NOT IN"
            " (SELECT id FROM bench_baseline_checks WHERE baseline_run_id = ? ORDER BY id DESC LIMIT 200)",
            (row["baseline_run_id"], row["baseline_run_id"]))
        self._conn().commit()
        return row

    # ── public ─────────────────────────────────────────────────────────
    def recheck(self, run_id: Optional[str] = None) -> dict:
        with self._lock:
            pins = {b["run_id"]: b for b in self._pinned()}
            want = [run_id] if run_id else list(pins)
            queued, skipped = [], []
            for rid in want:
                if rid not in pins:
                    skipped.append({"run_id": rid, "reason": "not a pinned baseline"})
                elif rid in self._active or self._pending.get(rid, {}).get("starting"):
                    skipped.append({"run_id": rid, "reason": "check already running"})
                else:
                    self._pending[rid] = {"trigger": "manual", "attempts": 0, "retry_at": 0.0, "build_from": ""}
                    queued.append(rid)
            return {"ok": True, "queued": queued, "skipped": skipped}

    def snapshot(self) -> dict:
        cfg = self._cfg()
        with self._lock:
            now = self._now()
            hosts = {h["agent_id"]: h for h in self._hosts()}
            pins = self._pinned()
            builds = {aid: (self._build_of(aid) or "") for aid in {b["agent_id"] for b in pins}}
            out = []
            for b in pins:
                model_hosts = {h["agent_id"]: h for h in bench_live.hosts_for(list(hosts.values()), b["model_id"])}
                h = model_hosts.get(b["agent_id"]) or {}
                build = builds.get(b["agent_id"], "")
                last = self._last_check(b["run_id"])
                settled = self._last_check(b["run_id"], settled_only=True)
                pend = self._pending.get(b["run_id"]) or {}
                act = self._active.get(b["run_id"])
                out.append({"run_id": b["run_id"], "model_id": b["model_id"], "agent_id": b["agent_id"],
                            "hostname": h.get("hostname") or b["agent_id"][:8], "ts": b["ts"], "gen_tps": b["gen_tps"],
                            "config": dict(b.get("config") or {}),
                            "online": bool(h.get("online")), "loaded": bool(h.get("loaded")),
                            "llama_build": build,
                            "build_changed": bool(build and settled and settled.get("llama_build")
                                                   and settled["llama_build"] != build),
                            "running": act is not None,
                            "active_run_id": act["run_id"] if act else None,
                            "pending": (act or pend).get("trigger") if (act or pend) else None,
                            "last_check": last})
            hhmm = str(cfg.get("nightly_at") or "")
            sched = {"enabled": bool(cfg.get("enabled")), "nightly_at": hhmm,
                     "on_build_change": bool(cfg.get("on_build_change")),
                     "regression_pct": float(cfg.get("regression_pct") or 0),
                     "nightly_valid": parse_hhmm(hhmm) is not None,
                     "next_nightly_ts": next_slot_ts(now, hhmm, self._tz) if cfg.get("enabled") else None}
            return {"baselines": out, "schedule": sched}

    def tick(self) -> None:
        try:
            self._tick_locked()
        except Exception as e:  # noqa: BLE001 - a tick failure must not kill the scheduler loop
            self._log.warning("bench baseline tick failed: %s", e)

    def _tick_locked(self) -> None:
        cfg = self._cfg()
        to_start: list = []
        with self._lock:
            now = self._now()
            self._poll_active(cfg, now)
            pins = self._pinned()
            self._detect_build_changes(cfg, pins, now)
            since = self._schedule_since(cfg, now)
            hosts = self._hosts()
            for b in pins:
                if b["run_id"] in self._active:
                    continue
                pend = self._pending.get(b["run_id"])
                if pend is None:
                    if not (cfg.get("enabled") and nightly_due(now, str(cfg.get("nightly_at") or ""),
                                                               self._last_auto_ts(b["run_id"]), self._tz, since)):
                        continue
                    pend = {"trigger": "nightly", "attempts": 0, "retry_at": 0.0, "build_from": ""}
                    self._pending[b["run_id"]] = pend
                if pend.get("starting") or pend["retry_at"] > now:
                    continue
                item = self._prep_start(b, pend, hosts)
                if item is not None:
                    pend["starting"] = True
                    to_start.append(item)
            live = {b["run_id"] for b in pins}
            for rid in [r for r in list(self._pending) if r not in live]:
                self._pending.pop(rid, None)
        # HTTP calls to agents happen here, outside the lock.
        for b, pend, body, build in to_start:
            self._launch(b, pend, body, build, now)

    # ── internals ──────────────────────────────────────────────────────
    def _schedule_since(self, cfg: dict, now: float) -> Optional[float]:
        """Epoch when the current (enabled, nightly_at) schedule was armed; None while disabled."""
        conn = self._conn()
        if not cfg.get("enabled"):
            conn.execute("DELETE FROM bench_baseline_state WHERE key IN ('schedule_sig', 'schedule_since')")
            conn.commit()
            return None
        sig = f"{cfg.get('nightly_at') or ''}"
        rows = dict(conn.execute("SELECT key, value FROM bench_baseline_state WHERE key IN ('schedule_sig', 'schedule_since')").fetchall())
        if rows.get("schedule_sig") == sig and rows.get("schedule_since"):
            try:
                return float(rows["schedule_since"])
            except ValueError:
                pass
        conn.execute("INSERT OR REPLACE INTO bench_baseline_state (key, value) VALUES ('schedule_sig', ?), ('schedule_since', ?)", (sig, repr(now)))
        conn.commit()
        return now

    def _last_auto_ts(self, run_id: str) -> Optional[float]:
        last = self._last_check(run_id, auto_only=True)
        return _parse_iso(last["ts"]) if last else None

    def _detect_build_changes(self, cfg: dict, pins: list, now: float) -> None:
        conn = self._conn()
        for aid in sorted({b["agent_id"] for b in pins}):
            build = self._build_of(aid) or ""
            if not build:
                continue
            r = conn.execute("SELECT llama_build FROM bench_baseline_agents WHERE agent_id = ?", (aid,)).fetchone()
            prev = r[0] if r else None
            if prev == build:
                continue
            conn.execute("INSERT INTO bench_baseline_agents (agent_id, llama_build, seen_ts) VALUES (?,?,?)"
                         " ON CONFLICT(agent_id) DO UPDATE SET llama_build = excluded.llama_build, seen_ts = excluded.seen_ts",
                         (aid, build, now))
            # setdefault keeps an existing pending trigger; queues even for an active baseline.
            if prev and cfg.get("enabled") and cfg.get("on_build_change"):
                for b in pins:
                    if b["agent_id"] == aid:
                        self._pending.setdefault(b["run_id"], {"trigger": "build", "attempts": 0, "retry_at": 0.0, "build_from": prev})
        conn.commit()

    def _prep_start(self, b: dict, pend: dict, hosts: list) -> "Optional[tuple[dict, dict, dict, str]]":
        """Under the lock: resolve the host + build body, or record a skip. No HTTP call here."""
        h = {x["agent_id"]: x for x in bench_live.hosts_for(hosts, b["model_id"])}.get(b["agent_id"]) or {}
        build = self._build_of(b["agent_id"]) or ""
        if not h.get("online") or not h.get("loaded"):
            self._record(b, pend["trigger"], "skipped", error="host offline" if not h.get("online") else "model not loaded on the host", build=build)
            self._pending.pop(b["run_id"], None)
            return None
        if not (b.get("config") or {}).get("bench"):
            self._record(b, pend["trigger"], "skipped", error="baseline has no stored config", build=build)
            self._pending.pop(b["run_id"], None)
            return None
        body = {k: v for k, v in (b.get("config") or {}).items() if k != "model_id"}
        body.update({"model_id": b["model_id"], "baseline_run_id": b["run_id"]})
        return b, pend, body, build

    def _launch(self, b: dict, pend: dict, body: dict, build: str, started: float) -> None:
        """Outside the lock: the actual (slow) agent call, then apply the outcome under the lock."""
        try:
            ok, val = self._run(b["agent_id"], body)
        except Exception as e:  # noqa: BLE001 - a host error must not stop the watcher
            ok, val = False, str(e)
        with self._lock:
            if self._pending.get(b["run_id"]) is not pend:
                return  # baseline unpinned, or its pending entry was replaced meanwhile
            if ok:
                self._active[b["run_id"]] = {"run_id": str(val), "started": started, "trigger": pend["trigger"],
                                             "build": build, "build_from": pend.get("build_from") or ""}
                self._pending.pop(b["run_id"], None)
                return
            pend["starting"] = False
            if "in progress" in str(val):
                pend["busy"] = pend.get("busy", 0) + 1
                if pend["busy"] >= MAX_BUSY:
                    self._record(b, pend["trigger"], "skipped", error="host busy all day", build=build)
                    self._pending.pop(b["run_id"], None)
                else:
                    pend["retry_at"] = started + RETRY_S
                return
            pend["attempts"] += 1
            if pend["attempts"] >= MAX_ATTEMPTS:
                self._record(b, pend["trigger"], "failed", error=str(val)[:300], build=build)
                self._pending.pop(b["run_id"], None)
            else:
                pend["retry_at"] = started + RETRY_S

    def _poll_active(self, cfg: dict, now: float) -> None:
        pins = {b["run_id"]: b for b in self._pinned()}
        for rid, act in list(self._active.items()):
            b = pins.get(rid)
            if b is None:
                self._active.pop(rid, None)
                continue
            hit = bench_live.read_run(self._conn(), act["run_id"])
            if hit is None:
                if now - act["started"] > CHECK_MAX_WAIT_S:
                    self._record(b, act["trigger"], "failed", run_id=act["run_id"], error="timed out", build=act["build"])
                    self._active.pop(rid, None)
                continue
            meta, _doc = hit
            self._active.pop(rid, None)
            self._finish(cfg, b, act, meta)

    def _finish(self, cfg: dict, b: dict, act: dict, meta: dict) -> None:
        cur = meta.get("gen_tps") if meta.get("ok") else None
        d = delta_pct(cur, b.get("gen_tps"))
        pct = float(cfg.get("regression_pct") or 0)
        sev = severity(d, pct)
        status = "regressed" if sev else ("ok" if cur is not None else "failed")
        hostname = ({x["agent_id"]: x for x in self._hosts()}.get(b["agent_id"]) or {}).get("hostname") or b["agent_id"][:8]
        row = self._record(b, act["trigger"], status, run_id=act["run_id"], gen_tps=cur, delta=d, sev=sev,
                           error=None if cur is not None else "run failed on the agent", build=act["build"])
        tag = _metric_tag(b["model_id"])
        metric = f"decode_tps:{tag}"
        if cur is not None:
            ts = _iso(self._now())
            pts = [{"source": "benchmark", "metric_name": metric, "value": float(cur), "unit": "t/s",
                    "timestamp": ts, "hostname": hostname}]
            if d is not None:
                pts.append({"source": "benchmark", "metric_name": f"baseline_delta_pct:{tag}", "value": float(d),
                            "unit": "%", "timestamp": ts, "hostname": hostname})
            try:
                self._push(pts)
            except Exception as e:  # noqa: BLE001
                self._log.warning("bench baseline metric push failed: %s", e)
        if sev:
            base = float(b.get("gen_tps") or 0)
            msg = f"{b['model_id']} on {hostname}: decode {cur:.1f} t/s, {d:+.0f} % vs baseline {base:.1f} t/s"
            if act.get("build_from") and act.get("build"):
                msg += f" (llama.cpp {act['build_from']} → {act['build']})"
            payload = {"name": "Benchmark regression", "source": "benchmark", "metric": metric,
                       "host": hostname, "severity": sev, "value": round(float(cur), 2),
                       "threshold": round(base * (1 - pct / 100.0), 2), "message": msg}
            try:
                self._alert(payload)
            except Exception as e:  # noqa: BLE001
                self._log.warning("bench baseline alert failed: %s", e)
        self._log.info("bench baseline %s: %s on %s %s (%s)", act["trigger"], b["model_id"], hostname, status,
                       row.get("delta_pct"))


def register_routes(app, watcher, primary_agent: Optional[Callable[[str], Optional[dict]]] = None) -> None:
    from flask import jsonify, request as flask_request

    @app.route("/api/benchmark/live/baselines")
    def bench_baselines_list():
        extra = {}
        if primary_agent is not None:
            extra["primary_agent_id"] = (primary_agent("llama") or {}).get("agent_id") or ""
        return jsonify({"ok": True, **watcher.snapshot(), **extra})

    @app.route("/api/benchmark/live/baselines/recheck", methods=["POST"])
    def bench_baselines_recheck():
        body = flask_request.get_json(silent=True) or {}
        rid = body.get("run_id")
        if rid is not None and not isinstance(rid, str):
            return jsonify({"ok": False, "error": "run_id must be a string"}), 400
        return jsonify(watcher.recheck((rid or "").strip()[:64] or None))

    @app.route("/api/benchmark/live/baselines/checks")
    def bench_baselines_checks():
        rid = (flask_request.args.get("run_id") or "").strip()[:64]
        if not rid:
            return jsonify({"ok": False, "error": "run_id required"}), 400
        try:
            limit = int(flask_request.args.get("limit") or 20)
        except ValueError:
            limit = 20
        return jsonify({"ok": True, "checks": watcher.history(rid, limit)})


def start_thread(watcher, shutting_down: Callable[[], bool]):
    """Daemon loop ticking the watcher every CHECK_POLL_S; None under pytest."""
    import sys
    if "pytest" in sys.modules:
        return None

    def _loop():
        while not shutting_down():
            try:
                watcher.tick()
            except Exception as e:  # noqa: BLE001
                _log.warning("bench baseline tick failed: %s", e)
            time.sleep(CHECK_POLL_S)

    t = threading.Thread(target=_loop, name="bench-baselines", daemon=True)
    t.start()
    return t
