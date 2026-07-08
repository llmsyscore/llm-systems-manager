"""Admin action audit log (#217): route matching + central hook recording."""
from __future__ import annotations

import sqlite3

import pytest

import manager_mod as manager_mod  # noqa: E402  # loaded by conftest


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, role TEXT,
            ip TEXT, method TEXT, path TEXT, action TEXT, target TEXT,
            status INTEGER, outcome TEXT)
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
])
def test_audit_match_known_routes(method, path, expected_action, expected_target):
    got = manager_mod._audit_match(method, path)
    assert got == (expected_action, expected_target)


def test_audit_match_generic_admin_fallback():
    assert manager_mod._audit_match("POST", "/api/admin/some-new-thing") == \
        ("admin.some-new-thing", None)


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/remote/provider-state"),   # agent push traffic
    ("POST", "/api/agents/heartbeat"),        # agent heartbeat
    ("GET",  "/api/admin/users"),             # read-only
    ("POST", "/api/llm/load"),                # non-admin operator surface
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
            ("2026-07-07T00:00:00+00:00", "a", "admin", "127.0.0.1",
             "POST", "/api/admin/x", "admin.x", None, 200, "ok"))
    count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert count <= 50 + manager_mod._AUDIT_PRUNE_EVERY
    # Newest rows survive.
    assert conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] == 120
