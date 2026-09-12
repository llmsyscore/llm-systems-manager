"""Live benchmark history (#879): speed-bench runs per model/agent with a
pinned baseline; proxies to the primary llama agent."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

RESULT_CAP = 256 * 1024
KEEP_PER_MODEL = 50
FLEET_POLL_S = 3.0
FLEET_JOB_RETENTION = 16
FLEET_MAX_WAIT_S = 7200


def init_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bench_live_runs (
            id            INTEGER PRIMARY KEY,
            run_id        TEXT NOT NULL UNIQUE,
            model_id      TEXT NOT NULL,
            agent_id      TEXT NOT NULL DEFAULT '',
            ts            TEXT NOT NULL,
            ok            INTEGER NOT NULL DEFAULT 1,
            baseline      INTEGER NOT NULL DEFAULT 0,
            gen_tps       REAL, ppt_tps REAL, latency_s REAL, accept_rate REAL, wh_per_ktok REAL,
            config_json   TEXT NOT NULL,
            result_json   TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bench_live_model ON bench_live_runs(model_id, agent_id, id)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bench_live_runs)").fetchall()}
    if "llama_build" not in cols:
        conn.execute("ALTER TABLE bench_live_runs ADD COLUMN llama_build TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _finite(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _row(r) -> dict:
    return {"id": r[0], "run_id": r[1], "model_id": r[2], "agent_id": r[3], "ts": r[4],
            "ok": bool(r[5]), "baseline": bool(r[6]), "gen_tps": r[7], "ppt_tps": r[8],
            "latency_s": r[9], "accept_rate": r[10], "wh_per_ktok": r[11],
            "config": json.loads(r[12] or "{}"), "llama_build": r[13] or ""}


_COLS = ("id, run_id, model_id, agent_id, ts, ok, baseline, gen_tps, ppt_tps, latency_s, accept_rate, wh_per_ktok, "
         "config_json, llama_build")


def read_run(conn, run_id: str) -> "Optional[tuple[dict, dict]]":
    """Fetch one stored live run as (meta, parsed result doc), or None."""
    r = conn.execute(f"SELECT {_COLS}, result_json FROM bench_live_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not r:
        return None
    try:
        doc = json.loads(r[14])
    except ValueError:
        doc = {}
    return _row(r), doc


def latest_per_agent(conn, model_id: str) -> list[dict]:
    """Newest ok=1 run per agent for a model, sorted by gen_tps desc."""
    rows = conn.execute(
        f"SELECT {_COLS} FROM bench_live_runs WHERE model_id = ? AND ok = 1 ORDER BY id DESC", (model_id,)).fetchall()
    seen: dict = {}
    for r in rows:
        row = _row(r)
        seen.setdefault(row["agent_id"], row)
    out = list(seen.values())
    out.sort(key=lambda r: -(r.get("gen_tps") or 0))
    return out


def speed_table(db_path: str, model_id: str) -> list[dict]:
    """latest_per_agent over a short-lived connection; [] when the table is missing."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        return latest_per_agent(conn, model_id)
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def hosts_for(rows: list, model_id: str) -> list[dict]:
    """Fleet host rows for one model: loaded = online, model matches, not sleeping."""
    out = []
    for h in rows:
        loaded = bool(h.get("online")) and (h.get("model") == model_id) and (h.get("state") != "sleeping")
        out.append({"agent_id": h["agent_id"], "hostname": h.get("hostname") or h["agent_id"][:8],
                    "online": bool(h.get("online")), "loaded": loaded, "state": h.get("state")})
    out.sort(key=lambda r: (not r["loaded"], str(r["hostname"]).lower()))
    return out


def _host_row(agent_id: str, hostname: str) -> dict:
    return {"agent_id": agent_id, "hostname": hostname, "status": "queued", "error": None, "run_id": None,
            "started": None, "finished": None, "gen_tps": None, "agg_max_tps": None, "latency_s": None,
            "accept_rate": None, "wh_per_ktok": None, "bench": None, "matrix_ignored": False}


def _fill_from_run(host: dict, meta: dict, doc: dict, job: Optional[dict] = None) -> None:
    """Copy the stored run's headline metrics onto a fleet job host row."""
    aggs = [((l.get("all") or {}).get("agg_pred_tps")) for l in (doc.get("levels") or []) if isinstance(l, dict)]
    aggs = [a for a in aggs if isinstance(a, (int, float))]
    host.update({"gen_tps": meta.get("gen_tps"), "latency_s": meta.get("latency_s"), "accept_rate": meta.get("accept_rate"),
                 "wh_per_ktok": meta.get("wh_per_ktok"), "agg_max_tps": max(aggs) if aggs else None,
                 "bench": (meta.get("config") or {}).get("bench")})
    # Flags a host whose stale agent dropped the requested prompt x output matrix.
    if job and (job.get("config") or {}).get("matrix") and not (meta.get("config") or {}).get("matrix"):
        host["matrix_ignored"] = True


def register_routes(app, ctx, *, db_path: str, proxy: Callable, agent_by_token: Callable,
                    request_agent: Callable, note_tool_start: Callable,
                    fleet_hosts: Optional[Callable] = None, run_on_agent: Optional[Callable] = None,
                    cancel_on_agent: Optional[Callable] = None,
                    llama_build_of: Optional[Callable[[str], str]] = None) -> None:
    from flask import jsonify, request as flask_request

    tls = threading.local()
    lock = threading.Lock()

    def conn_factory():
        conn = getattr(tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(db_path, timeout=30.0)
            conn.execute("PRAGMA busy_timeout=5000")
            tls.conn = conn
        return conn

    init_table(conn_factory())

    jobs: dict = {}
    jobs_lock = threading.Lock()

    def _hosts_for(model_id: str) -> list[dict]:
        return hosts_for(fleet_hosts() if fleet_hosts else [], model_id)

    def _public_job(job: dict) -> dict:
        hosts = [dict(h) for h in job["hosts"]]
        done = [h for h in hosts if h["status"] == "done" and h.get("gen_tps") is not None]
        done.sort(key=lambda h: -h["gen_tps"])
        return {"job_id": job["job_id"], "model_id": job["model_id"], "ts": job["ts"], "done": job["done"],
                "cancelled": job["cancelled"], "config": job["config"], "hosts": hosts,
                "ranking": [h["agent_id"] for h in done]}

    def _start_host(job: dict, host: dict, body: dict) -> None:
        ok, val = run_on_agent(host["agent_id"], body)
        with jobs_lock:
            if job["cancelled"]:
                host["status"] = "cancelled"
                return
            if ok:
                host.update({"status": "running", "run_id": val, "started": time.time()})
            else:
                host.update({"status": "failed", "error": str(val)[:300], "finished": time.time()})

    def _fleet_start_all(job: dict) -> None:
        """Start every host in parallel and block until each has an initial status."""
        body = {**job["config"], "model_id": job["model_id"]}
        threads = [threading.Thread(target=_start_host, args=(job, h, body), daemon=True) for h in job["hosts"]]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _fleet_run(job: dict) -> None:
        """Background thread: start every host, then poll until each finishes."""
        _fleet_start_all(job)
        deadline = time.time() + FLEET_MAX_WAIT_S
        while time.time() < deadline:
            with jobs_lock:
                pending = [h for h in job["hosts"] if h["status"] == "running"]
                if not pending:
                    break
                if job["cancelled"]:
                    break
            for h in pending:
                with lock:
                    hit = read_run(conn_factory(), h["run_id"])
                if hit:
                    meta, doc = hit
                    with jobs_lock:
                        if h["status"] == "running":
                            _fill_from_run(h, meta, doc, job)
                            h["status"] = "done" if meta.get("ok") else "failed"
                            h["finished"] = time.time()
                            if not meta.get("ok"):
                                h["error"] = "run failed on the agent"
            time.sleep(FLEET_POLL_S)
        with jobs_lock:
            for h in job["hosts"]:
                if h["status"] == "running":
                    h.update({"status": "cancelled" if job["cancelled"] else "failed",
                              "error": None if job["cancelled"] else "timed out", "finished": time.time()})
            job["done"] = True

    @app.route("/api/benchmark/live/hosts")
    def bench_live_hosts():
        model_id = (flask_request.args.get("model_id") or "").strip()
        if not model_id:
            return jsonify({"ok": False, "error": "model_id required"}), 400
        return jsonify({"ok": True, "hosts": _hosts_for(model_id)})

    @app.route("/api/benchmark/live/fleet", methods=["POST"])
    def bench_live_fleet():
        if not (run_on_agent and fleet_hosts):
            return jsonify({"ok": False, "error": "fleet runs not available"}), 503
        body = flask_request.get_json(silent=True) or {}
        model_id = str(body.get("model_id") or "").strip()[:200]
        cfg = body.get("config") if isinstance(body.get("config"), dict) else {}
        if not model_id:
            return jsonify({"ok": False, "error": "model_id required"}), 400
        loaded = {h["agent_id"]: h for h in _hosts_for(model_id) if h["loaded"]}
        want = body.get("agents")
        if want is None:
            want = list(loaded)
        if not isinstance(want, list) or not want or any(a not in loaded for a in want):
            return jsonify({"ok": False, "error": "no host has this model loaded" if not loaded else "unknown or unloaded host"}), 400
        cfg = {k: v for k, v in cfg.items() if k != "model_id"}
        job = {"job_id": uuid.uuid4().hex[:12], "model_id": model_id, "ts": datetime.now(timezone.utc).isoformat(),
               "config": cfg, "done": False, "cancelled": False,
               "hosts": [_host_row(a, loaded[a]["hostname"]) for a in want]}
        with jobs_lock:
            jobs[job["job_id"]] = job
            for old in list(jobs)[:-FLEET_JOB_RETENTION]:
                if jobs[old]["done"]:
                    jobs.pop(old, None)
        threading.Thread(target=_fleet_run, args=(job,), daemon=True).start()
        return jsonify({"ok": True, "job_id": job["job_id"], "hosts": [dict(h) for h in job["hosts"]]})

    @app.route("/api/benchmark/live/fleet/<job_id>")
    def bench_live_fleet_get(job_id):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return jsonify({"ok": False, "error": "not found"}), 404
            return jsonify({"ok": True, "job": _public_job(job)})

    @app.route("/api/benchmark/live/fleet/<job_id>/cancel", methods=["POST"])
    def bench_live_fleet_cancel(job_id):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return jsonify({"ok": False, "error": "not found"}), 404
            job["cancelled"] = True
            running = [h for h in job["hosts"] if h["status"] == "running"]
            for h in job["hosts"]:
                if h["status"] == "queued":
                    h["status"] = "cancelled"
        out = []
        for h in running:
            try:
                if cancel_on_agent and cancel_on_agent(h["agent_id"]):
                    out.append(h["agent_id"])
            except Exception:
                continue  # an unreachable host ends via the poll timeout
        return jsonify({"ok": True, "cancelled": out})

    @app.route("/api/benchmark/live/speed")
    def bench_live_speed():
        model_id = (flask_request.args.get("model_id") or "").strip()
        if not model_id:
            return jsonify({"ok": False, "error": "model_id required"}), 400
        names = {h["agent_id"]: h.get("hostname") for h in (fleet_hosts() if fleet_hosts else [])}
        with lock:
            rows = latest_per_agent(conn_factory(), model_id)
        return jsonify({"ok": True, "model_id": model_id, "hosts": [
            {"agent_id": r["agent_id"], "hostname": names.get(r["agent_id"]) or r["agent_id"][:8], "run_id": r["run_id"],
             "ts": r["ts"], "bench": (r.get("config") or {}).get("bench"), "gen_tps": r["gen_tps"], "ppt_tps": r["ppt_tps"],
             "latency_s": r["latency_s"], "accept_rate": r["accept_rate"], "wh_per_ktok": r["wh_per_ktok"],
             "llama_build": r.get("llama_build") or ""} for r in rows]})

    @app.route("/api/benchmark/live/preflight")
    def bench_live_preflight():
        return proxy("llama", "GET", "/llama/bench/live/preflight", timeout=10)

    @app.route("/api/benchmark/live/setup", methods=["POST"])
    def bench_live_setup():
        body = flask_request.get_json(force=True) or {}
        return proxy("llama", "POST", "/llama/bench/live/setup", json=body, timeout=15,
                     on_target=note_tool_start("llama", "benchmark"))

    @app.route("/api/benchmark/live/run", methods=["POST"])
    def bench_live_run():
        body = flask_request.get_json(force=True) or {}
        return proxy("llama", "POST", "/llama/bench/live/run", json=body, timeout=15,
                     on_target=note_tool_start("llama", "benchmark"))

    @app.route("/api/benchmark/live/store", methods=["POST"])
    def bench_live_store():
        token = (flask_request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        agent = agent_by_token(token) if token else None
        if not agent:
            return jsonify({"ok": False, "error": "agent token required"}), 403
        raw = flask_request.get_data(cache=False) or b""
        if len(raw) > RESULT_CAP:
            return jsonify({"ok": False, "error": "result too large"}), 413
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return jsonify({"ok": False, "error": "invalid json"}), 400
        run_id = str((doc or {}).get("run_id") or "").strip()[:64]
        model_id = str((doc or {}).get("model_id") or "").strip()[:200]
        levels = doc.get("levels") if isinstance(doc, dict) else None
        if not run_id or not model_id or not isinstance(levels, list) or not levels:
            return jsonify({"ok": False, "error": "run_id, model_id, levels required"}), 400
        first = (levels[0].get("all") if isinstance(levels[0], dict) else None) or {}
        agent_id = agent.get("agent_id") or ""
        ts = datetime.now(timezone.utc).isoformat()
        # Old agents omit llama_build; the manager's last llama sample fills it.
        build = str(doc.get("llama_build") or "").strip()[:64]
        if not build and llama_build_of:
            try:
                build = str(llama_build_of(agent_id) or "")[:64]
            except Exception:
                build = ""
        with lock:
            conn = conn_factory()
            cur = conn.execute(
                "INSERT OR IGNORE INTO bench_live_runs (run_id, model_id, agent_id, ts, ok, baseline, gen_tps, ppt_tps,"
                " latency_s, accept_rate, wh_per_ktok, config_json, result_json, llama_build)"
                " VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?,?)",
                (run_id, model_id, agent_id, ts, 1 if doc.get("ok", True) else 0,
                 _finite(first.get("pred_tps")), _finite(first.get("prompt_tps")), _finite(first.get("latency_s")),
                 _finite(first.get("accept_rate")), _finite(doc.get("wh_per_ktok")),
                 json.dumps(doc.get("config") or {}), json.dumps(doc), build))
            conn.execute(
                "DELETE FROM bench_live_runs WHERE model_id = ? AND agent_id = ? AND baseline = 0 AND id NOT IN "
                "(SELECT id FROM bench_live_runs WHERE model_id = ? AND agent_id = ? ORDER BY id DESC LIMIT ?)",
                (model_id, agent_id, model_id, agent_id, KEEP_PER_MODEL))
            conn.commit()
        return jsonify({"ok": True, "stored": bool(cur.rowcount)})

    @app.route("/api/benchmark/live/runs")
    def bench_live_runs():
        model_id = (flask_request.args.get("model_id") or "").strip()
        agent_id = (flask_request.args.get("agent_id") or "").strip()
        if not agent_id:
            agent_id = ((request_agent("llama") or {}).get("agent_id") or "")
        scope = " AND model_id = ?" if model_id else ""
        args: list = [agent_id] + ([model_id] if model_id else [])
        q = (f"SELECT {_COLS} FROM bench_live_runs WHERE agent_id = ?{scope} AND (baseline = 1 OR id IN "
             f"(SELECT id FROM bench_live_runs WHERE agent_id = ?{scope} ORDER BY id DESC LIMIT ?)) ORDER BY id DESC")
        args = args + args + [KEEP_PER_MODEL]
        with lock:
            rows = conn_factory().execute(q, args).fetchall()
        return jsonify({"ok": True, "runs": [_row(r) for r in rows]})

    @app.route("/api/benchmark/live/runs", methods=["DELETE"])
    def bench_live_runs_clear():
        model_id = (flask_request.args.get("model_id") or "").strip()
        if not model_id:
            return jsonify({"ok": False, "error": "model_id required"}), 400
        agent_id = (flask_request.args.get("agent_id") or "").strip()
        if not agent_id:
            agent_id = ((request_agent("llama") or {}).get("agent_id") or "")
        with lock:
            conn = conn_factory()
            cur = conn.execute("DELETE FROM bench_live_runs WHERE model_id = ? AND agent_id = ?",
                               (model_id, agent_id))
            conn.commit()
        return jsonify({"ok": True, "deleted": cur.rowcount})

    @app.route("/api/benchmark/live/runs/<run_id>")
    def bench_live_run_get(run_id):
        with lock:
            hit = read_run(conn_factory(), run_id)
        if not hit:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "meta": hit[0], "run": hit[1]})

    @app.route("/api/benchmark/live/runs/<run_id>/baseline", methods=["POST"])
    def bench_live_run_baseline(run_id):
        with lock:
            conn = conn_factory()
            r = conn.execute("SELECT model_id, agent_id FROM bench_live_runs WHERE run_id = ?", (run_id,)).fetchone()
            if not r:
                return jsonify({"ok": False, "error": "not found"}), 404
            conn.execute("UPDATE bench_live_runs SET baseline = 0 WHERE model_id = ? AND agent_id = ?", (r[0], r[1]))
            conn.execute("UPDATE bench_live_runs SET baseline = 1 WHERE run_id = ?", (run_id,))
            conn.commit()
        return jsonify({"ok": True})

    @app.route("/api/benchmark/live/runs/<run_id>", methods=["DELETE"])
    def bench_live_run_delete(run_id):
        with lock:
            conn = conn_factory()
            cur = conn.execute("DELETE FROM bench_live_runs WHERE run_id = ?", (run_id,))
            conn.commit()
        if not cur.rowcount:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True})
