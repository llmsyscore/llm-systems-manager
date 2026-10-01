"""Every manager → agent dial goes through agent_http(), so a locked agent's role is always required (#1161)."""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
BARE = re.compile(r"\brequests\.(get|post|put|delete|request)\((?:(?!\n\s*\n).)*?agent_tls_kwargs\(", re.DOTALL)


def test_no_bare_requests_call_dials_an_agent():
    offenders = []
    for path in sorted(BACKEND.glob("*.py")):
        for m in BARE.finditer(path.read_text()):
            line = path.read_text().count("\n", 0, m.start()) + 1
            offenders.append(f"{path.name}:{line}")
    assert offenders == []
