"""#468: bench driver timing from canned SSE streams."""
from __future__ import annotations

import json

import pytest

import report_card as rc


def _fake_stream(chunks_with_delays, clock, seen=None):
    """Yields SSE lines, advancing the injected fake clock per chunk."""
    def post(url, payload):
        if seen is not None:
            seen.append((url, payload))
        assert payload["stream"] is True
        assert payload["max_tokens"] == rc.GEN_TOKENS
        for delay, tok in chunks_with_delays:
            clock["t"] += delay
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"content": tok}}], "usage": None})
        yield "data: [DONE]"
    return post


def test_bench_stream_times_ttft_and_generation():
    clock = {"t": 100.0}
    # first token after 0.5s, then 3 tokens at 0.1s each
    post = _fake_stream([(0.5, "a"), (0.1, "b"), (0.1, "c"), (0.1, "d")], clock)
    rep = rc.bench_stream("http://x/openai", "m", post, now=lambda: clock["t"])
    assert abs(rep["ttft_s"] - 0.5) < 1e-9
    assert rep["gen_tokens"] == 4
    assert abs(rep["gen_duration_s"] - 0.3) < 1e-9
    assert rep["prompt_tokens"] == len(rc.PROMPT_CORPUS) // 4


def test_bench_stream_posts_to_chat_completions():
    clock = {"t": 0.0}
    seen = []
    post = _fake_stream([(0.1, "a")], clock, seen)
    rc.bench_stream("http://x/openai/", "mymodel", post, now=lambda: clock["t"])
    url, payload = seen[0]
    assert url == "http://x/openai/chat/completions"
    assert payload["model"] == "mymodel"
    assert payload["messages"][0]["content"] == rc.PROMPT_CORPUS


def test_bench_stream_ignores_non_data_and_empty_deltas():
    clock = {"t": 0.0}

    def post(url, payload):
        yield ": keepalive"
        yield ""
        clock["t"] += 0.2
        yield "data: " + json.dumps({"choices": [{"delta": {}}]})
        clock["t"] += 0.3
        yield "data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]})
        yield "data: [DONE]"

    rep = rc.bench_stream("http://x/openai", "m", post, now=lambda: clock["t"])
    assert rep["gen_tokens"] == 1
    assert abs(rep["ttft_s"] - 0.5) < 1e-9


def test_bench_stream_raises_when_no_tokens_streamed():
    def post(url, payload):
        yield "data: [DONE]"

    with pytest.raises(RuntimeError, match="no tokens"):
        rc.bench_stream("http://x/openai", "m", post, now=lambda: 0.0)


def test_run_bench_discards_warmup_and_reports_medians():
    clock = {"t": 0.0}
    post = _fake_stream([(0.5, "a"), (0.1, "b")], clock)
    calls = []
    out = rc.run_bench("http://x/openai", "m", post, now=lambda: clock["t"],
                       progress_cb=lambda ev: calls.append(ev))
    assert len(out["reps"]) == rc.REPS          # warmup not included
    assert out["gen_tps"] > 0 and out["ttft_s"] > 0
    assert any(ev["phase"] == "warmup" for ev in calls)
    assert sum(1 for ev in calls if ev["phase"] == "rep") == rc.REPS
