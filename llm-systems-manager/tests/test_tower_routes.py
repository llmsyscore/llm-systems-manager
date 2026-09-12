"""#924 Tower routes: disabled → 404, state, threads per user, run → SSE, one run per user, model pin audit."""
from __future__ import annotations

import json
import queue
import threading
import time

import pytest

import auth
import manager_mod as M
import tower
from config.unified_config import settings
from flask import session as _flask_session

ENTRIES = [{"id": "qwen3-14b", "provider": "llama", "status": {"value": "loaded"}, "hosts": ["box"]}]


def _wait_done(rid, timeout=2.0):
    """Polls M._tower_runs for a run to finish; returns the run dict (or None if never seen)."""
    deadline = time.time() + timeout
    run = M._tower_runs._runs.get(rid)
    while run and not run["done"] and time.time() < deadline:
        time.sleep(0.01)
    return run


@pytest.fixture
def client(monkeypatch):
    orig_tower = settings.manager.tower.model_copy()
    settings.manager.tower.enabled = True
    settings.manager.tower.capabilities = "read"
    settings.manager.tower.off_topic = "refuse"
    # Route role checks off the session directly — this suite's synthetic
    # session users ("alice"/"bob") aren't registered manager_users rows.
    monkeypatch.setattr(auth, "_live_role_for_session", lambda: (_flask_session.get("role"), True))
    monkeypatch.setattr(tower, "_gateway_entries", lambda: ENTRIES, raising=False)
    orig_store = M._tower_runs._store
    orig_shutting_down = M._tower_runs._shutting_down
    orig_stream_max_s = M._tower_runs._stream_max_s
    M._tower_runs._store = tower.Store(":memory:")
    M._tower_runs._store.init_tables()
    M._tower_runs._runs.clear()
    M._tower_runs._active_user.clear()
    M._tower_runs._rate.clear()
    M.app.config["TESTING"] = True
    try:
        with M.app.test_client() as c:
            with c.session_transaction() as s:
                s["auth_ok"] = True; s["role"] = "operator"; s["user"] = "alice"
            yield c
    finally:
        M._tower_runs._store = orig_store
        M._tower_runs._shutting_down = orig_shutting_down
        M._tower_runs._stream_max_s = orig_stream_max_s
        for k, v in orig_tower.model_dump().items():
            setattr(settings.manager.tower, k, v)


def test_disabled_routes_404(client):
    settings.manager.tower.enabled = False
    d = client.get("/api/tower/state")
    assert d.status_code == 200
    body = d.get_json()
    assert body["ok"] and body["enabled"] is False and body["admin"] is False  # session role is "operator"
    assert client.get("/api/tower/threads").status_code == 404


def test_state_reports_model_and_tiers(client):
    d = client.get("/api/tower/state").get_json()
    assert d["ok"] and d["enabled"] and d["model"] == "qwen3-14b" and d["provider"] == "llama"
    assert d["capabilities"] == "read" and d["off_topic"] == "refuse" and d["insights_new"] == 0


def test_threads_are_scoped_to_the_session_user(client):
    t = client.post("/api/tower/threads", json={"page": {"tab": "overall"}}).get_json()
    assert t["ok"] and t["thread"]["id"]
    assert [x["id"] for x in client.get("/api/tower/threads").get_json()["threads"]] == [t["thread"]["id"]]
    with client.session_transaction() as s:
        s["user"] = "bob"
    assert client.get(f"/api/tower/threads/{t['thread']['id']}").status_code == 404


def test_bypass_sessions_get_separate_thread_namespaces(client):
    """#924 review IMPORTANT 5: sessions with no named user each mint their own id."""
    with client.session_transaction() as s:
        del s["user"]
    c2 = M.app.test_client()
    with c2.session_transaction() as s:
        s["auth_ok"] = True; s["role"] = "operator"
    t1 = client.post("/api/tower/threads", json={}).get_json()["thread"]["id"]
    t2 = c2.post("/api/tower/threads", json={}).get_json()["thread"]["id"]
    ids1 = [x["id"] for x in client.get("/api/tower/threads").get_json()["threads"]]
    ids2 = [x["id"] for x in c2.get("/api/tower/threads").get_json()["threads"]]
    assert ids1 == [t1] and ids2 == [t2] and t1 != t2


def test_page_context_is_whitelisted_and_capped(client):
    """#924 review IMPORTANT 6: unknown keys dropped, strings/list capped."""
    page = {"tab": "x" * 200, "sub": "s", "host": "h", "alert_id": "a1",
            "cards": ["c" * 100] * 30, "evil": "drop me", "extra": {"a": 1}}
    stored = client.post("/api/tower/threads", json={"page": page}).get_json()["thread"]["page"]
    assert set(stored) <= {"tab", "sub", "host", "alert_id", "cards"}
    assert stored["sub"] == "s" and stored["host"] == "h" and stored["alert_id"] == "a1"
    assert len(stored["tab"]) == 120
    assert len(stored["cards"]) == 24 and all(len(c) == 64 for c in stored["cards"])
    assert "evil" not in stored and "extra" not in stored


def test_message_starts_a_run_and_streams_events(client, monkeypatch):
    def fake_turn(**kw):
        kw["emit"]({"event": "model", "model": "qwen3-14b", "provider": "llama", "hosts": ["box"]})
        kw["emit"]({"event": "delta", "text": "hi"})
        kw["emit"]({"event": "done", "ok": True, "calls": 0, "elapsed_ms": 5})
        return {"ok": True, "calls": 0}
    monkeypatch.setattr(tower, "run_turn", fake_turn)
    tid = client.post("/api/tower/threads", json={}).get_json()["thread"]["id"]
    r = client.post(f"/api/tower/threads/{tid}/messages", json={"text": "hello", "page": {"tab": "overall"}})
    assert r.status_code == 200 and r.get_json()["run_id"]
    rid = r.get_json()["run_id"]
    body = client.get(f"/api/tower/runs/{rid}/stream").get_data(as_text=True)
    kinds = [json.loads(l[6:])["event"] for l in body.splitlines() if l.startswith("data: ")]
    assert kinds == ["model", "delta", "done"]


def test_second_run_while_one_is_active_is_409(client, monkeypatch):
    gate = threading.Event()
    def slow_turn(**kw):
        gate.wait(5); kw["emit"]({"event": "done", "ok": True, "calls": 0, "elapsed_ms": 1}); return {"ok": True}
    monkeypatch.setattr(tower, "run_turn", slow_turn)
    tid = client.post("/api/tower/threads", json={}).get_json()["thread"]["id"]
    r = client.post(f"/api/tower/threads/{tid}/messages", json={"text": "a"})
    assert r.status_code == 200
    rid = r.get_json()["run_id"]
    assert client.post(f"/api/tower/threads/{tid}/messages", json={"text": "b"}).status_code == 409
    gate.set()
    run = _wait_done(rid)
    assert run is not None and run["done"] is True


def test_empty_text_is_400_and_no_model_is_503(client, monkeypatch):
    tid = client.post("/api/tower/threads", json={}).get_json()["thread"]["id"]
    assert client.post(f"/api/tower/threads/{tid}/messages", json={"text": "  "}).status_code == 400
    monkeypatch.setattr(tower, "_gateway_entries", lambda: [], raising=False)
    r = client.post(f"/api/tower/threads/{tid}/messages", json={"text": "x"})
    assert r.status_code == 503 and r.get_json()["error"] == "no_model"


def test_gc_frees_active_user_slot_after_ttl(client, monkeypatch):
    """#924 review CRITICAL 1: a popped-by-TTL run must not KeyError the next start()."""
    def fake_turn(**kw):
        kw["emit"]({"event": "done", "ok": True, "calls": 0, "elapsed_ms": 1})
        return {"ok": True}
    monkeypatch.setattr(tower, "run_turn", fake_turn)
    tid = client.post("/api/tower/threads", json={}).get_json()["thread"]["id"]
    r = client.post(f"/api/tower/threads/{tid}/messages", json={"text": "a"})
    rid = r.get_json()["run_id"]
    run = _wait_done(rid)
    assert run is not None and run["done"] is True
    later = time.time() + tower._RUN_TTL_S + 1
    monkeypatch.setattr(tower, "_now", lambda: later)
    r2 = client.post(f"/api/tower/threads/{tid}/messages", json={"text": "b"})
    assert r2.status_code == 200
    assert rid not in M._tower_runs._runs  # GC'd, and no stale KeyError along the way


def test_worker_reaches_done_even_when_the_queue_backs_up(client, monkeypatch):
    """#924 review CRITICAL 2: emit must never block on a full, undrained queue."""
    def flood_turn(**kw):
        for i in range(600):
            kw["emit"]({"event": "delta", "text": str(i)})
        kw["emit"]({"event": "done", "ok": True, "calls": 0, "elapsed_ms": 1})
        return {"ok": True}
    monkeypatch.setattr(tower, "run_turn", flood_turn)
    tid = client.post("/api/tower/threads", json={}).get_json()["thread"]["id"]
    rid = client.post(f"/api/tower/threads/{tid}/messages", json={"text": "a"}).get_json()["run_id"]
    run = _wait_done(rid, timeout=2.0)
    assert run is not None and run["done"] is True


def test_closing_the_stream_cancels_the_run():
    """#924 review CRITICAL 2: the SSE generator closing (client gone) cancels an unfinished run."""
    run = {"id": "r1", "user": "alice", "queue": queue.Queue(maxsize=4),
           "done": False, "cancel": threading.Event(), "started": time.time()}
    run["queue"].put({"event": "status", "state": "thinking"})
    gen = M._tower_runs.stream(run)
    next(gen)
    gen.close()
    assert run["cancel"].is_set()


def test_stop_unknown_run_is_404(client):
    assert client.post("/api/tower/runs/doesnotexist/stop").status_code == 404


def test_model_pin_is_admin_only_and_writes_through_settings(client, monkeypatch):
    written = {}
    monkeypatch.setattr(M.settings_toml_io, "apply_patches", lambda sets, removals=(): written.update(sets))
    monkeypatch.setattr(M, "_tower_reload_config", lambda: None)
    assert client.put("/api/tower/model", json={"model": "qwen3-14b"}).status_code == 403
    with client.session_transaction() as s:
        s["role"] = "admin"
    assert client.put("/api/tower/model", json={"model": "qwen3-14b"}).status_code == 200
    assert written == {"manager.tower.model": "qwen3-14b"}


def test_model_pin_rejects_invalid_model_with_400(client, monkeypatch):
    monkeypatch.setattr(M.settings_catalog, "validate_and_coerce", lambda changes: ({}, {p: "not an editable setting" for p in changes}))
    monkeypatch.setattr(M.settings_toml_io, "apply_patches", lambda sets, removals=(): pytest.fail("must not write"))
    with client.session_transaction() as s:
        s["role"] = "admin"
    r = client.put("/api/tower/model", json={"model": "qwen3-14b"})
    assert r.status_code == 400 and r.get_json()["error"] == "invalid model"


def test_service_health_uses_agent_liveness_not_a_missing_last_seen(monkeypatch):
    import datetime as dt
    fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    monkeypatch.setattr(M.agent_registry, "load_agents", lambda: {"agents": {
        "a1": {"status": "approved", "hostname": "box", "last_heartbeat": fresh},
        "a2": {"status": "approved", "hostname": "mac", "last_heartbeat": old},
        "a3": {"status": "disabled", "hostname": "gone", "last_heartbeat": fresh}}})
    monkeypatch.setattr(M, "_ae_session", type("S", (), {"get": lambda self, url, timeout=3: type("R", (), {"ok": True})()})())
    h = M._tower_service_health()
    assert h["agents"]["online"] == ["box"]
    assert h["agents"]["offline"] == [{"host": "mac", "liveness": "down"}]
    assert h["alarm_engine"] == {"ok": True}


def test_tower_write_setting_splits_removals_and_runs_the_matching_hot_reloader(monkeypatch):
    calls = {}
    monkeypatch.setattr(M.settings_toml_io, "apply_patches", lambda sets, removals=(): calls.update({"sets": sets, "removals": list(removals)}))
    monkeypatch.setitem(M._HOT_RELOADERS, "manager.tower.", lambda: calls.setdefault("reloaded", []).append("tower"))
    M._tower_write_setting("manager.tower.max_tokens", 512)
    assert calls == {"sets": {"manager.tower.max_tokens": 512}, "removals": [], "reloaded": ["tower"]}
    calls.clear()
    M._tower_write_setting("manager.tower.max_tokens", None)
    assert calls["sets"] == {} and calls["removals"] == ["manager.tower.max_tokens"] and calls["reloaded"] == ["tower"]


def test_bypass_identity_is_a_permanent_session(client):
    with client.session_transaction() as s:
        s.pop("user", None); s["auth_ok"] = True; s["role"] = "operator"
    r = client.post("/api/tower/threads", json={"page": {}})
    assert r.status_code == 200
    with client.session_transaction() as s:
        assert s.get("tower_uid") and s.permanent is True
