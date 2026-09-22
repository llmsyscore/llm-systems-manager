"""#910: LM Studio native API helpers — cached model list, thinking options, the OpenAI-shaped chunk view."""
from __future__ import annotations

import pytest

import agent_registry
import lms_native

AGENT = {"agent_id": "a" * 32, "hostname": "mac", "token": "t"}
ROWS = [{"key": "qwen3.5-9b@q6_k", "capabilities": {"reasoning": {"allowed_options": ["off", "on"], "default": "on"}}},
        {"key": "qwen3.5-9b@q4_k_m", "capabilities": {"reasoning": {"allowed_options": ["off", "on"]}}},
        {"key": "gpt-oss-20b", "capabilities": {"reasoning": {"allowed_options": ["low", "medium", "high"]}}},
        {"key": "solo-7b@q8_0", "capabilities": {}}]


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload = status, payload
    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    lms_native.forget()
    lms_native.wire(lambda model_id: [AGENT])
    yield
    lms_native.forget()


def test_models_are_fetched_once_per_ttl_and_a_404_means_no_native_api(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_registry, "agent_request",
                        lambda m, a, p, **kw: (calls.append(p), _Resp(200, {"models": ROWS}), [], None)[1:])
    t = [1000.0]
    assert lms_native.models(AGENT, now=lambda: t[0]) == ROWS
    assert lms_native.models(AGENT, now=lambda: t[0] + 10) == ROWS
    assert calls == ["/lms/native/models"]
    t[0] += lms_native.CACHE_TTL_S + 1
    monkeypatch.setattr(agent_registry, "agent_request", lambda m, a, p, **kw: (_Resp(404), [], None))
    assert lms_native.models(AGENT, now=lambda: t[0]) is None


def test_match_takes_the_exact_key_or_the_sole_bare_variant():
    assert lms_native.match(ROWS, "qwen3.5-9b@q6_k")["key"] == "qwen3.5-9b@q6_k"
    assert lms_native.match(ROWS, "qwen3.5-9b") is None          # two variants: ambiguous
    assert lms_native.match(ROWS, "solo-7b")["key"] == "solo-7b@q8_0"
    assert lms_native.match(ROWS, "missing") is None


def test_thinking_options_and_availability_read_through_the_agents(monkeypatch):
    monkeypatch.setattr(agent_registry, "agent_request", lambda m, a, p, **kw: (_Resp(200, {"models": ROWS}), [], None))
    assert lms_native.thinking_options("qwen3.5-9b@q6_k") == ["off", "on"]
    assert lms_native.thinking_options("gpt-oss-20b") == ["low", "medium", "high"]
    assert lms_native.thinking_options("solo-7b") is None
    assert lms_native.available("solo-7b") is True
    assert lms_native.available("missing") is False
    lms_native.forget()
    monkeypatch.setattr(agent_registry, "agent_request", lambda m, a, p, **kw: (None, [], None))
    assert lms_native.thinking_options("qwen3.5-9b@q6_k") is None
    assert lms_native.available("qwen3.5-9b@q6_k") is False


def test_reasoning_setting_maps_the_level_to_what_the_model_takes():
    assert lms_native.reasoning_setting("off", ["off", "on"]) == "off"
    assert lms_native.reasoning_setting("medium", ["off", "on"]) == "on"
    assert lms_native.reasoning_setting("medium", ["low", "medium", "high"]) == "medium"
    assert lms_native.reasoning_setting("off", ["low", "medium", "high"]) is None
    assert lms_native.reasoning_setting("high", None) is None


def test_as_chunks_gives_the_openai_shape_with_the_reasoning_count():
    events = [{"type": "chat.start"}, {"type": "reasoning.delta", "content": "hm"},
              {"type": "message.delta", "content": "Hi"}, {"type": "message.delta", "content": "."},
              {"type": "chat.end", "result": {"stats": {"input_tokens": 9, "total_output_tokens": 4, "reasoning_output_tokens": 2}}}]
    out = list(lms_native.as_chunks(events))
    deltas = [c["choices"][0]["delta"] for c in out]
    assert deltas[:3] == [{"reasoning_content": "hm"}, {"content": "Hi"}, {"content": "."}]
    assert out[-1]["usage"] == {"prompt_tokens": 9, "completion_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 2}}


def test_trim_effort_drops_only_unsupported_levels_on_think_by_default_models(monkeypatch):
    rows = ROWS + [{"key": "quiet-7b", "capabilities": {"reasoning": {"allowed_options": ["off", "on"], "default": "off"}}}]
    monkeypatch.setattr(agent_registry, "agent_request", lambda m, a, p, **kw: (_Resp(200, {"models": rows}), [], None))
    body = {"model": "qwen3.5-9b@q6_k", "reasoning_effort": "medium", "max_tokens": 9}
    assert lms_native.trim_effort("qwen3.5-9b@q6_k", body) == {"model": "qwen3.5-9b@q6_k", "max_tokens": 9}
    assert lms_native.trim_effort("qwen3.5-9b@q6_k", {"reasoning_effort": "none"}) == {"reasoning_effort": "none"}
    assert lms_native.trim_effort("gpt-oss-20b", {"reasoning_effort": "medium"}) == {"reasoning_effort": "medium"}
    assert lms_native.trim_effort("quiet-7b", {"reasoning_effort": "medium"}) == {"reasoning_effort": "medium"}
    assert lms_native.trim_effort("missing", {"reasoning_effort": "high"}) == {"reasoning_effort": "high"}
