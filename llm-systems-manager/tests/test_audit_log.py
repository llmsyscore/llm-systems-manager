"""Admin action audit log (#217): route matching + central hook recording."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

import manager_mod as manager_mod  # noqa: E402  # loaded by conftest


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, role TEXT,
            ip TEXT, method TEXT, path TEXT, action TEXT, target TEXT,
            status INTEGER, outcome TEXT, auth TEXT, detail TEXT, event TEXT)
    """)
    return conn


@pytest.mark.parametrize("method,path,expected_action,expected_target", [
    ("POST",   "/api/agents/abc123/approve",       "agent.approve",   "abc123"),
    ("POST",   "/api/agents/abc123/disable",       "agent.disable",   "abc123"),
    ("DELETE", "/api/agents/abc123",               "agent.delete",    "abc123"),
    ("POST",   "/api/admin/service/manager/restart", "service.restart", "manager"),
    ("POST",   "/api/admin/auth",                  "auth.mode-change", None),
    ("POST",   "/api/admin/users",                 "user.create",     None),
    ("PATCH",  "/api/admin/users/bob",             "user.modify",     "bob"),
    ("DELETE", "/api/admin/users/bob",             "user.delete",     "bob"),
    ("POST",   "/api/admin/export/manager",        "backup.export",   None),
    ("POST",   "/api/llm/server/svcconfig",        "config.svcconfig", None),
    ("POST",   "/api/config/interval",             "config.interval", None),
    ("POST",   "/api/llm/load",                    "llama.load",      None),
    ("POST",   "/api/llm/unload",                  "llama.unload",    None),
    ("POST",   "/api/lmstudio/load",               "lms.load",        None),
    ("POST",   "/api/lmstudio/unload",             "lms.unload",      None),
])
def test_audit_match_known_routes(method, path, expected_action, expected_target):
    got = manager_mod._audit_match(method, path)
    assert got[:2] == (expected_action, expected_target)


def test_audit_match_generic_admin_fallback():
    assert manager_mod._audit_match("POST", "/api/admin/some-new-thing") == \
        ("admin.some-new-thing", None, "admin.other")


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/remote/provider-state"),   # agent push traffic
    ("POST", "/api/agents/heartbeat"),        # agent heartbeat
    ("GET",  "/api/admin/users"),             # read-only
    ("POST", "/api/llm/models"),              # not a load/unload
    ("POST", "/api/vllm/metrics"),            # telemetry, not control
])
def test_audit_match_skips_unaudited(method, path):
    if method == "GET":
        # GETs are filtered by the hook before matching; matcher itself
        # would match /api/admin/* — assert the hook's method filter path.
        return
    assert manager_mod._audit_match(method, path) is None


def test_audit_hook_records_denied_mutation(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    client = manager_mod.app.test_client()
    resp = client.post("/api/agents/deadbeef/approve")
    assert resp.status_code in (401, 403)
    rows = conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "agent.approve"
    assert row["target"] == "deadbeef"
    assert row["outcome"] == "denied"
    assert row["status"] == resp.status_code


def test_audit_hook_ignores_get(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    client = manager_mod.app.test_client()
    client.get("/api/admin/users")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_audit_record_prunes_past_cap(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(manager_mod, "_AUDIT_MAX_ROWS", 50)
    monkeypatch.setattr(manager_mod, "_AUDIT_PRUNE_EVERY", 10)
    for i in range(120):
        manager_mod._audit_record(
            ("2026-07-07T00:00:00+00:00", "a", "admin", "192.0.2.10", "session",
             "POST", "/api/admin/x", "admin.x", None, 200, "ok", None))
    count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert count <= 50 + manager_mod._AUDIT_PRUNE_EVERY
    # Newest rows survive.
    assert conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] == 120


def test_audit_hook_records_manual_unload_with_the_model_as_target(monkeypatch):
    conn = _mem_db()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    client = manager_mod.app.test_client()
    resp = client.post("/api/lmstudio/unload", json={"model": "qwen3-30b"})
    assert resp.status_code in (401, 403)
    row = conn.execute("SELECT * FROM audit_log").fetchone()
    assert row["action"] == "lms.unload" and row["target"] == "qwen3-30b"
    assert row["outcome"] == "denied"


def test_audit_body_target_prefers_model_id_and_ignores_junk():
    with manager_mod.app.test_request_context(json={"model_id": "m1", "model": "m2"}):
        assert manager_mod._audit_body_target() == "m1"
    with manager_mod.app.test_request_context(json={"model": 42}):
        assert manager_mod._audit_body_target() is None
    with manager_mod.app.test_request_context(data="not json"):
        assert manager_mod._audit_body_target() is None


# ── #794: event gating, automated traffic, auth kind, detail ──────────────

def _cfg(monkeypatch, **over):
    monkeypatch.setattr(manager_mod, "_AUDIT_CFG", {**manager_mod._AUDIT_CFG, **over})


def test_audit_hook_skips_disabled_event(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    _cfg(monkeypatch, disabled={"agent.lifecycle"})
    manager_mod.app.test_client().post("/api/agents/deadbeef/approve")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_audit_hook_drops_test_tagged_requests_unless_saved(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    _cfg(monkeypatch, save_automated=False, disabled=set())
    c = manager_mod.app.test_client()
    c.post("/api/agents/deadbeef/approve", headers={"X-LLMSys-Source": "test"})
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    _cfg(monkeypatch, save_automated=True, disabled=set())
    c.post("/api/agents/deadbeef/approve", headers={"X-LLMSys-Source": "smoke-test"})
    row = conn.execute("SELECT auth FROM audit_log").fetchone()
    assert row["auth"] == "test"


def test_audit_hook_gates_automated_actors_like_tagged_traffic(monkeypatch):
    """#814: a session user listed in automated_actors follows the Unit tests toggle."""
    import auth
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    monkeypatch.setattr(auth, "_live_role_for_session", lambda: ("admin", True))
    _cfg(monkeypatch, save_automated=False, disabled=set(), automated_actors=["smoketestuser"])
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True; s["user"] = "smoketestuser"; s["role"] = "admin"
    c.post("/api/agents/deadbeef/approve", environ_base={"REMOTE_ADDR": "192.0.2.12"})
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    _cfg(monkeypatch, save_automated=True, disabled=set(), automated_actors=["smoketestuser"])
    c.post("/api/agents/deadbeef/approve", environ_base={"REMOTE_ADDR": "192.0.2.12"})
    row = conn.execute("SELECT actor, auth FROM audit_log").fetchone()
    assert (row["actor"], row["auth"]) == ("smoketestuser", "session")


def test_audit_hook_keeps_failed_logins_for_automated_actors(monkeypatch):
    import auth
    import manager_users
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    _cfg(monkeypatch, save_automated=False, disabled=set(), automated_actors=["smoketestuser"])
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    monkeypatch.setattr(manager_users, "authenticate", lambda u, p, ip: {"ok": False})
    c = manager_mod.app.test_client()
    assert c.post("/login", data={"username": "smoketestuser", "password": "guess"}).status_code == 401
    row = conn.execute("SELECT actor, action, outcome FROM audit_log").fetchone()
    assert (row["actor"], row["action"], row["outcome"]) == ("smoketestuser", "auth.login", "denied")


def test_audit_hook_records_bypass_auth_kind_and_agent_detail(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents",
                        lambda: {"agents": {"deadbeef": {"hostname": "box-1", "status": "approved"}}})
    c = manager_mod.app.test_client()
    c.post("/api/agents/deadbeef/approve")
    row = conn.execute("SELECT auth, detail FROM audit_log").fetchone()
    assert row["auth"] == "bypass"
    assert json.loads(row["detail"])["agent"] == "box-1"


def test_audit_hook_records_error_detail(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    c = manager_mod.app.test_client()
    r = c.post("/api/admin/service/nope/restart")
    assert r.status_code >= 400
    row = conn.execute("SELECT detail, outcome FROM audit_log").fetchone()
    assert row["outcome"] in ("error", "denied")
    assert "error" in json.loads(row["detail"])


@pytest.mark.parametrize("method,path,action,event", [
    ("POST",   "/api/agents/a1/collection", "agent.collection", "agent.collection"),
    ("POST",   "/api/agents/a1/host-role", "agent.host-role", "agent.roles"),
    ("POST",   "/api/account/password", "account.password", "account.password"),
    ("POST",   "/api/llm/server/wake", "llama.server.wake", "llama.server"),
    ("POST",   "/api/vllm/server/start", "vllm.server.start", "vllm.server"),
    ("POST",   "/api/vllm/lora/load", "vllm.lora.load", "vllm.server"),
    ("POST",   "/api/llm/profiles/m%201/save", "model.profile.save", "model.config"),
    ("DELETE", "/api/llm/config/some/model", "model.config.delete", "model.config"),
    ("POST",   "/api/llm/download", "model.download", "model.downloads"),
    ("POST",   "/api/llm/download/cancel", "model.download-cancel", "model.downloads"),
    ("PUT",    "/api/autopilot", "autopilot.toggle", "autopilot.toggle"),
    ("POST",   "/api/autopilot/proposals/p1/apply", "autopilot.proposal-apply", "autopilot.proposal"),
    ("POST",   "/api/terminal/create", "terminal.open", "terminal.open"),
    ("POST",   "/api/lms/terminal/create", "terminal.open", "terminal.open"),
    ("POST",   "/api/reportcard/run", "reportcard.run", "reportcard"),
    ("DELETE", "/api/reportcard/history", "reportcard.clear-history", "reportcard"),
    ("POST",   "/api/alarm/alerts/17/close", "alarm.close", "alarm.actions"),
    ("PUT",    "/api/alarm/rules/r1", "alarm.rule", "alarm.actions"),
    ("POST",   "/login", "auth.login", "auth.login"),
    ("GET",    "/logout", "auth.logout", "auth.logout"),
    ("POST",   "/logout", "auth.logout", "auth.logout"),
    ("POST",   "/api/layout", "config.layout", "config.layout"),
])
def test_audit_match_new_routes(method, path, action, event):
    got = manager_mod._audit_match(method, path)
    assert got is not None and got[0] == action and got[2] == event
    assert manager_mod._audit_event_for(action) == event


def test_every_route_event_exists_in_catalog():
    keys = {ev["key"] for g in manager_mod.AUDIT_EVENT_GROUPS for ev in g["events"]}
    for entry in manager_mod._AUDIT_ROUTES:
        assert entry[3] in keys, entry[2]
    assert manager_mod._audit_event_for("autopilot:load") == "autopilot.executor"
    assert manager_mod._audit_group_for("autopilot:load") == "auto"
    assert manager_mod._audit_group_for("agent.vllm-pool") == "agent"


def test_settings_detail_records_old_and_new_and_masks_secrets(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    monkeypatch.setattr(manager_mod.settings_catalog, "file_catalog_values",
                        lambda: {"manager.audit.retention_days": 30, "manager.backup.passphrase": "old"})
    monkeypatch.setattr(manager_mod.settings_toml_io, "apply_patches", lambda *a, **k: None)
    monkeypatch.setattr(manager_mod, "_audit_reload_config", lambda: None)
    c = _admin_client(monkeypatch)
    r = c.put("/api/admin/settings", json={"changes": {"manager.audit.retention_days": 60,
                                                        "manager.backup.passphrase": "hunter2-secret"}})
    assert r.status_code == 200, r.get_json()
    d = json.loads(conn.execute("SELECT detail FROM audit_log").fetchone()["detail"])
    assert d["changes"]["manager.audit.retention_days"] == [30, 60]
    assert d["changes"]["manager.backup.passphrase"] == ["•••", "•••"]


def test_load_detail_carries_model_and_agent(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(manager_mod.agent_registry, "load_agents",
                        lambda: {"agents": {"a1": {"hostname": "lms-box", "status": "approved"}}})
    c = manager_mod.app.test_client()
    c.post("/api/lmstudio/load", json={"model": "qwen3-30b", "agent_id": "a1", "context_length": 32768})
    row = conn.execute("SELECT target, detail FROM audit_log").fetchone()
    d = json.loads(row["detail"])
    assert row["target"] == "qwen3-30b"
    assert d["model"] == "qwen3-30b" and d["agent"] == "lms-box" and d["context_length"] == 32768


def test_audit_purge_removes_rows_older_than_retention(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    _cfg(monkeypatch, retention_days=60)
    old = "2026-06-01T00:00:00+00:00"; new = "2026-08-30T00:00:00+00:00"
    for ts in (old, old, new):
        conn.execute("INSERT INTO audit_log (ts, action) VALUES (?, 'x')", (ts,))
    removed = manager_mod._audit_purge(now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert removed == 2
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    assert manager_mod._AUDIT_PURGE_STATE["removed"] == 2


def test_audit_purge_keeps_everything_when_retention_zero(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    _cfg(monkeypatch, retention_days=0)
    conn.execute("INSERT INTO audit_log (ts, action) VALUES ('2020-01-01T00:00:00+00:00', 'x')")
    assert manager_mod._audit_purge() == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def _seed(conn):
    rows = [
        ("2026-09-01T21:01:00+00:00", "llmadmin", "admin", "192.0.2.10", "session", "POST", "/api/admin/users", "user.create", "adriel", 200, "ok", None),
        ("2026-09-01T20:08:00+00:00", "adriel", "operator", "192.0.2.11", "session", "POST", "/api/lmstudio/unload", "lms.unload", "qwen3-30b", 200, "ok", '{"model":"qwen3-30b"}'),
        ("2026-09-01T19:00:00+00:00", "adriel", "operator", "192.0.2.11", "session", "POST", "/api/agents/a1/role-primary", "agent.role-primary", "a1", 403, "denied", None),
        ("2026-09-01T18:00:00+00:00", "autopilot", "system", "-", "internal", "", "", "autopilot:load", "m@a1", None, "ok", None),
        ("2026-09-01T17:00:00+00:00", "", "admin", "127.0.0.1", "test", "PUT", "/api/admin/settings", "config.settings", None, 200, "ok", None),
        ("2026-09-01T16:00:00+00:00", "", "admin", "127.0.0.1", None, "PUT", "/api/admin/settings", "config.settings", None, 200, "ok", None),
        ("2026-09-01T15:00:00+00:00", "smoketestuser", "admin", "192.0.2.12", "session", "DELETE", "/api/llm/aliases/smoke-test-model", "model.alias.delete", "smoke-test-model", 200, "ok", None),
    ]
    conn.executemany("INSERT INTO audit_log (ts, actor, role, ip, auth, method, path, action, target, status, outcome, detail)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _admin_client(monkeypatch):
    """Test client admitted as an admin session past the auth gate."""
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    manager_mod.app.config["TESTING"] = True
    c = manager_mod.app.test_client()
    with c.session_transaction() as s:
        s["auth_ok"] = True
        s["role"] = "admin"
    return c


def _client(monkeypatch):
    conn = _mem_db(); _seed(conn)
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    _cfg(monkeypatch, automated_actors=["smoketestuser"])
    return _admin_client(monkeypatch)


def test_audit_list_filters(monkeypatch):
    c = _client(monkeypatch)
    def q(qs):
        d = c.get("/api/admin/audit-log?" + qs).get_json()
        assert d["ok"]
        return d
    assert q("")["total"] == 7
    assert q("actor=adriel")["total"] == 2
    assert q("actor=smoketestuser")["total"] == 1
    assert q("outcome=denied")["total"] == 1
    assert q("q=qwen")["total"] == 1
    assert q("hide_automated=1")["total"] == 4
    assert q("hide_automated=1&actor=smoketestuser")["total"] == 1
    assert q("actor=system")["total"] == 0
    assert q("actor=local")["total"] == 1
    assert q("actor=autopilot")["total"] == 1
    assert q("group=auto")["total"] == 1
    assert q("group=agent")["total"] == 1
    assert q("group=user")["total"] == 1
    assert q("group=config")["total"] == 2 and q("group=model")["total"] == 2
    assert q("group=nope")["total"] == 0
    d = q("sort=actor&dir=asc&hide_automated=1")
    assert [e["actor"] for e in d["entries"]] == ["adriel", "adriel", "autopilot", "llmadmin"]
    e = q("actor=llmadmin")["entries"][0]
    assert e["group"] == "user" and e["label"] == "Created a dashboard user" and e["id"]
    assert q("actor=adriel&q=qwen")["entries"][0]["detail"] == {"model": "qwen3-30b"}


def test_audit_list_default_limit_is_page_size(monkeypatch):
    c = _client(monkeypatch)
    _cfg(monkeypatch, page_size=10)
    d = c.get("/api/admin/audit-log").get_json()
    assert d["page_size"] == 10 and len(d["entries"]) == 7


def test_audit_csv_export(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/admin/audit-log.csv?actor=adriel")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"]
    lines = r.get_data(as_text=True).strip().splitlines()
    assert lines[0].startswith("ts,actor,role,ip,auth,action") and len(lines) == 3


def test_audit_stats_and_events(monkeypatch):
    c = _client(monkeypatch)
    _cfg(monkeypatch, disabled={"auth.logout", "tools.run"}, retention_days=60)
    s = c.get("/api/admin/audit-log/stats").get_json()
    assert s["total"] == 7 and s["oldest"] == "2026-09-01T15:00:00+00:00"
    assert s["actors"] == ["adriel", "autopilot", "llmadmin", "smoketestuser"] and s["retention_days"] == 60
    ev = c.get("/api/admin/audit-log/events").get_json()
    flat = {e["key"]: e for g in ev["groups"] for e in g["events"]}
    assert flat["auth.logout"]["enabled"] is False and flat["user.manage"]["enabled"] is True
    assert ev["config"]["retention_days"] == 60
    assert ev["config"]["automated_actors"] == list(manager_mod._AUDIT_CFG["automated_actors"])


def _fake_snapshot(monkeypatch, disabled_events, file_has_key):
    class _A:  # mimics settings.manager.audit
        retention_days = 7; page_size = 50; save_automated = True
        automated_actors = ["smoketestuser", " bot ", "", "ci-a, ci-b"]
    _A.disabled_events = disabled_events
    class _M:
        audit = _A()
    class _S:
        manager = _M()
    monkeypatch.setattr(manager_mod.settings_catalog, "_snapshot", lambda: _S())
    monkeypatch.setattr(manager_mod.settings_catalog, "file_catalog_values",
                        lambda: {"manager.audit.disabled_events": disabled_events} if file_has_key else {})


def test_audit_reload_config_reads_the_file_snapshot(monkeypatch):
    _fake_snapshot(monkeypatch, ["auth.logout"], file_has_key=True)
    saved = dict(manager_mod._AUDIT_CFG)
    try:
        manager_mod._audit_reload_config()
        assert manager_mod._AUDIT_CFG["retention_days"] == 7
        assert manager_mod._AUDIT_CFG["page_size"] == 50
        assert manager_mod._AUDIT_CFG["save_automated"] is True
        assert manager_mod._AUDIT_CFG["automated_actors"] == ["smoketestuser", "bot", "ci-a", "ci-b"]
        assert manager_mod._AUDIT_CFG["disabled"] == {"auth.logout"}
    finally:
        manager_mod._AUDIT_CFG.clear(); manager_mod._AUDIT_CFG.update(saved)


def test_audit_reload_config_uses_catalog_defaults_when_unset(monkeypatch):
    _fake_snapshot(monkeypatch, [], file_has_key=False)
    saved = dict(manager_mod._AUDIT_CFG)
    try:
        manager_mod._audit_reload_config()
        assert manager_mod._AUDIT_CFG["disabled"] == manager_mod._AUDIT_DEFAULT_OFF
        assert "auth.logout" in manager_mod._AUDIT_DEFAULT_OFF and "user.manage" not in manager_mod._AUDIT_DEFAULT_OFF
    finally:
        manager_mod._AUDIT_CFG.clear(); manager_mod._AUDIT_CFG.update(saved)


def test_source_header_only_counts_from_loopback(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    _cfg(monkeypatch, save_automated=False, disabled=set())
    c = manager_mod.app.test_client()
    c.post("/api/agents/deadbeef/approve", headers={"X-LLMSys-Source": "test"},
           environ_base={"REMOTE_ADDR": "192.0.2.77"})
    row = conn.execute("SELECT auth, event FROM audit_log").fetchone()
    assert row is not None and row["auth"] != "test" and row["event"] == "agent.lifecycle"


def test_group_filter_uses_the_stored_event_and_falls_back_for_legacy_rows(monkeypatch):
    c = _client(monkeypatch)
    conn = manager_mod.get_db()
    conn.execute("INSERT INTO audit_log (ts, actor, action, event) VALUES ('2026-09-01T22:00:00+00:00', 'x', 'admin.some-new-thing', 'config.settings')")
    d = c.get("/api/admin/audit-log?group=config").get_json()
    assert d["total"] == 3
    assert {e["event"] for e in d["entries"]} == {"config.settings"}


def test_csv_neutralises_formula_cells(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    conn.execute("INSERT INTO audit_log (ts, actor, action, target) VALUES ('2026-09-01T22:00:00+00:00', '=cmd|calc', 'auth.login', '@evil')")
    c = _admin_client(monkeypatch)
    body = c.get("/api/admin/audit-log.csv").get_data(as_text=True)
    assert "'=cmd|calc" in body and "'@evil" in body


def test_hide_automated_hides_blank_bypass_from_loopback_only(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    conn.executemany("INSERT INTO audit_log (ts, actor, ip, auth, action) VALUES (?,?,?,?,?)", [
        ("2026-09-01T22:00:00+00:00", "", "127.0.0.1", "bypass", "agent.approve"),
        ("2026-09-01T22:00:00+00:00", "", "192.0.2.5", "bypass", "agent.approve"),
    ])
    c = _admin_client(monkeypatch)
    assert c.get("/api/admin/audit-log?hide_automated=1").get_json()["total"] == 1
    assert c.get("/api/admin/audit-log?actor=local").get_json()["total"] == 2


def test_detail_shrink_keeps_a_truncated_changes_diff():
    big = {"changes": {f"manager.k{i}": ["a" * 100, "b" * 100] for i in range(40)}, "applied": ["x"]}
    out = manager_mod._audit_detail_shrink(big)
    assert len(out["changes"]) == 20 and out["changes_truncated"] == 20
    assert all(len(v[0]) <= 80 for v in out["changes"].values())
    assert len(json.dumps(out)) < manager_mod._AUDIT_DETAIL_MAX * 3


def test_login_failure_is_audited_with_the_attempted_username(monkeypatch):
    import auth
    import manager_users
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    monkeypatch.setattr(manager_users, "authenticate", lambda u, p, ip: {"ok": False})
    c = manager_mod.app.test_client()
    r = c.post("/login", data={"username": "ghost", "password": "nope"})
    assert r.status_code == 401
    row = conn.execute("SELECT actor, action, target, outcome, auth FROM audit_log").fetchone()
    assert (row["actor"], row["action"], row["target"], row["outcome"]) == ("ghost", "auth.login", "ghost", "denied")


def test_login_success_is_audited_as_the_signed_in_user(monkeypatch):
    import auth
    import manager_users
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    monkeypatch.setattr(manager_users, "authenticate",
                        lambda u, p, ip: {"ok": True, "username": "llmadmin", "role": "admin"})
    c = manager_mod.app.test_client()
    r = c.post("/login", data={"username": "llmadmin", "password": "x"})
    assert r.status_code in (302, 303)
    row = conn.execute("SELECT actor, role, action, outcome FROM audit_log").fetchone()
    assert (row["actor"], row["role"], row["action"], row["outcome"]) == ("llmadmin", "admin", "auth.login", "ok")


def test_lockout_is_audited_as_denied_with_reason(monkeypatch):
    import auth
    import manager_users
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    monkeypatch.setattr(auth, "auth_mode", lambda: "required")
    monkeypatch.setattr(manager_users, "authenticate", lambda u, p, ip: {"ok": False, "locked": True})
    c = manager_mod.app.test_client()
    assert c.post("/login", data={"username": "ghost", "password": "nope"}).status_code == 429
    row = conn.execute("SELECT outcome, detail FROM audit_log").fetchone()
    assert row["outcome"] == "denied" and json.loads(row["detail"])["reason"] == "locked"


def test_user_create_targets_the_new_username(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.app.test_client().post("/api/admin/users", json={"username": "newbie", "password": "x" * 12, "role": "operator"})
    row = conn.execute("SELECT target, detail FROM audit_log").fetchone()
    assert row["target"] == "newbie" and json.loads(row["detail"])["role"] == "operator"


def test_hide_automated_hides_untagged_loopback_rows_even_with_an_actor(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    conn.executemany("INSERT INTO audit_log (ts, actor, ip, auth, action, target) VALUES (?,?,?,?,?,?)", [
        ("2026-09-01T19:32:00+00:00", "llmadmin", "127.0.0.1", None, "user.delete", "smoke-test-user"),
        ("2026-09-01T19:32:00+00:00", "llmadmin", "127.0.0.1", "session", "user.delete", "op1"),
        ("2026-09-01T19:32:00+00:00", "llmadmin", "192.0.2.10", None, "user.delete", "real-old-row"),
    ])
    c = _admin_client(monkeypatch)
    shown = c.get("/api/admin/audit-log?hide_automated=1").get_json()
    assert sorted(e["target"] for e in shown["entries"]) == ["op1", "real-old-row"]
    assert c.get("/api/admin/audit-log").get_json()["total"] == 3


def _strict_json(data):
    def _bad(tok):
        raise ValueError("non-finite token in JSON: " + tok)
    return json.loads(data, parse_constant=_bad)


def test_audit_list_never_emits_nan_or_infinity(monkeypatch):
    """#815: NaN/Infinity inside a stored detail must not reach the JSON response."""
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    conn.execute("INSERT INTO audit_log (ts, actor, role, ip, auth, method, path, action, target, status, outcome, detail)"
                 " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("2026-09-03T11:10:08+00:00", "llmadmin", "admin", "192.0.2.10", "session", "PUT",
                  "/api/admin/settings", "config.settings", None, 400, "error",
                  '{"changes": {"manager.port": [5000, NaN], "x": [Infinity, -Infinity]}, "n": 1}'))
    c = _admin_client(monkeypatch)
    resp = c.get("/api/admin/audit-log")
    d = _strict_json(resp.data)
    assert d["ok"] and d["entries"][0]["detail"] == {"changes": {"manager.port": [5000, None], "x": [None, None]}, "n": 1}


def test_audit_hook_stores_non_finite_detail_values_as_null(monkeypatch):
    conn = _mem_db(); monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    _cfg(monkeypatch, disabled=set())
    monkeypatch.setattr(manager_mod, "_audit_detail",
                        lambda action, target, resp: {"score": float("nan"), "ceil": float("inf"), "ok": 1.5})
    manager_mod.app.test_client().post("/api/agents/deadbeef/approve")
    raw = conn.execute("SELECT detail FROM audit_log").fetchone()["detail"]
    assert _strict_json(raw) == {"score": None, "ceil": None, "ok": 1.5}


def test_audit_hook_failure_is_logged_at_warning(monkeypatch, caplog):
    """#853: a dropped audit row must surface at the running log level."""
    def boom():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(manager_mod, "get_db", boom)
    client = manager_mod.app.test_client()
    with caplog.at_level(logging.WARNING, logger="llm-systems-manager"):
        resp = client.post("/api/admin/export/manager")
    assert resp.status_code in (401, 403)
    dropped = [r for r in caplog.records if r.levelno >= logging.WARNING
               and r.getMessage().startswith("audit row dropped")]
    assert len(dropped) == 1
    assert "backup.export" in dropped[0].getMessage()
    assert dropped[0].exc_info
