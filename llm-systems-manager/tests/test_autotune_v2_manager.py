"""#880: the manager proxies the autotune preflight and keeps the run/stream/cancel routes."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "backend" / "llm-systems-manager.py").read_text()


def test_preflight_route_proxies_to_the_agent():
    m = re.search(r'@app\.route\("/api/llm/autotune/preflight"\)\s*\ndef (\w+)\(\):(.*?)\n\n', SRC, re.S)
    assert m, "preflight route missing"
    body = m.group(2)
    # #916: the route is provider-aware (llama by default, LM Studio via ?provider=lms).
    assert 'proxy_to_primary(provider, "GET", f"/{provider}/autotune/preflight"' in body and '_tool_provider("autotune")' in body


def test_run_route_still_notes_tool_start():
    m = re.search(r'@app\.route\("/api/llm/autotune/run", methods=\["POST"\]\)(.*?)\n\n', SRC, re.S)
    # #897: the route hands off to _tool_start, which notes the start (proxied) or queues the run.
    assert m and "_tool_start(provider, tool," in m.group(1) and '_tool_provider("autotune")' in m.group(1)
    # The quality guard shares this route but is its own tool to the gate (#887).
    assert '"quality" if (body.get("mode") or "") == "quality" else "autotune"' in m.group(1)


import json

import pytest

import manager_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
    # No default llama host in the test registry: the status route lists every host unless told otherwise.
    monkeypatch.setattr(manager_mod, "_request_agent", lambda kind: None)
    manager_mod.app.config["TESTING"] = True
    conn = manager_mod.get_db()
    conn.execute("DELETE FROM tool_runs")
    conn.commit()
    with manager_mod.app.test_client() as c:
        with c.session_transaction() as s:
            s["auth_ok"] = True
            s["role"] = "admin"
        yield c


def _row(conn, agent, model, ok, summary, ts):
    ba = summary.get("build_active")
    conn.execute("INSERT INTO tool_runs (tool, model_id, agent_id, provider, ok, summary, ts, run_id, build_active) "
                 "VALUES ('autotune', ?, ?, 'llama', ?, ?, ?, ?, ?)",
                 (model, agent, 1 if ok else 0, json.dumps(summary), ts, ts, None if ba is None else int(ba)))


def test_quality_is_an_accepted_ledger_tool():
    assert "quality" in manager_mod._TOOL_RUN_TOOLS


def test_status_flags_stale_when_heartbeat_build_differs(client, monkeypatch):
    conn = manager_mod.get_db()
    _row(conn, "a1", "org/m:Q4", True, {"llama_build": "b100-aaa", "decode_tps": 40.0}, "2026-09-01T00:00:00Z")
    _row(conn, "a1", "org/m:Q4", True, {"llama_build": "b110-bbb", "decode_tps": 42.0}, "2026-09-02T00:00:00Z")
    _row(conn, "a1", "org/m:Q4", False, {"llama_build": "b120-ccc"}, "2026-09-03T00:00:00Z")
    _row(conn, "a1", "org/old:Q4", True, {}, "2026-09-02T00:00:00Z")
    _row(conn, "a2", "org/m:Q4", True, {"llama_build": "b120-ccc"}, "2026-09-02T00:00:01Z")
    conn.commit()
    monkeypatch.setattr(manager_mod, "_llama_build_of", lambda aid: "b120-ccc")
    d = client.get("/api/llm/autotune/status").get_json()
    assert d["ok"]
    by = {(i["agent_id"], i["model_id"]): i for i in d["items"]}
    assert by[("a1", "org/m:Q4")]["llama_build"] == "b110-bbb"          # newest OK row wins
    assert by[("a1", "org/m:Q4")]["stale"] is True and by[("a1", "org/m:Q4")]["current_build"] == "b120-ccc"
    assert by[("a1", "org/old:Q4")]["stale"] is None                    # pre-#887 row: unknown
    assert by[("a2", "org/m:Q4")]["stale"] is False
    d2 = client.get("/api/llm/autotune/status?model_id=org/m:Q4&agent_id=a2").get_json()
    assert [i["agent_id"] for i in d2["items"]] == ["a2"]


def test_status_keeps_a_regressed_verify_stale(client, monkeypatch):
    conn = manager_mod.get_db()
    _row(conn, "a1", "org/m:Q4", True, {"llama_build": "b120-ccc", "mode": "verify", "regressed": True},
         "2026-09-04T00:00:00Z")
    _row(conn, "a2", "org/m:Q4", True, {"llama_build": "b120-ccc", "mode": "verify", "regressed": False},
         "2026-09-04T00:00:01Z")
    conn.commit()
    monkeypatch.setattr(manager_mod, "_llama_build_of", lambda aid: "b120-ccc")
    by = {i["agent_id"]: i for i in client.get("/api/llm/autotune/status").get_json()["items"]}
    assert by["a1"]["stale"] is True
    assert by["a1"]["llama_build"] == "b120-ccc" and by["a1"]["current_build"] == "b120-ccc"
    assert by["a2"]["stale"] is False


def test_status_ignores_other_build_rows_of_a_build_run(client, monkeypatch):
    conn = manager_mod.get_db()
    _row(conn, "a1", "org/m:Q4", True, {"llama_build": "b120-ccc", "build_name": "llama-a", "build_active": True},
         "2026-09-06T00:00:00Z")
    _row(conn, "a1", "org/m:Q4", True, {"llama_build": "b100-aaa", "build_name": "llama-b", "build_active": False},
         "2026-09-06T00:00:01Z")
    conn.commit()
    monkeypatch.setattr(manager_mod, "_llama_build_of", lambda aid: "b120-ccc")
    items = client.get("/api/llm/autotune/status").get_json()["items"]
    assert [(i["llama_build"], i["stale"]) for i in items] == [("b120-ccc", False)]


# #1399: the recording route copies build_active into its own column; the status route filters on it.
def test_record_stores_build_active_as_a_column(client, monkeypatch):
    monkeypatch.setattr(manager_mod, "_llama_build_of", lambda aid: "b120-ccc")
    for rid, active in (("r1", True), ("r1-2", False), ("r0", None)):
        body = {"tool": "autotune", "model_id": "org/m:Q4", "run_id": rid, "llama_build": "b120-ccc"}
        if active is not None:
            body["build_active"] = active
        assert client.post("/api/tools/runs", json=body).get_json()["ok"]
    conn = manager_mod.get_db()
    got = dict(conn.execute("SELECT run_id, build_active FROM tool_runs").fetchall())
    assert got == {"r1": 1, "r1-2": 0, "r0": None}
    # A row whose summary says active but whose column says not is skipped: the route reads the column only.
    conn.execute("UPDATE tool_runs SET build_active = 0 WHERE run_id = 'r1'")
    conn.execute("UPDATE tool_runs SET build_active = 0 WHERE run_id = 'r0'")
    conn.commit()
    assert client.get("/api/llm/autotune/status").get_json()["items"] == []


# #1410: the dashboard's record of a run lands first without the flag; the agent's later record fills it.
def test_later_record_fills_a_missing_build_active(client, monkeypatch):
    monkeypatch.setattr(manager_mod, "_llama_build_of", lambda aid: "b120-ccc")
    base = {"tool": "autotune", "model_id": "org/m:Q4", "llama_build": "b100-aaa"}
    assert client.post("/api/tools/runs", json={**base, "run_id": "r1-2"}).get_json()["ok"]
    assert client.post("/api/tools/runs", json={**base, "run_id": "r1-2", "build_active": False}).get_json()["ok"]
    assert client.post("/api/tools/runs", json={**base, "run_id": "r1-2", "build_active": True}).get_json()["ok"]
    conn = manager_mod.get_db()
    assert tuple(conn.execute("SELECT COUNT(*), MAX(build_active) FROM tool_runs").fetchone()) == (1, 0)
    assert client.get("/api/llm/autotune/status").get_json()["items"] == []


def test_build_active_column_backfills_from_the_summary(tmp_path, monkeypatch):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    conn.execute("CREATE TABLE tool_runs (id INTEGER PRIMARY KEY, tool TEXT NOT NULL, model_id TEXT NOT NULL,"
                 " agent_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT 'llama',"
                 " ok INTEGER NOT NULL DEFAULT 1, summary TEXT, ts TEXT NOT NULL, run_id TEXT, conditions TEXT)")
    for rid, summary in (("a", '{"build_active": true}'), ("b", '{"build_active": false}'),
                         ("c", '{"x": 1}'), ("d", "not json"), ("e", None)):
        conn.execute("INSERT INTO tool_runs (tool, model_id, summary, ts, run_id) VALUES ('autotune', 'm', ?, 't', ?)",
                     (summary, rid))
    conn.commit()
    monkeypatch.setattr(manager_mod, "get_db", lambda: conn)
    manager_mod.init_db()
    got = dict(conn.execute("SELECT run_id, build_active FROM tool_runs").fetchall())
    assert got == {"a": 1, "b": 0, "c": None, "d": None, "e": None}


# #917: the route scopes to the host the cards show — explicit agent_id, else the resolved default host.
def test_status_scopes_to_the_resolved_default_host(client, monkeypatch):
    conn = manager_mod.get_db()
    _row(conn, "a1", "org/m:Q4", True, {"llama_build": "b100"}, "2026-09-05T00:00:00Z")
    _row(conn, "a2", "org/m:Q4", True, {"llama_build": "b120"}, "2026-09-01T00:00:00Z")
    _row(conn, "a2", "org/n:Q4", True, {"llama_build": "b120"}, "2026-09-02T00:00:00Z")
    conn.commit()
    monkeypatch.setattr(manager_mod, "_llama_build_of", lambda aid: "b120")
    monkeypatch.setattr(manager_mod, "_request_agent", lambda kind: {"agent_id": "a2"} if kind == "llama" else None)
    items = client.get("/api/llm/autotune/status").get_json()["items"]
    assert sorted((i["agent_id"], i["model_id"]) for i in items) == [("a2", "org/m:Q4"), ("a2", "org/n:Q4")]
    # An older tune on the default host is not shadowed by a newer one elsewhere.
    assert [i["llama_build"] for i in items if i["model_id"] == "org/m:Q4"] == ["b120"]
    items = client.get("/api/llm/autotune/status?agent_id=a1&model_id=org/m:Q4").get_json()["items"]
    assert [(i["agent_id"], i["model_id"]) for i in items] == [("a1", "org/m:Q4")]
    assert client.get("/api/llm/autotune/status?model_id=org/zzz").get_json()["items"] == []


# #918: the real build lookup against a heartbeat with no llama.cpp build reports both unknowns as null.
def test_status_null_build_path_uses_the_real_lookup(client):
    import provider_state
    conn = manager_mod.get_db()
    _row(conn, "nb", "org/m:Q4", True, {"llama_build": "b100"}, "2026-09-05T00:00:00Z")
    _row(conn, "wb", "org/m:Q4", True, {"llama_build": "b100"}, "2026-09-05T00:00:01Z")
    conn.commit()
    provider_state.STORE.put("llama", "nb", {"llama": {"state": "running"}})
    provider_state.STORE.put("llama", "wb", {"llama": {"state": "running", "build": "b100"}})
    try:
        by = {i["agent_id"]: i for i in client.get("/api/llm/autotune/status").get_json()["items"]}
    finally:
        provider_state.STORE.evict("nb")
        provider_state.STORE.evict("wb")
    assert by["nb"]["current_build"] is None and by["nb"]["stale"] is None
    assert by["wb"]["current_build"] == "b100" and by["wb"]["stale"] is False
    by = {i["agent_id"]: i for i in client.get("/api/llm/autotune/status").get_json()["items"]}
    assert by["wb"]["current_build"] is None and by["wb"]["stale"] is None   # host gone from the store
