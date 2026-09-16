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
    assert r == {"ok": True, "jobs": [], "summary": {"queued": 0, "running": 0, "failed_24h": 0, "next_due": None}}
    row = env.svc.submit("closed", {}, user="bob", label="B")
    r = env.get("/api/jobs?status=all&limit=5").get_json()
    assert [j["label"] for j in r["jobs"]] == ["B"] and r["jobs"][0]["can_cancel"] is False
    d = env.get(f"/api/jobs/{row['id']}").get_json()
    assert d["ok"] and d["job"]["spec"] == {} and d["job"]["kind_title"] == "Closed kind"
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
