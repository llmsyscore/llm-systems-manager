"""#916: the LM Studio timings shim turns a streamed completion into one llama-server-shaped reply with timings."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_AGENT_ROOT = Path(__file__).resolve().parents[1]
try:
    import requests  # noqa: F401
    # Another test file may have left a stub in sys.modules; only a real package can proxy.
    HAVE_REQUESTS = bool(getattr(requests, "__file__", None)) and callable(getattr(requests, "post", None))
except ImportError:                           # CI agent venv has no deps: stub it for the pure tests
    HAVE_REQUESTS = False
    import types
    sys.modules.setdefault("requests", types.SimpleNamespace(Session=object, RequestException=Exception))


@pytest.fixture(scope="module")
def shim():
    spec = importlib.util.spec_from_file_location("lms_timings_shim_real", _AGENT_ROOT / "providers" / "lms_timings_shim.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lms_timings_shim_real"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_collect_stream_builds_timings_from_first_and_last_token(shim):
    ticks = iter([1.20, 1.30, 1.40, 1.50, 1.60, 1.60])
    lines = [
        b'data: {"id":"c1","model":"m","created":5,"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
        b'data: {"id":"c1","choices":[{"index":0,"delta":{"reasoning_content":"think"},"finish_reason":null}]}',
        b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}',
        b'',
        b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"!"},"finish_reason":"stop"}]}',
        b'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":100,"completion_tokens":30,"total_tokens":130}}',
        b'data: [DONE]',
    ]
    out = shim.collect_stream(lines, t_start=1.0, clock=lambda: next(ticks))
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "Hi!", "reasoning_content": "think"}
    assert out["choices"][0]["finish_reason"] == "stop" and out["usage"]["completion_tokens"] == 30 and out["id"] == "c1"
    t = out["timings"]
    # first token at 1.20 → prefill 200 ms for 100 tokens; last mark 1.30 → decode 100 ms for 30 tokens
    assert t["prompt_n"] == 100 and t["prompt_ms"] == 200.0 and t["prompt_per_second"] == 500.0
    assert t["predicted_n"] == 30 and t["predicted_ms"] == 100.0 and t["predicted_per_second"] == 300.0


def test_collect_stream_without_usage_counts_chunks(shim):
    lines = [b'data: {"choices":[{"index":0,"delta":{"content":"a"},"finish_reason":null}]}',
             b'data: {"choices":[{"index":0,"delta":{"content":"b"},"finish_reason":"length"}]}', b'data: [DONE]']
    out = shim.collect_stream(lines, t_start=time.perf_counter())
    assert out["usage"]["completion_tokens"] == 2 and out["choices"][0]["finish_reason"] == "length"
    assert out["timings"]["predicted_n"] == 2 and out["timings"]["prompt_per_second"] is None


def test_single_turn_and_native_request_shape(shim):
    one = {"model": "m", "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}],
           "max_tokens": 64, "temperature": 0, "top_p": 0.9, "stream": False, "n": 1}
    turn = shim.single_turn(one)
    assert turn == {"system": "Be brief.", "user": "hi"}
    assert shim.native_request(one, turn) == {"input": "hi", "store": False, "stream": False, "model": "m",
                                              "system_prompt": "Be brief.", "max_output_tokens": 64, "temperature": 0, "top_p": 0.9}
    multi = {"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]}
    assert shim.single_turn(multi) is None
    assert shim.single_turn({"messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}]}) is None
    assert shim.single_turn({"messages": []}) is None


def test_from_native_maps_stats_to_timings(shim):
    data = {"model_instance_id": "qwen@q6", "output": [{"type": "reasoning", "content": "hmm"}, {"type": "message", "content": [{"type": "text", "text": "Hi!"}]}],
            "stats": {"input_tokens": 646, "total_output_tokens": 586, "reasoning_output_tokens": 100,
                      "tokens_per_second": 29.75, "time_to_first_token_seconds": 1.088, "model_load_time_seconds": 2.656}}
    out = shim.from_native(data, {"model": "qwen@q6", "max_tokens": 586})
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "Hi!", "reasoning_content": "hmm"}
    assert out["choices"][0]["finish_reason"] == "length" and out["model"] == "qwen@q6"
    assert out["usage"] == {"prompt_tokens": 646, "completion_tokens": 586, "total_tokens": 1232, "completion_tokens_details": {"reasoning_tokens": 100}}
    t = out["timings"]
    assert t["prompt_n"] == 646 and t["prompt_ms"] == 1088.0 and t["prompt_per_second"] == round(646 / 1.088, 3)
    assert t["predicted_n"] == 586 and t["predicted_per_second"] == 29.75 and t["predicted_ms"] == round(586 / 29.75 * 1000, 3)
    assert t["source"] == "native" and t["model_load_ms"] == 2656.0
    assert shim.from_native({"stats": {}}, {"max_tokens": 4})["choices"][0]["finish_reason"] == "stop"


class _FakeLmStudio(BaseHTTPRequestHandler):
    """LM Studio 0.4 stand-in: native /api/v1/models + /api/v1/chat, and a streaming OpenAI endpoint."""
    seen: list = []
    native = True

    def log_message(self, *a):
        return

    def do_GET(self):
        if self.path == "/api/v1/models" and _FakeLmStudio.native:
            body = b'{"models": []}'
            self.send_response(200)
        else:
            body = b'{"error": "nope"}'
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        _FakeLmStudio.seen.append((self.path, body))
        if self.path == "/api/v1/chat":
            out = json.dumps({"model_instance_id": body.get("model"), "output": [{"type": "message", "content": [{"type": "text", "text": "Hello"}]}],
                              "stats": {"input_tokens": 7, "total_output_tokens": 2, "reasoning_output_tokens": 0,
                                        "tokens_per_second": 40.0, "time_to_first_token_seconds": 0.5}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = ['{"id":"x","model":"m","choices":[{"index":0,"delta":{"content":"He"},"finish_reason":null}]}',
                  '{"id":"x","choices":[{"index":0,"delta":{"content":"llo"},"finish_reason":"stop"}]}',
                  '{"id":"x","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}']
        for c in chunks:
            self.wfile.write(f"data: {c}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.mark.skipif(not HAVE_REQUESTS, reason="needs requests")
def test_shim_proxies_and_answers_non_streamed(shim):
    import requests
    up = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLmStudio)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    try:
        with shim.Shim(f"http://127.0.0.1:{up.server_address[1]}") as sh:
            assert sh.url.startswith("http://127.0.0.1:") and sh.url != sh.upstream
            r = requests.post(sh.url + "/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                                                     "max_tokens": 8, "stream": False}, timeout=10)
            assert r.status_code == 200
            d = r.json()
            # single turn on a 0.4 host: the native call's own stats
            assert d["object"] == "chat.completion" and d["choices"][0]["message"]["content"] == "Hello"
            assert d["timings"]["source"] == "native" and d["timings"]["predicted_per_second"] == 40.0 and d["timings"]["prompt_ms"] == 500.0
            path, body = _FakeLmStudio.seen[-1]
            assert path == "/api/v1/chat" and body == {"input": "hi", "store": False, "stream": False, "model": "m", "max_output_tokens": 8}
            # multi-turn: streamed through the OpenAI endpoint with wall-clock marks
            r2 = requests.post(sh.url + "/v1/chat/completions", json={"model": "m", "max_tokens": 8, "messages": [
                {"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}, {"role": "user", "content": "more"}]}, timeout=10)
            d2 = r2.json()
            assert d2["choices"][0]["message"]["content"] == "Hello" and "source" not in d2["timings"] and d2["timings"]["predicted_n"] == 2
            path2, body2 = _FakeLmStudio.seen[-1]
            assert path2 == "/v1/chat/completions" and body2["stream"] is True and body2["stream_options"]["include_usage"] is True
            assert requests.post(sh.url + "/v1/other", json={}, timeout=5).status_code == 404
            assert sh.count == 2 and sh.native_count == 1 and sh.stream_count == 1 and sh.mode == "native stats"
        assert sh.url == sh.upstream        # stopped: back to the plain upstream url
        # an older LM Studio (no native API) streams every request
        _FakeLmStudio.native = False
        with shim.Shim(f"http://127.0.0.1:{up.server_address[1]}") as sh:
            d3 = requests.post(sh.url + "/v1/chat/completions", json={"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}, timeout=10).json()
            assert "source" not in d3["timings"] and sh.stream_count == 1 and sh.mode == "streaming"
    finally:
        up.shutdown()
        up.server_close()
