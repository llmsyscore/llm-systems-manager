# llm-systems-manager/tests/test_fleet_wiring.py
"""Final-review fix: _fleet_run_on_agent/_fleet_cancel_on_agent wiring (#884, #885)."""
from __future__ import annotations

import manager_mod

AID = "a" * 32


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


def _agent():
    return {"agent_id": AID, "token": "t", "hostname": "alpha"}


def test_ok_reply_without_run_id_is_treated_as_a_failure(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", lambda aid, capability=None: _agent())
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request",
                        lambda *a, **k: (_Resp({"ok": True}), None, None))
    started = []
    monkeypatch.setattr(manager_mod.tool_activity, "note_start", lambda *a, **k: started.append(a))
    ok, val = manager_mod._fleet_run_on_agent(AID, {"model_id": "m"})
    assert ok is False
    assert val == "agent returned no run id"
    assert started == []


def test_ok_reply_with_run_id_succeeds_and_notes_start(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", lambda aid, capability=None: _agent())
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request",
                        lambda *a, **k: (_Resp({"ok": True, "run_id": "x"}), None, None))
    started = []
    monkeypatch.setattr(manager_mod.tool_activity, "note_start", lambda *a, **k: started.append(a))
    ok, val = manager_mod._fleet_run_on_agent(AID, {"model_id": "m"})
    assert (ok, val) == (True, "x")
    assert started == [(AID, "llama", "benchmark")]


def test_unreachable_agent_reply_is_a_failure(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", lambda aid, capability=None: _agent())
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request",
                        lambda *a, **k: (None, None, "connection refused"))
    ok, val = manager_mod._fleet_run_on_agent(AID, {"model_id": "m"})
    assert ok is False
    assert val == "connection refused"


def test_cancel_resolves_the_agent_with_the_same_capability_as_run(monkeypatch):
    seen = []

    def _resolve(aid, capability=None):
        seen.append(capability)
        return _agent()

    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", _resolve)
    monkeypatch.setattr(manager_mod.agent_registry, "agent_request",
                        lambda *a, **k: (_Resp({"ok": True}), None, None))

    manager_mod._fleet_run_on_agent(AID, {"model_id": "m"})
    manager_mod._fleet_cancel_on_agent(AID)
    assert len(seen) == 2 and seen[0] == seen[1]


def test_cancel_returns_false_for_an_unknown_agent(monkeypatch):
    monkeypatch.setattr(manager_mod.agent_registry, "resolve_agent_by_id", lambda aid, capability=None: None)
    assert manager_mod._fleet_cancel_on_agent(AID) is False
