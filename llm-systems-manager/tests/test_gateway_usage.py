"""#496: gateway-observed token usage counters + SSE usage tap."""
from __future__ import annotations

import importlib

import gateway_usage as gu


AID = "a" * 32


def setup_function(_fn):
    importlib.reload(gu)


def test_usage_from_json_bytes():
    body = b'{"id":"x","usage":{"prompt_tokens":7,"completion_tokens":5}}'
    assert gu.usage_from_json_bytes(body) == (7, 5)
    assert gu.usage_from_json_bytes(b"not json") is None
    assert gu.usage_from_json_bytes(b'{"usage":null}') is None
    assert gu.usage_from_json_bytes(b'{"usage":{}}') is None


def test_record_and_counters_accumulate():
    gu.record(AID, 7, 5)
    gu.record(AID, 3, 10)
    assert gu.counters() == {AID: {"gen": 15, "prompt": 10}}


def test_record_ignores_empty():
    gu.record("", 7, 5)
    gu.record(AID, 0, 0)
    gu.record(AID, None, None)
    assert gu.counters() == {}


def test_tap_sse_captures_final_usage_across_chunk_splits():
    sse = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":12,'
           b'"completion_tokens":34}}\n\n'
           b'data: [DONE]\n\n')
    # Awkward split boundaries, including mid-JSON.
    chunks = [sse[i:i + 17] for i in range(0, len(sse), 17)]
    got = []
    out = b"".join(gu.tap_sse(iter(chunks),
                              lambda p, g: got.append((p, g))))
    assert out == sse
    assert got == [(12, 34)]


def test_tap_sse_without_usage_reports_nothing():
    chunks = [b'data: {"choices":[{"delta":{}}]}\n\n', b"data: [DONE]\n\n"]
    got = []
    b"".join(gu.tap_sse(iter(chunks), lambda p, g: got.append((p, g))))
    assert got == []


def test_tap_sse_str_chunks_pass_through():
    chunks = ['data: {"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n']
    got = []
    out = list(gu.tap_sse(iter(chunks), lambda p, g: got.append((p, g))))
    assert out == chunks and got == [(1, 2)]


def test_tap_sse_close_propagates_to_inner_generator():
    closed = []

    def inner():
        try:
            while True:
                yield b"data: x\n\n"
        finally:
            closed.append(True)

    src = inner()
    tapped = gu.tap_sse(src, lambda p, g: None)
    next(tapped)
    # Hold a reference to src so refcount GC can't mask a missing close.
    tapped.close()
    assert closed == [True]


def test_tap_sse_strip_removes_bare_usage_event_but_records():
    sse = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
           b'data: {"choices":[],"usage":{"prompt_tokens":12,'
           b'"completion_tokens":34}}\n\n'
           b'data: [DONE]\n\n')
    chunks = [sse[i:i + 13] for i in range(0, len(sse), 13)]
    got = []
    out = b"".join(gu.tap_sse(iter(chunks),
                              lambda p, g: got.append((p, g)),
                              strip_usage=True))
    assert got == [(12, 34)]
    assert b'"usage"' not in out
    assert b'"content":"hi"' in out and b"[DONE]" in out


def test_tap_sse_strip_keeps_usage_on_content_chunks():
    # A usage field on a chunk that still carries choices must pass through.
    sse = (b'data: {"choices":[{"delta":{"content":"x"}}],'
           b'"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
           b'data: [DONE]\n\n')
    got = []
    out = b"".join(gu.tap_sse(iter([sse]),
                              lambda p, g: got.append((p, g)),
                              strip_usage=True))
    assert out == sse and got == [(1, 2)]
