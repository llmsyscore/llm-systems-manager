"""#1226: with the agent-security switch off, only read-only routes skip the token check;
everything that changes the agent or its providers still requires it."""
from __future__ import annotations

import hmac
import re
from pathlib import Path

import pytest

AGENT_PY = Path(__file__).resolve().parents[1] / "llm-systems-agent.py"
SRC = AGENT_PY.read_text()


class _HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code


def _extract(name: str) -> str:
    m = re.search(rf"^(?:async )?def {name}\(.*?(?=^\S)", SRC, re.MULTILINE | re.DOTALL)
    assert m, f"could not extract {name}()"
    return m.group(0)


def _check_bearer(switch_off: bool):
    ns = {"_state": {"auth_disabled_global": switch_off}, "_token_provider": lambda: "tok",
          "HTTPException": _HTTPException, "hmac": hmac, "Optional": object}
    exec(compile(_extract("_check_bearer"), str(AGENT_PY), "exec"), ns)
    return ns["_check_bearer"]


def test_switch_off_waives_only_relaxed_calls():
    chk = _check_bearer(True)
    chk(None, relaxed=True)
    chk("Bearer nope", relaxed=True)
    chk("Bearer tok")
    with pytest.raises(_HTTPException) as missing:
        chk(None)
    assert missing.value.status_code == 401
    with pytest.raises(_HTTPException) as wrong:
        chk("Bearer nope")
    assert wrong.value.status_code == 403


def test_switch_on_checks_every_call():
    chk = _check_bearer(False)
    with pytest.raises(_HTTPException):
        chk(None, relaxed=True)
    chk("Bearer tok", relaxed=True)


def _route_body(decorator: str) -> str:
    start = SRC.index(decorator)
    end = SRC.find("\n@app.", start + 1)
    return SRC[start:end if end > 0 else len(SRC)]


@pytest.mark.parametrize("decorator", [
    '@app.get("/status")', '@app.get("/metrics")', '@app.get("/config")', '@app.get("/agent/config-file")',
    '@app.get("/agent/log/tail")', '@app.get("/agent/log/stream")', '@app.get("/openclaw/aggregate")',
])
def test_read_only_routes_are_the_relaxed_ones(decorator):
    assert "_check_bearer(authorization, relaxed=True)" in _route_body(decorator)


@pytest.mark.parametrize("decorator", [
    '@app.post("/agent/restart")', '@app.post("/agent/self-update")', '@app.post("/config/reload")',
    '@app.post("/agent/collection")', '@app.put("/agent/config-file")',
])
def test_routes_that_change_the_agent_always_check_the_token(decorator):
    body = _route_body(decorator)
    assert "_check_bearer(authorization)" in body and "relaxed=True" not in body


def test_provider_routes_get_the_strict_check():
    assert SRC.count("check_bearer=_check_bearer,") == 2
    assert SRC.count("_check_bearer(authorization, relaxed=True)") == 7
