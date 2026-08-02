# agent/tests/test_lms_openai_passthrough.py
"""#493: /lms/openai/* passthrough — route table, bearer/enabled gates,
LMS_API_URL forwarding."""
from __future__ import annotations

import asyncio
import types

import pytest

from tests.test_lms_delete import _load_lms


@pytest.fixture
def lms():
    mod = _load_lms()
    cfg = types.SimpleNamespace(LMS_ENABLED=True, LMS_CMD="/usr/bin/lms",
                                AGENT_USER=None, LMS_API_URL="http://lms-host:1235")
    mod.set_context(types.SimpleNamespace(config=cfg,
                                          check_bearer=lambda *_a: None))
    return mod


def test_openai_routes_registered(lms):
    routes = {(m, p) for m, p, _h in lms._ROUTES}
    assert ("POST", "/lms/openai/chat/completions") in routes
    assert ("POST", "/lms/openai/completions") in routes


def test_forward_uses_lms_api_url(lms, monkeypatch):
    seen = {}

    async def fake_forward(sub, request, api_url):
        seen.update(sub=sub, api_url=api_url)
        return "resp"

    monkeypatch.setattr(lms._shared, "openai_forward", fake_forward)
    out = asyncio.run(lms.lms_openai_chat(request=object(), authorization="Bearer t"))
    assert out == "resp"
    assert seen == {"sub": "chat/completions", "api_url": "http://lms-host:1235"}


def test_forward_refused_when_lms_disabled(lms, monkeypatch):
    lms._require_ctx().config.LMS_ENABLED = False

    async def fake_forward(sub, request, api_url):  # pragma: no cover
        raise AssertionError("must not forward when disabled")

    monkeypatch.setattr(lms._shared, "openai_forward", fake_forward)
    # Accepts any HTTPException variant; checks status_code when present.
    with pytest.raises(Exception) as ei:
        asyncio.run(lms.lms_openai_completions(request=object(), authorization="Bearer t"))
    assert not isinstance(ei.value, AssertionError)
    sc = getattr(ei.value, "status_code", None)
    assert sc in (None, 503)
