"""Manager hardening (#666, #668, #669, #670): bounded long-history cache,
layout payload validation, audit-log int parsing, one-shot WS tickets."""
from __future__ import annotations

import json

import pytest

import auth
import manager_mod as M


# ── #666 ─────────────────────────────────────────────────────────────────────

def test_history_long_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(M, "_history_long_cache", {})
    for i in range(M._HISTORY_LONG_MAX_ENTRIES * 3):
        M._history_long_put((61 + i, 10000), [{"ts": i}], float(i))
    assert len(M._history_long_cache) == M._HISTORY_LONG_MAX_ENTRIES
    newest = M._HISTORY_LONG_MAX_ENTRIES * 3 - 1
    assert (61 + newest, 10000) in M._history_long_cache
    assert (61, 10000) not in M._history_long_cache


# ── #668 ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def layout_file(tmp_path, monkeypatch):
    p = tmp_path / "layout.json"
    p.write_text(json.dumps({"cols": 3}))
    monkeypatch.setattr(M, "LAYOUT_FILE", p)
    monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
    return p


@pytest.mark.parametrize("bad", ["[]", "\"str\"", "42", "null"])
def test_layout_rejects_non_object(layout_file, bad):
    with M.app.test_client() as c:
        r = c.post("/api/layout", data=bad, content_type="application/json")
    assert r.status_code == 400
    assert json.loads(layout_file.read_text()) == {"cols": 3}


def test_layout_rejects_oversized(layout_file):
    huge = {"pad": "x" * (M._LAYOUT_MAX_BYTES + 1)}
    with M.app.test_client() as c:
        r = c.post("/api/layout", data=json.dumps(huge), content_type="application/json")
    assert r.status_code == 400
    assert json.loads(layout_file.read_text()) == {"cols": 3}


def test_layout_accepts_object(layout_file):
    with M.app.test_client() as c:
        r = c.post("/api/layout", data=json.dumps({"cols": 4, "hidden": []}),
                   content_type="application/json")
        assert r.status_code == 200
        assert c.get("/api/layout").get_json() == {"cols": 4, "hidden": []}


# ── #669 ─────────────────────────────────────────────────────────────────────

def test_audit_log_int_parse_tolerates_garbage(monkeypatch):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, role TEXT,"
                 " ip TEXT, method TEXT, path TEXT, action TEXT, target TEXT, status INTEGER, outcome TEXT, auth TEXT, detail TEXT, event TEXT)")
    monkeypatch.setattr(M, "get_db", lambda: conn)
    monkeypatch.setattr(M, "_require_admin", lambda: None)
    monkeypatch.setattr(auth, "auth_mode", lambda: "disabled")
    with M.app.test_client() as c:
        for q in ("limit=abc&offset=zzz", "limit=&offset=", "limit=1e3"):
            r = c.get(f"/api/admin/audit-log?{q}")
            assert r.status_code == 200, q
            assert r.get_json()["ok"] is True


# ── #670 ─────────────────────────────────────────────────────────────────────

def test_ws_ticket_is_single_use(monkeypatch):
    monkeypatch.setattr(M, "_ws_tickets_spent", {})
    t = M._issue_ws_ticket()
    assert M.ws_handshake_denial(f"/ws/alarm?ticket={t}") is None
    assert M.ws_handshake_denial(f"/ws/alarm?ticket={t}") == (1008, "unauthorized")
    # A fresh ticket still works; verify-only never spends.
    t2 = M._issue_ws_ticket()
    assert M._verify_ws_ticket(t2) and M._verify_ws_ticket(t2)
    assert M.ws_handshake_denial(f"/ws/alarm?ticket={t2}") is None


def test_ws_ticket_nonce_is_signed(monkeypatch):
    monkeypatch.setattr(M, "_ws_tickets_spent", {})
    expiry, nonce, sig = M._issue_ws_ticket().split(".")
    assert M._verify_ws_ticket(f"{expiry}.{'0' * len(nonce)}.{sig}") is False
    assert M._verify_ws_ticket(f"{expiry}.{nonce}") is False


def test_spent_nonces_are_swept_after_expiry(monkeypatch):
    monkeypatch.setattr(M, "_ws_tickets_spent", {"old": 1})
    assert M._consume_ws_ticket(M._issue_ws_ticket()) is True
    assert "old" not in M._ws_tickets_spent
    assert len(M._ws_tickets_spent) == 1
