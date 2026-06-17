# llm-systems-manager/tests/test_llama_state_build_method.py
"""#73: /api/llama-state surfaces the agent-reported build_method."""
from __future__ import annotations

import manager_mod as M


def test_payload_includes_build_method(monkeypatch):
    wrapper = {"sample": {"llama": {"state": "awake", "model": "x", "build_method": "homebrew"}},
               "last_seen": 0.0}
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda kind, aid: wrapper)
    payload = M._build_llama_state_payload("agent-123")
    assert payload["build_method"] == "homebrew"


def test_payload_build_method_absent_is_none(monkeypatch):
    wrapper = {"sample": {"llama": {"state": "awake"}}, "last_seen": 0.0}
    monkeypatch.setattr(M.provider_state.STORE, "get", lambda kind, aid: wrapper)
    payload = M._build_llama_state_payload("agent-123")
    assert payload["build_method"] is None
