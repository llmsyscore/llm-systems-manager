"""Live benchmark history (#879): speed-bench runs per model/agent with a
pinned baseline; proxies to the primary llama agent."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

RESULT_CAP = 256 * 1024
KEEP_PER_MODEL = 50


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
            "config": json.loads(r[12] or "{}")}


_COLS = "id, run_id, model_id, agent_id, ts, ok, baseline, gen_tps, ppt_tps, latency_s, accept_rate, wh_per_ktok, config_json"


def register_routes(app, ctx, *, db_path: str, proxy: Callable, agent_by_token: Callable,
                    request_agent: Callable, note_tool_start: Callable) -> None:
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
        with lock:
            conn = conn_factory()
            cur = conn.execute(
                "INSERT OR IGNORE INTO bench_live_runs (run_id, model_id, agent_id, ts, ok, baseline, gen_tps, ppt_tps,"
                " latency_s, accept_rate, wh_per_ktok, config_json, result_json) VALUES (?,?,?,?,?,0,?,?,?,?,?,?,?)",
                (run_id, model_id, agent_id, ts, 1 if doc.get("ok", True) else 0,
                 _finite(first.get("pred_tps")), _finite(first.get("prompt_tps")), _finite(first.get("latency_s")),
                 _finite(first.get("accept_rate")), _finite(doc.get("wh_per_ktok")),
                 json.dumps(doc.get("config") or {}), json.dumps(doc)))
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
            r = conn_factory().execute(
                f"SELECT {_COLS}, result_json FROM bench_live_runs WHERE run_id = ?", (run_id,)).fetchone()
        if not r:
            return jsonify({"ok": False, "error": "not found"}), 404
        meta = _row(r)
        try:
            run = json.loads(r[13])
        except ValueError:
            run = {}
        return jsonify({"ok": True, "meta": meta, "run": run})

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
