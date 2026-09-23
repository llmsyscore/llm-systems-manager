"""Timings shim for LM Studio benchmarks (#916): a loopback proxy in front of LM Studio that answers
the unmodified speed-bench script with llama-server-style `timings`. Single-turn requests on LM Studio
0.4+ go to the native /api/v1/chat and carry its server-side stats; multi-turn requests and older
LM Studio stream the OpenAI endpoint and take first-token / last-token wall-clock marks."""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import requests

log = logging.getLogger("llm-systems-agent.providers.lms_timings_shim")

CHAT_PATH = "/v1/chat/completions"
NATIVE_CHAT_PATH = "/api/v1/chat"
NATIVE_MODELS_PATH = "/api/v1/models"
STREAM_TIMEOUT_S = 900
# OpenAI sampling keys the native chat call also takes.
NATIVE_PASS = ("temperature", "top_p", "top_k", "repeat_penalty", "seed")


def _delta_text(chunk: dict) -> tuple[str, str]:
    """(content, reasoning) text carried by one streamed chunk."""
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return "", ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or ""), str(delta.get("reasoning_content") or "")


def timings(prompt_tokens: int, completion_tokens: int, t_start: float, t_first: Optional[float], t_end: float) -> dict:
    """llama-server `timings` block from wall-clock marks: prefill ends at the first token, decode runs to the last."""
    first = t_first if t_first is not None else t_end
    prompt_s = max(first - t_start, 1e-6)
    decode_s = max(t_end - first, 1e-6)
    out = {"prompt_n": int(prompt_tokens), "prompt_ms": round(prompt_s * 1000.0, 3),
           "prompt_per_second": round(prompt_tokens / prompt_s, 3) if prompt_tokens else None,
           "predicted_n": int(completion_tokens), "predicted_ms": round(decode_s * 1000.0, 3),
           "predicted_per_second": round(completion_tokens / decode_s, 3) if completion_tokens else None}
    return out


def collect_stream(lines, t_start: float, clock=time.perf_counter) -> dict:
    """Folds an SSE line iterator into one non-streamed completion with `timings` and `usage`."""
    content, reasoning = [], []
    usage: dict = {}
    meta: dict = {}
    finish = None
    t_first: Optional[float] = None
    n_chunks = 0
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        raw = raw.strip()
        if not raw.startswith("data:"):
            continue
        body = raw[5:].strip()
        if body == "[DONE]":
            break
        try:
            chunk = json.loads(body)
        except ValueError:
            continue
        for k in ("id", "model", "created", "system_fingerprint"):
            if k in chunk and k not in meta:
                meta[k] = chunk[k]
        c, r = _delta_text(chunk)
        if (c or r) and t_first is None:
            t_first = clock()
        if c or r:
            n_chunks += 1
        content.append(c)
        reasoning.append(r)
        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict) and choices[0].get("finish_reason"):
            finish = choices[0]["finish_reason"]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
    t_end = clock()
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or n_chunks)
    message: dict = {"role": "assistant", "content": "".join(content)}
    if any(reasoning):
        message["reasoning_content"] = "".join(reasoning)
    return {"id": meta.get("id") or "shim", "object": "chat.completion", "created": meta.get("created") or int(time.time()),
            "model": meta.get("model"), "system_fingerprint": meta.get("system_fingerprint"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
            "usage": usage or {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                               "total_tokens": prompt_tokens + completion_tokens},
            "timings": timings(prompt_tokens, completion_tokens, t_start, t_first, t_end)}


def single_turn(payload: dict) -> Optional[dict]:
    """{system, user} when the OpenAI request is one user turn (plus an optional system prompt), else None."""
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None
    system: list[str] = []
    user: Optional[str] = None
    for m in msgs:
        if not isinstance(m, dict) or not isinstance(m.get("content"), str):
            return None
        role = m.get("role")
        if role == "system" and user is None:
            system.append(m["content"])
        elif role == "user" and user is None:
            user = m["content"]
        else:
            return None
    if user is None:
        return None
    return {"system": "\n\n".join(system), "user": user}


def native_request(payload: dict, turn: dict) -> dict:
    body: dict = {"input": turn["user"], "store": False, "stream": False}
    if payload.get("model"):
        body["model"] = payload["model"]
    if turn.get("system"):
        body["system_prompt"] = turn["system"]
    if isinstance(payload.get("max_tokens"), int):
        body["max_output_tokens"] = payload["max_tokens"]
    for k in NATIVE_PASS:
        if k in payload and payload[k] is not None:
            body[k] = payload[k]
    return body


def from_native(data: dict, payload: dict) -> dict:
    """OpenAI-shaped completion with `timings` from the native reply's server-side stats."""
    st = data.get("stats") or {}
    content, reasoning = [], []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("content")
        if isinstance(text, list):
            text = "".join(str(p.get("text") or "") for p in text if isinstance(p, dict))
        if not isinstance(text, str):
            continue
        (reasoning if item.get("type") == "reasoning" else content).append(text)
    prompt_tokens = int(st.get("input_tokens") or 0)
    completion_tokens = int(st.get("total_output_tokens") or 0)
    reasoning_tokens = int(st.get("reasoning_output_tokens") or 0)
    ttft = float(st.get("time_to_first_token_seconds") or 0.0)
    tps = float(st.get("tokens_per_second") or 0.0)
    message: dict = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    max_tokens = payload.get("max_tokens")
    finish = "length" if isinstance(max_tokens, int) and completion_tokens >= max_tokens else "stop"
    timings_block = {"prompt_n": prompt_tokens, "prompt_ms": round(ttft * 1000.0, 3),
                     "prompt_per_second": round(prompt_tokens / ttft, 3) if prompt_tokens and ttft > 0 else None,
                     "predicted_n": completion_tokens,
                     "predicted_ms": round(completion_tokens / tps * 1000.0, 3) if tps > 0 else None,
                     "predicted_per_second": round(tps, 3) if tps > 0 else None, "source": "native"}
    if st.get("model_load_time_seconds") is not None:
        timings_block["model_load_ms"] = round(float(st["model_load_time_seconds"]) * 1000.0, 3)
    return {"id": f"native-{int(time.time() * 1000)}", "object": "chat.completion", "created": int(time.time()),
            "model": data.get("model_instance_id") or payload.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens,
                      "completion_tokens_details": {"reasoning_tokens": reasoning_tokens}},
            "timings": timings_block}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        return

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.rstrip("/") != CHAT_PATH:
            self._send(404, {"error": {"message": f"shim only serves {CHAT_PATH}"}})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": {"message": "invalid json"}})
            return
        server: "Shim" = self.server.shim  # type: ignore[attr-defined]
        turn = single_turn(payload)
        if turn is not None and server.native_available():
            self._native(server, payload, turn)
            return
        payload["stream"] = True
        opts = payload.get("stream_options") if isinstance(payload.get("stream_options"), dict) else {}
        payload["stream_options"] = dict(opts, include_usage=True)
        t_start = time.perf_counter()
        try:
            resp = server.session.post(server.upstream + CHAT_PATH, json=payload, stream=True, timeout=(10, STREAM_TIMEOUT_S))
        except requests.RequestException as e:
            self._send(502, {"error": {"message": f"LM Studio unreachable: {e}"[:300]}})
            return
        if resp.status_code != 200:
            try:
                body = resp.json()
            except ValueError:
                body = {"error": {"message": resp.text[:300]}}
            self._send(resp.status_code, body)
            return
        try:
            out = collect_stream(resp.iter_lines(), t_start)
        except requests.RequestException as e:
            self._send(502, {"error": {"message": f"LM Studio stream failed: {e}"[:300]}})
            return
        finally:
            resp.close()
        server.count += 1
        server.stream_count += 1
        self._send(200, out)

    def _native(self, server: "Shim", payload: dict, turn: dict) -> None:
        """Single-turn request on LM Studio 0.4+: the native chat call reports its own stats."""
        try:
            resp = server.session.post(server.upstream + NATIVE_CHAT_PATH, json=native_request(payload, turn),
                                       timeout=(10, STREAM_TIMEOUT_S))
        except requests.RequestException as e:
            self._send(502, {"error": {"message": f"LM Studio unreachable: {e}"[:300]}})
            return
        try:
            data = resp.json()
        except ValueError:
            data = {"error": {"message": resp.text[:300]}}
        if resp.status_code != 200:
            self._send(resp.status_code, data)
            return
        server.count += 1
        server.native_count += 1
        self._send(200, from_native(data, payload))


class Shim:
    """Loopback proxy in front of one LM Studio server; `url` is what the bench script should target."""

    def __init__(self, upstream: str, native: Optional[bool] = None):
        self.upstream = upstream.rstrip("/")
        self.session = requests.Session()
        self._srv: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._native = native          # None = probe the native API on first use
        self._native_lock = threading.Lock()
        self.count = 0
        self.native_count = 0
        self.stream_count = 0

    def native_available(self) -> bool:
        """True when LM Studio answers GET /api/v1/models (0.4 or newer); probed once per shim."""
        with self._native_lock:
            if self._native is None:
                try:
                    r = self.session.get(self.upstream + NATIVE_MODELS_PATH, timeout=5)
                    self._native = r.status_code == 200
                except requests.RequestException:
                    self._native = False
            return bool(self._native)

    @property
    def mode(self) -> str:
        return "native stats" if self._native else "streaming"

    @property
    def url(self) -> str:
        if not self._srv:
            return self.upstream
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "Shim":
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv.daemon_threads = True
        srv.shim = self  # type: ignore[attr-defined]
        self._srv = srv
        self._thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True,
                                        name="lms-timings-shim")
        self._thread.start()
        return self

    def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception as e:
                log.debug("shim stop: %s", e)
        self.session.close()

    def __enter__(self) -> "Shim":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
