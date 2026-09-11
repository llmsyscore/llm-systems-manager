"""#891: batch route contract with injected deps (runner threads are off under pytest)."""
from __future__ import annotations

import sqlite3

import pytest

import autotune_batch as ab

A1, A2 = "a" * 32, "b" * 32
NOW = 1_800_000_000.0


@pytest.fixture
def env(tmp_path):
    from flask import Flask
    app = Flask(__name__)
    world = {"models": {A1: ["org/m:Q4", "org/n:Q8"], A2: None}, "cancelled": []}
    deps = ab.Deps(
        hosts=lambda: [{"agent_id": A1, "hostname": "alpha", "online": True},
                       {"agent_id": A2, "hostname": "bravo", "online": False}],
        busy_agents=lambda: {A2},
        preflight=lambda aid: None, run_on_agent=lambda aid, body: (True, "r"),
        stream_on_agent=lambda aid, last_id=None: iter(()), cancel_on_agent=lambda aid: world["cancelled"].append(aid) or True,
        stop_server=lambda aid: (True, None), restart_server=lambda aid: (True, None),
        read_config=lambda aid: {}, write_config=lambda aid, cfg: (True, None),
        active_profile=lambda aid, mid: None, save_profile=lambda *a: None, alert=lambda p: True,
        now=lambda: NOW)
    runner = ab.register_routes(app, None, db_path=str(tmp_path / "t.db"), deps=deps,
                                models_for=lambda aid: world["models"].get(aid), now=lambda: NOW)
    c = app.test_client()
    c.runner, c.world = runner, world
    return c


def _body(**kw):
    b = {"items": [{"agent_id": A1, "model_id": "org/m:Q4"}], "objective": "balanced",
         "dims": {"context": {"on": True}}, "budget_min": 480}
    b.update(kw)
    return b


def test_hosts_lists_models_online_and_busy(env):
    r = env.get("/api/llm/autotune/batch-hosts").get_json()
    assert r["ok"]
    alpha, bravo = r["hosts"]
    assert alpha == {"agent_id": A1, "hostname": "alpha", "online": True, "busy": False, "models": ["org/m:Q4", "org/n:Q8"]}
    assert bravo == {"agent_id": A2, "hostname": "bravo", "online": False, "busy": True, "models": []}


def test_start_get_list_and_409_while_active(env):
    r = env.post("/api/llm/autotune/batch", json=_body())
    assert r.status_code == 200 and r.get_json()["ok"]
    b = r.get_json()["batch"]
    assert b["status"] == "queued" and b["items"][0]["hostname"] == "alpha" and b["start_at"] == NOW
    assert env.get(f"/api/llm/autotune/batch/{b['id']}").get_json()["batch"]["id"] == b["id"]
    lst = env.get("/api/llm/autotune/batches?limit=5").get_json()
    assert lst["ok"] and [x["id"] for x in lst["batches"]] == [b["id"]]
    r2 = env.post("/api/llm/autotune/batch", json=_body())
    assert r2.status_code == 409 and "already" in r2.get_json()["error"]


def test_host_list_failure_does_not_leak_the_exception(tmp_path):
    from flask import Flask
    app = Flask(__name__)

    def boom():
        raise RuntimeError("secret path /opt/whatever exploded")
    deps = ab.Deps(hosts=boom, busy_agents=set, preflight=lambda a: None, run_on_agent=lambda a, b: (False, "x"),
                   stream_on_agent=lambda a, last=None: iter(()), cancel_on_agent=lambda a: True,
                   stop_server=lambda a: (True, None), restart_server=lambda a: (True, None),
                   read_config=lambda a: {}, write_config=lambda a, c: (True, None),
                   active_profile=lambda a, m: None, save_profile=lambda *a: None, alert=lambda p: True,
                   now=lambda: NOW)
    ab.register_routes(app, None, db_path=str(tmp_path / "t.db"), deps=deps, models_for=lambda a: [], now=lambda: NOW)
    r = app.test_client().get("/api/llm/autotune/batch-hosts")
    assert r.status_code == 502 and r.get_json()["error"] == "host list unavailable"
    assert "secret" not in r.get_data(as_text=True)


def test_validation_errors_are_fixed_strings(env):
    r = env.post("/api/llm/autotune/batch", json=_body(budget_min=4))
    assert r.status_code == 400
    assert r.get_json()["error"] == f"budget_min must be {ab.BUDGET_RANGE[0]}\u2013{ab.BUDGET_RANGE[1]}"


def test_start_rejects_bad_bodies(env):
    r = env.post("/api/llm/autotune/batch", json=_body(items=[{"agent_id": "zz", "model_id": "m"}]))
    assert r.status_code == 400 and "approved llama host" in r.get_json()["error"]
    r = env.post("/api/llm/autotune/batch", json=_body(budget_min=2))
    assert r.status_code == 400


def test_get_unknown_is_404(env):
    assert env.get("/api/llm/autotune/batch/nope").status_code == 404
    assert env.post("/api/llm/autotune/batch/nope/cancel").status_code == 404


def test_cancel_queued_batch_ends_it(env):
    b = env.post("/api/llm/autotune/batch", json=_body()).get_json()["batch"]
    r = env.post(f"/api/llm/autotune/batch/{b['id']}/cancel").get_json()
    assert r == {"ok": True, "status": "cancelled"}
    assert env.get(f"/api/llm/autotune/batch/{b['id']}").get_json()["batch"]["status"] == "cancelled"
    # A finished batch cannot be cancelled again.
    assert env.post(f"/api/llm/autotune/batch/{b['id']}/cancel").status_code == 409


def test_list_limit_is_clamped(env):
    for _ in range(3):
        b = env.post("/api/llm/autotune/batch", json=_body()).get_json()["batch"]
        env.post(f"/api/llm/autotune/batch/{b['id']}/cancel")
    assert len(env.get("/api/llm/autotune/batches?limit=2").get_json()["batches"]) == 2
    assert len(env.get("/api/llm/autotune/batches?limit=999").get_json()["batches"]) == 3
    assert len(env.get("/api/llm/autotune/batches?limit=x").get_json()["batches"]) == 3


def test_register_recovers_rows_left_by_a_restart(tmp_path):
    from flask import Flask
    conn = sqlite3.connect(tmp_path / "t.db")
    ab.init_table(conn)
    store = ab.Store(lambda: conn)
    running = ab.new_batch(ab.validate_body(_body(), {A1}, NOW), {A1: "alpha"}, NOW)
    running["status"] = "running"; running["items"][0]["status"] = "running"
    store.save(running)
    conn.close()
    deps = ab.Deps(hosts=lambda: [], busy_agents=set, preflight=lambda a: None, run_on_agent=lambda a, b: (False, "x"),
                   stream_on_agent=lambda a, last_id=None: iter(()), cancel_on_agent=lambda a: True, stop_server=lambda a: (True, None),
                   restart_server=lambda a: (True, None), read_config=lambda a: {}, write_config=lambda a, c: (True, None),
                   active_profile=lambda a, m: None, save_profile=lambda *a: None, alert=lambda p: True, now=lambda: NOW)
    app = Flask(__name__)
    ab.register_routes(app, None, db_path=str(tmp_path / "t.db"), deps=deps, models_for=lambda a: [], now=lambda: NOW + 5)
    b = app.test_client().get(f"/api/llm/autotune/batch/{running['id']}").get_json()["batch"]
    assert b["status"] == "failed" and b["error"] == "manager restarted mid-batch"


def test_start_refuses_while_boot_recovery_is_still_running(env):
    class Alive:
        def is_alive(self):
            return True
    env.runner._recover_thread = Alive()
    r = env.post("/api/llm/autotune/batch", json=_body())
    assert r.status_code == 409 and "recovery" in r.get_json()["error"]
    env.runner._recover_thread = None
    assert env.post("/api/llm/autotune/batch", json=_body()).status_code == 200
