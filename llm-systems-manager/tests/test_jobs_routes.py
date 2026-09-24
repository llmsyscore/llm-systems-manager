"""#915 job routes: list/detail for any role, submit for operators of api kinds, cancel by owner or admin, health block."""
from __future__ import annotations

import sqlite3
import types

import pytest
from flask import Flask, session

import jobs


@pytest.fixture
def env():
    app = Flask(__name__)
    app.secret_key = "t"
    app.config["TESTING"] = True
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    jobs.init_table(conn)
    svc = jobs.Service(jobs.Store(lambda: conn), cfg=lambda: types.SimpleNamespace(workers=4, history_days=30),
                       now=lambda: 1000.0, inline=True)
    svc.register(jobs.Kind("open", "Open kind", run=lambda j: jobs.ok(), api=True,
                           validate=lambda spec, who: ((spec, None) if spec.get("x") else (None, "x is required"))))
    svc.register(jobs.Kind("closed", "Closed kind", run=lambda j: jobs.ok()))
    jobs.register_routes(app, svc, role_of=lambda: session.get("role"), user_of=lambda: session.get("user") or "")
    c = app.test_client()
    c.svc = svc
    return c


def _as(c, role, user="alice"):
    with c.session_transaction() as s:
        s["role"], s["user"] = role, user


def test_list_and_detail_need_a_session(env):
    assert env.get("/api/jobs").status_code == 401
    _as(env, "viewer")
    r = env.get("/api/jobs").get_json()
    assert r == {"ok": True, "jobs": [], "summary": {"queued": 0, "running": 0, "failed_24h": 0, "next_due": None}, "total": 0}
    r = env.get("/api/jobs?facets=1").get_json()
    assert r["kinds"] == [{"name": "open", "title": "Open kind"}, {"name": "closed", "title": "Closed kind"}] and r["users"] == []
    row = env.svc.submit("closed", {}, user="bob", label="B")
    r = env.get("/api/jobs?status=all&limit=5").get_json()
    assert [j["label"] for j in r["jobs"]] == ["B"] and r["jobs"][0]["can_cancel"] is False
    d = env.get(f"/api/jobs/{row['id']}").get_json()
    assert d["ok"] and d["job"]["spec"] == {} and d["job"]["kind_title"] == "Closed kind" and d["job"]["audit"] is None
    assert env.get("/api/jobs/nope").status_code == 404
    assert env.get("/api/jobs?status=bogus").status_code == 400


def test_submit_rules(env):
    _as(env, "viewer")
    assert env.post("/api/jobs", json={"kind": "open", "spec": {"x": 1}}).status_code == 403
    _as(env, "operator")
    r = env.post("/api/jobs", json={"kind": "closed", "spec": {}})
    assert r.status_code == 409 and r.get_json()["error"] == "kind not submittable"
    r = env.post("/api/jobs", json={"kind": "nope", "spec": {}})
    assert r.status_code == 404 and r.get_json()["error"] == "unknown kind"
    r = env.post("/api/jobs", json={"kind": "open", "spec": {}})
    assert r.status_code == 400 and r.get_json()["error"] == "x is required"
    r = env.post("/api/jobs", json={"kind": "open", "spec": {"x": 1}, "label": "hello", "not_before": 1500.0,
                                    "period_s": 60, "runs_left": 3})
    assert r.status_code == 200
    j = r.get_json()["job"]
    assert j["label"] == "hello" and j["source"] == "api" and j["user"] == "alice" and j["next_run"] == 1500.0
    assert j["can_cancel"] is True
    assert env.post("/api/jobs", json={"kind": "open", "spec": {"x": 1}, "not_before": "soon"}).status_code == 400


def test_cancel_by_owner_or_admin(env):
    mine = env.svc.submit("closed", {}, user="alice", label="mine")
    theirs = env.svc.submit("closed", {}, user="bob", label="theirs")
    _as(env, "operator")
    assert env.post(f"/api/jobs/{theirs['id']}/cancel").status_code == 403
    r = env.post(f"/api/jobs/{mine['id']}/cancel")
    assert r.status_code == 200 and r.get_json()["job"]["status"] == "cancelled"
    assert env.post(f"/api/jobs/{mine['id']}/cancel").status_code == 409
    _as(env, "admin", "root")
    assert env.post(f"/api/jobs/{theirs['id']}/cancel").get_json()["job"]["status"] == "cancelled"
    assert env.post("/api/jobs/nope/cancel").status_code == 404


def _failed(env, user="bob", label="F"):
    row = env.svc.submit("closed", {}, user=user, label=label)
    env.svc._store.update(row["id"], status="failed", resolved=990.0, message="boom")
    return row


def test_ack_marks_a_failed_job_seen_for_its_owner_or_an_admin(env):
    """#1044: ack needs a session, the owner or an admin, a failed row, and works once."""
    row = _failed(env)
    assert env.post(f"/api/jobs/{row['id']}/ack").status_code == 401
    _as(env, "viewer", "bob")
    assert env.post(f"/api/jobs/{row['id']}/ack").status_code == 403
    _as(env, "operator", "alice")
    assert env.post(f"/api/jobs/{row['id']}/ack").status_code == 403
    assert env.post("/api/jobs/nope/ack").status_code == 404
    live = env.svc.submit("closed", {}, user="alice", label="L")
    r = env.post(f"/api/jobs/{live['id']}/ack")
    assert r.status_code == 409 and r.get_json()["error"] == "job is queued"
    _as(env, "operator", "bob")
    before = env.get("/api/jobs?status=failed").get_json()
    assert before["summary"]["failed_24h"] == 1 and before["jobs"][0]["can_ack"] is True and before["jobs"][0]["acked"] is False
    r = env.post(f"/api/jobs/{row['id']}/ack")
    assert r.status_code == 200 and r.get_json()["job"]["acked"] is True and r.get_json()["job"]["can_ack"] is False
    r = env.post(f"/api/jobs/{row['id']}/ack")
    assert r.status_code == 409 and r.get_json()["error"] == "already acknowledged"
    after = env.get("/api/jobs?status=failed").get_json()
    assert after["summary"]["failed_24h"] == 0 and after["jobs"][0]["acked"] is True
    assert env.svc.failed_recent() == []
    _as(env, "admin", "root")
    other = _failed(env, user="carol", label="G")
    assert env.post(f"/api/jobs/{other['id']}/ack").status_code == 200


def test_ack_writes_an_audit_row_and_survives_an_older_table():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("""CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT, user TEXT, role TEXT, source TEXT,
        spec TEXT, state TEXT, status TEXT NOT NULL, not_before REAL, period_s REAL, runs_left INTEGER, next_run REAL, last_run REAL,
        started REAL, run_count INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, lease REAL, exclusive TEXT,
        result TEXT, message TEXT, thread_id TEXT, created REAL NOT NULL, resolved REAL);""")
    jobs.init_table(conn)
    assert "acked_at" in {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    audit = []
    svc = jobs.Service(jobs.Store(lambda: conn), cfg=lambda: types.SimpleNamespace(workers=4, history_days=30),
                       audit=audit.append, now=lambda: 1000.0, inline=True)
    svc.register(jobs.Kind("closed", "Closed kind", run=lambda j: jobs.ok()))
    row = svc.submit("closed", {}, user="bob", label="F")
    assert svc.ack(row["id"], actor="bob") is None
    svc._store.update(row["id"], status="failed", resolved=990.0, message="boom")
    out = svc.ack(row["id"], actor="bob")
    assert out["acked_at"] == 1000.0 and svc.ack(row["id"], actor="bob") is None
    assert [a["action"] for a in audit] == ["jobs.submit", "jobs.ack"] and audit[-1]["actor"] == "bob"


def test_list_pages_with_offset_and_total(env):
    for i in range(3):
        env.svc.submit("closed", {}, user="bob" if i else "carol", label=f"j{i}")
    _as(env, "viewer")
    r = env.get("/api/jobs?status=all&limit=2&offset=2&facets=1").get_json()
    assert r["total"] == 3 and len(r["jobs"]) == 1 and r["users"] == ["bob", "carol"]
    r = env.get("/api/jobs?status=all&user=bob&offset=junk").get_json()
    assert r["total"] == 2 and len(r["jobs"]) == 2


def test_detail_audit_rows_are_admin_only():
    app = Flask(__name__)
    app.secret_key = "t"
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    jobs.init_table(conn)
    svc = jobs.Service(jobs.Store(lambda: conn), cfg=lambda: types.SimpleNamespace(workers=4, history_days=30),
                       now=lambda: 1000.0, inline=True)
    svc.register(jobs.Kind("closed", "Closed kind", run=lambda j: jobs.ok()))

    def boom(jid):
        raise RuntimeError("audit db gone")
    audit = {"fn": lambda jid: [{"action": "jobs.cancel", "detail": {"job_id": jid}}]}
    jobs.register_routes(app, svc, role_of=lambda: session.get("role"), user_of=lambda: session.get("user") or "",
                         audit_for=lambda jid: audit["fn"](jid))
    c = app.test_client()
    row = svc.submit("closed", {}, user="bob", label="B")
    _as(c, "admin", "root")
    assert c.get(f"/api/jobs/{row['id']}").get_json()["job"]["audit"] == [{"action": "jobs.cancel", "detail": {"job_id": row["id"]}}]
    _as(c, "operator", "bob")
    assert c.get(f"/api/jobs/{row['id']}").get_json()["job"]["audit"] is None
    audit["fn"] = boom
    _as(c, "admin", "root")
    r = c.get(f"/api/jobs/{row['id']}")
    assert r.status_code == 200 and r.get_json()["job"]["audit"] is None
