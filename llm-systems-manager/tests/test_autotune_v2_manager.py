"""#880: the manager proxies the autotune preflight and keeps the run/stream/cancel routes."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "backend" / "llm-systems-manager.py").read_text()


def test_preflight_route_proxies_to_the_agent():
    m = re.search(r'@app\.route\("/api/llm/autotune/preflight"\)\s*\ndef (\w+)\(\):(.*?)\n\n', SRC, re.S)
    assert m, "preflight route missing"
    body = m.group(2)
    assert 'proxy_to_primary("llama", "GET", "/llama/autotune/preflight"' in body


def test_run_route_still_notes_tool_start():
    m = re.search(r'@app\.route\("/api/llm/autotune/run", methods=\["POST"\]\)(.*?)\n\n', SRC, re.S)
    assert m and '_note_tool_start("llama", tool)' in m.group(1)
    # The quality guard shares this route but is its own tool to the gate (#887).
    assert '"quality" if (body.get("mode") or "") == "quality" else "autotune"' in m.group(1)


import json

import pytest

import manager_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(manager_mod, "_require_admin", lambda: None)
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
    conn.execute("INSERT INTO tool_runs (tool, model_id, agent_id, provider, ok, summary, ts, run_id) "
                 "VALUES ('autotune', ?, ?, 'llama', ?, ?, ?, ?)",
                 (model, agent, 1 if ok else 0, json.dumps(summary), ts, ts))


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
