"""#1225: the manager has no helper that pretends to honour the agent-security switch."""
from __future__ import annotations

import agent_registry


def test_the_unused_gate_helper_is_gone():
    assert not hasattr(agent_registry, "agent_auth_gate")
    assert "agent_auth_gate" not in agent_registry.__all__
