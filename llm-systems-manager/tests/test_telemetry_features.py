"""Daily report feature counts (#1322): integer counts per fixed key, last 24 h where windowed."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import manager_mod as m


def _db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE tool_runs (tool TEXT, ts TEXT)")
    c.execute("CREATE TABLE tower_messages (role TEXT, ts REAL)")
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=2)
    c.executemany("INSERT INTO tool_runs VALUES (?, ?)",
                  [("benchmark", now.isoformat()), ("benchmark", now.isoformat()),
                   ("autotune", now.isoformat()), ("benchmark", old.isoformat())])
    c.executemany("INSERT INTO tower_messages VALUES (?, ?)",
                  [("user", now.timestamp()), ("assistant", now.timestamp()), ("user", old.timestamp())])
    return c


class _Resp:
    def __init__(self, body, ok=True):
        self.ok, self._body = ok, body

    def json(self):
        return self._body


def _ae(rules_ok=True):
    now = datetime.now(timezone.utc)

    def get(url, timeout=None):
        if "/api/alarm/rules" in url:
            return _Resp([{"id": "a"}, {"id": "b"}, {"id": "c"}], ok=rules_ok)
        return _Resp([{"created_at": now.isoformat()},
                      {"created_at": (now - timedelta(days=3)).isoformat()},
                      {"created_at": now.replace(tzinfo=None).isoformat()},
                      {"created_at": None}])
    return SimpleNamespace(get=get)


def _wire(monkeypatch, rules_ok=True, ae_url="https://ae.example.test"):
    db = _db()
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "_alarm_engine_url", ae_url)
    monkeypatch.setattr(m, "_ae_session", _ae(rules_ok))
    monkeypatch.setattr(m.agent_registry, "load_agents", lambda: {"agents": {
        "a1": {"status": "approved", "hostname": "h1"}, "a2": {"status": "pending", "hostname": "h2"}}})
    samples = {("vllm", "a1"): {"sample": {"vllm": {"state": "running", "model": "x"}}},
               ("vllm", "a2"): {"sample": {"vllm": {"state": "running", "model": "y"}}}}
    monkeypatch.setattr(m.provider_state, "STORE", SimpleNamespace(get=lambda p, a: samples.get((p, a))))


def test_features_counts(monkeypatch):
    _wire(monkeypatch)
    f = m._telemetry_features()
    assert f == {"benchmark": 2, "autotune": 1, "quality": 0, "tower_eval": 0, "tower": 1,
                 "models": 1, "rules": 3, "alerts": 2}
    assert all(isinstance(v, int) for v in f.values())


def test_features_leave_out_what_the_alarm_engine_cannot_answer(monkeypatch):
    _wire(monkeypatch, rules_ok=False)
    f = m._telemetry_features()
    assert "rules" not in f and f["alerts"] == 2
    _wire(monkeypatch, ae_url="")
    f = m._telemetry_features()
    assert "rules" not in f and "alerts" not in f and f["benchmark"] == 2
