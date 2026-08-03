# agent/tests/test_openclaw_collectors.py
# OpenClaw collector changes (#498): per-hour/per-day buckets in the session
# parser, UTC timestamps from the sqlite/delivery collectors, cache eviction.
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

AGENT_DIR = Path(__file__).resolve().parents[1]
AGENT_PY = AGENT_DIR / "llm-systems-agent.py"

_FUNCS = ("_oc_is_session_file", "_oc_normalize_plugin_name",
          "_oc_extract_tool_plugins", "_oc_ts_to_day",
          "_oc_parse_session_file", "_oc_collect_sessions",
          "_oc_collect_flows", "_oc_collect_tasks", "_oc_collect_delivery")


def _extract_func(text: str, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^\S)", text, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}() from llm-systems-agent.py"
    return m.group(0)


def _oc_ns() -> dict:
    text = AGENT_PY.read_text()
    ns: dict = {
        "json": json, "Path": Path, "datetime": datetime, "timezone": timezone,
        "Any": Any, "Optional": Optional, "_oc_sqlite3": sqlite3,
        "logger": logging.getLogger("test"), "_oc_file_cache": {},
    }
    for fn in _FUNCS:
        exec(compile(_extract_func(text, fn), str(AGENT_PY), "exec"), ns)
    return ns


def _write_session(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")


def _msg(ts: str, tokens=(100, 50), tool: "str | None" = None, cost=0.0):
    content = []
    if tool:
        content.append({"type": "tool_use", "name": tool})
    usage: dict = {"input": tokens[0], "output": tokens[1],
                   "cacheRead": 0, "cacheWrite": 0}
    if cost:
        usage["cost"] = {"total": cost}
    return {"type": "message", "timestamp": ts,
            "message": {"role": "assistant", "model": "m1",
                        "usage": usage, "content": content}}


def test_parser_buckets_tokens_per_hour_and_tools_per_day(tmp_path):
    ns = _oc_ns()
    f = tmp_path / "sess1.jsonl"
    _write_session(f, [
        _msg("2026-08-03T10:05:00.000Z", tokens=(100, 50), tool="bash"),
        _msg("2026-08-03T10:45:00.000Z", tokens=(200, 100)),
        _msg("2026-08-03T11:10:00.000Z", tokens=(10, 5), tool="bash"),
        _msg("2026-08-02T09:00:00.000Z", tokens=(1, 1), tool="read"),
    ])
    agg = ns["_oc_parse_session_file"](f)
    assert agg["hourly"] == {
        "2026-08-03T10": {"input": 300, "output": 150},
        "2026-08-03T11": {"input": 10, "output": 5},
        "2026-08-02T09": {"input": 1, "output": 1},
    }
    assert agg["daily_tools"] == {
        "2026-08-03": {"bash": 2},
        "2026-08-02": {"read": 1},
    }
    assert agg["tool_breakdown"] == agg["tools"] == {"bash": 2, "read": 1}


def test_parser_caps_hourly_history(tmp_path):
    ns = _oc_ns()
    f = tmp_path / "sess2.jsonl"
    _write_session(f, [_msg(f"2026-07-{d:02d}T{h:02d}:00:00.000Z")
                       for d in range(1, 4) for h in range(0, 24)])
    agg = ns["_oc_parse_session_file"](f)
    assert len(agg["hourly"]) == 48
    assert min(agg["hourly"]) == "2026-07-02T00"  # oldest day pruned


def test_collect_sessions_evicts_deleted_files(tmp_path):
    ns = _oc_ns()
    sess_dir = tmp_path / "agents" / "main" / "sessions"
    sess_dir.mkdir(parents=True)
    a, b = sess_dir / "a.jsonl", sess_dir / "b.jsonl"
    _write_session(a, [_msg("2026-08-03T10:00:00.000Z")])
    _write_session(b, [_msg("2026-08-03T10:00:00.000Z")])
    out = ns["_oc_collect_sessions"](tmp_path / "agents")
    assert len(out) == 2 and len(ns["_oc_file_cache"]) == 2
    b.unlink()
    out = ns["_oc_collect_sessions"](tmp_path / "agents")
    assert len(out) == 1
    assert list(ns["_oc_file_cache"]) == [str(a)]


def _utc_offset_ok(iso: str) -> bool:
    return iso.endswith("+00:00") or iso.endswith("Z")


def test_flows_and_tasks_emit_utc_timestamps(tmp_path):
    ns = _oc_ns()
    flows_db = tmp_path / "registry.sqlite"
    conn = sqlite3.connect(flows_db)
    conn.execute("CREATE TABLE flow_runs (flow_id TEXT, status TEXT, goal TEXT,"
                 " created_at INTEGER, ended_at INTEGER, owner_key TEXT)")
    conn.execute("INSERT INTO flow_runs VALUES ('f1','succeeded','g',"
                 "1754000000000, 1754000005000, 'o')")
    conn.commit(); conn.close()
    flows = ns["_oc_collect_flows"](flows_db)
    assert _utc_offset_ok(flows["recent"][0]["created_iso"])

    tasks_db = tmp_path / "runs.sqlite"
    conn = sqlite3.connect(tasks_db)
    conn.execute("CREATE TABLE task_runs (task_id TEXT, label TEXT,"
                 " task_kind TEXT, runtime TEXT, error TEXT,"
                 " created_at INTEGER, status TEXT,"
                 " started_at INTEGER, ended_at INTEGER)")
    conn.execute("INSERT INTO task_runs VALUES ('t1','l','k','node','boom',"
                 "1754000000000,'failed',NULL,NULL)")
    conn.commit(); conn.close()
    tasks = ns["_oc_collect_tasks"](tasks_db)
    assert _utc_offset_ok(tasks["recent_failures"][0]["created_iso"])


def test_delivery_emits_utc_oldest_enqueue(tmp_path):
    ns = _oc_ns()
    dq = tmp_path / "failed"
    dq.mkdir()
    (dq / "m1.json").write_text(json.dumps(
        {"channel": "discord", "retryCount": 2, "enqueuedAt": 1754000000000,
         "lastError": "timeout: x"}), encoding="utf-8")
    out = ns["_oc_collect_delivery"](dq)
    assert out["total"] == 1
    assert _utc_offset_ok(out["oldest_enqueue_iso"])
