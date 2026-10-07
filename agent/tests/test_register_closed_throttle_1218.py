"""#1218: while the manager answers 503 to a registration, the agent logs one
reminder every five minutes instead of a warning every 15 seconds."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "llm-systems-agent.py").read_text()


def _register_loop() -> str:
    m = re.search(r"^def registry_register_blocking\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, "could not extract registry_register_blocking()"
    body = m.group(0)
    return body[:body.index("while not _state.get(\"approved\"):")]


def test_a_503_is_throttled_to_one_line_per_five_minutes():
    loop = _register_loop()
    branch = loop[loop.index("elif r.status_code == 503:"):]
    branch = branch[:branch.index("else:")]
    assert "_register_closed_last" in branch
    assert ">= 300" in branch
    assert "logger.warning" in branch
    assert re.search(r"^_register_closed_last = 0\.0$", SRC, re.MULTILINE)


def test_other_rejections_still_warn_every_time():
    loop = _register_loop()
    rest = loop[loop.index("else:", loop.index("elif r.status_code == 503:")):]
    assert "registration rejected" in rest
    assert "_register_closed_last" not in rest.split("except Exception")[0]
