"""#924: Flask-free gateway completions used by Tower."""
from __future__ import annotations

import json
import types

import pytest

import gateway
import gateway_usage

AGENT = {"agent_id": "a" * 32, "hostname": "box", "token": "t"}


class _Resp:
    def __init__(self, status, payload=None, lines=None, ctype="application/json"):
        self.status_code = status
        self.content = json.dumps(payload or {}).encode()
        self.headers = {"content-type": ctype}
        self._lines = lines or []
    def json(self): return json.loads(self.content)
    def iter_lines(self, decode_unicode=True): return iter(self._lines)
    def close(self): pass


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(gateway, "_gw_enabled", lambda: True)
    monkeypatch.setattr(gateway, "_provider_for_model", lambda m: "llama")
    monkeypatch.setattr(gateway, "_candidates", lambda *a, **k: [AGENT])


def test_complete_json_returns_upstream_payload_and_counts_usage(monkeypatch):
    payload = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    monkeypatch.setattr(gateway, "_forward_json", lambda agent, path, body: (_Resp(200, payload), None))
    seen = {}
    monkeypatch.setattr(gateway_usage, "client_record", lambda key, p, g: seen.update({"key": key, "p": p, "g": g}))
    out = gateway.complete_json({"model": "m", "messages": []}, label="tower")
    assert out["choices"][0]["message"]["content"] == "hi"
    assert seen["key"][0] == "tower" and (seen["p"], seen["g"]) == (3, 2)


def test_complete_json_fails_over_then_raises(monkeypatch):
    monkeypatch.setattr(gateway, "_candidates", lambda *a, **k: [AGENT, dict(AGENT, agent_id="b" * 32)])
    calls = []
    def fwd(agent, path, body):
        calls.append(agent["agent_id"][0]); return (_Resp(503), None)
    monkeypatch.setattr(gateway, "_forward_json", fwd)
    with pytest.raises(gateway.GatewayError) as ei:
        gateway.complete_json({"model": "m", "messages": []}, label="tower")
    assert ei.value.status == 503 and calls == ["a", "b"]


def test_complete_json_no_backend_is_404(monkeypatch):
    monkeypatch.setattr(gateway, "_candidates", lambda *a, **k: [])
    with pytest.raises(gateway.GatewayError) as ei:
        gateway.complete_json({"model": "m", "messages": []}, label="tower")
    assert ei.value.status == 404 and ei.value.err_type == "no_backend"


def test_complete_stream_yields_chunks_until_done(monkeypatch):
    lines = ['data: {"choices":[{"delta":{"content":"a"}}]}', '', 'data: {"choices":[{"delta":{"content":"b"}}],"usage":{"prompt_tokens":1,"completion_tokens":2}}', 'data: [DONE]']
    monkeypatch.setattr(gateway, "_dial_stream", lambda agent, path, body, read_timeout=None: _Resp(200, lines=lines, ctype="text/event-stream"))
    chunks = list(gateway.complete_stream({"model": "m", "messages": [], "stream": True}, label="tower"))
    assert [c["choices"][0]["delta"]["content"] for c in chunks] == ["a", "b"]


def test_complete_stream_non_stream_upstream_error_raises(monkeypatch):
    monkeypatch.setattr(gateway, "_dial_stream", lambda agent, path, body, read_timeout=None: _Resp(400, {"error": {"message": "bad"}}))
    with pytest.raises(gateway.GatewayError) as ei:
        list(gateway.complete_stream({"model": "m", "messages": [], "stream": True}, label="tower"))
    assert ei.value.status == 400


def _sse_lines():
    return ['data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7}}',
            'data: [DONE]']


def test_complete_stream_injects_the_usage_probe_for_counted_providers(monkeypatch):
    monkeypatch.setattr(gateway, "_provider_for_model", lambda m: "lms")
    monkeypatch.setattr(gateway, "_gw_cfg", lambda: types.SimpleNamespace(usage_probe=True))
    sent = {}
    def dial(agent, path, body, read_timeout=None):
        sent.update(body); return _Resp(200, lines=_sse_lines(), ctype="text/event-stream")
    monkeypatch.setattr(gateway, "_dial_stream", dial)
    recorded = []
    monkeypatch.setattr(gateway_usage, "record", lambda aid, p, g: recorded.append((aid, p, g)))
    monkeypatch.setattr(gateway_usage, "record_latency", lambda ms: recorded.append(("latency", ms)))
    list(gateway.complete_stream({"model": "m", "messages": []}, label="tower"))
    assert sent["stream"] is True
    assert (AGENT["agent_id"], 5, 7) in recorded
    assert any(r[0] == "latency" for r in recorded)
    assert sent["stream_options"] == {"include_usage": True}


def test_complete_stream_no_probe_and_no_per_agent_record_for_llama(monkeypatch):
    monkeypatch.setattr(gateway, "_gw_cfg", lambda: types.SimpleNamespace(usage_probe=True))
    sent = {}
    def dial(agent, path, body, read_timeout=None):
        sent.update(body); return _Resp(200, lines=_sse_lines(), ctype="text/event-stream")
    monkeypatch.setattr(gateway, "_dial_stream", dial)
    recorded = []
    monkeypatch.setattr(gateway_usage, "record", lambda aid, p, g: recorded.append((aid, p, g)))
    list(gateway.complete_stream({"model": "m", "messages": []}, label="tower"))
    assert "stream_options" not in sent
    assert recorded == []


def test_complete_stream_respects_usage_probe_off(monkeypatch):
    monkeypatch.setattr(gateway, "_provider_for_model", lambda m: "lms")
    monkeypatch.setattr(gateway, "_gw_cfg", lambda: types.SimpleNamespace(usage_probe=False))
    sent = {}
    def dial(agent, path, body, read_timeout=None):
        sent.update(body); return _Resp(200, lines=_sse_lines(), ctype="text/event-stream")
    monkeypatch.setattr(gateway, "_dial_stream", dial)
    list(gateway.complete_stream({"model": "m", "messages": []}, label="tower"))
    assert "stream_options" not in sent


def test_complete_stream_maps_a_read_failure_before_the_first_chunk_to_a_timeout(monkeypatch):
    import requests

    class _Broken(_Resp):
        def iter_lines(self, decode_unicode=True):
            raise requests.exceptions.ConnectionError("read timed out")
            yield  # pragma: no cover

    seen = {}
    def dial(agent, path, body, read_timeout=None):
        seen["read_timeout"] = read_timeout
        return _Broken(200, lines=[], ctype="text/event-stream")
    monkeypatch.setattr(gateway, "_dial_stream", dial)
    with pytest.raises(gateway.GatewayError) as ei:
        list(gateway.complete_stream({"model": "m", "messages": []}, label="tower", read_timeout=7))
    assert ei.value.err_type == "timeout" and ei.value.status == 504
    assert seen["read_timeout"] == 7


def test_complete_stream_maps_a_read_failure_mid_stream_to_upstream_dropped(monkeypatch):
    import requests

    class _Cut(_Resp):
        def iter_lines(self, decode_unicode=True):
            yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
            raise requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(gateway, "_dial_stream", lambda agent, path, body, read_timeout=None: _Cut(200, lines=[], ctype="text/event-stream"))
    gen = gateway.complete_stream({"model": "m", "messages": []}, label="tower")
    assert next(gen)["choices"][0]["delta"]["content"] == "a"
    with pytest.raises(gateway.GatewayError) as ei:
        next(gen)
    assert ei.value.err_type == "upstream" and ei.value.status == 502


def test_dial_stream_read_timeout_raises_only_when_a_read_timeout_is_requested(monkeypatch):
    import requests
    import agent_registry
    monkeypatch.setattr(agent_registry, "agent_callback_urls", lambda agent: ["https://box:8443"])
    monkeypatch.setattr(agent_registry, "agent_tls_kwargs", lambda url: {})
    monkeypatch.setattr(agent_registry, "note_dial_error", lambda *a: None)
    def post(*a, **kw):
        raise requests.exceptions.ReadTimeout("slow")
    monkeypatch.setattr(gateway.requests, "post", post)
    assert gateway._dial_stream({"token": "t"}, "/p", {}) is None
    with pytest.raises(gateway.GatewayError) as ei:
        gateway._dial_stream({"token": "t"}, "/p", {}, read_timeout=5)
    assert ei.value.err_type == "timeout"
