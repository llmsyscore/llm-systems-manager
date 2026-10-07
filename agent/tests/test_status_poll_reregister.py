"""#1201: a 404 on the approval-status poll means the manager dropped the pending
record, so the agent registers again instead of polling 404 forever."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "llm-systems-agent.py").read_text()


def _poll_loop() -> str:
    m = re.search(r"^def registry_register_blocking\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, "could not extract registry_register_blocking()"
    body = m.group(0)
    return body[body.index("while not _state.get(\"approved\"):"):]


def test_a_404_status_poll_registers_again():
    loop = _poll_loop()
    branch = loop[loop.index("elif r.status_code == 404:"):]
    branch = branch[:branch.index("else:")]
    assert "_state[\"agent_id\"] = None" in branch
    assert "return registry_register_blocking()" in branch


def test_other_poll_failures_still_only_warn():
    loop = _poll_loop()
    rest = loop[loop.index("else:"):]
    assert "registry_register_blocking()" not in rest.split("except Exception")[0]
