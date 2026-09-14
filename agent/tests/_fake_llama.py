"""Scriptable stand-in for llama-server: /v1/models, /props, /health, /metrics, /slots, /models/sse."""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeLlama:
    """Ephemeral-port llama-server double; `router=False` omits the per-model status objects."""

    def __init__(self, models: dict[str, str], sleeping=(), router: bool = True):
        self.models = dict(models)
        self.sleeping = set(sleeping)
        self.router = router
        self.requests: list[str] = []
        self._sse: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._stopped = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                with fake._lock:
                    fake.requests.append(self.path)
                    models = dict(fake.models)
                    asleep = set(fake.sleeping)
                if u.path in ("/v1/models", "/models"):
                    data = []
                    for mid, st in models.items():
                        e = {"id": mid, "object": "model"}
                        if fake.router:
                            e["status"] = {"value": st}
                        data.append(e)
                    self._json(200, {"object": "list", "data": data})
                    return
                if u.path == "/props":
                    mid = q.get("model")
                    if fake.router and not mid:
                        self._json(200, {"build_info": "b1"})
                        return
                    self._json(200, {"build_info": "b1", "total_slots": 1,
                                     "is_sleeping": (mid or next(iter(models), "")) in asleep})
                    return
                if u.path == "/health":
                    self._json(200, {"status": "ok"})
                    return
                if u.path in ("/metrics", "/slots"):
                    self._json(200, [] if u.path == "/slots" else {})
                    return
                if u.path == "/models/sse":
                    qq: queue.Queue = queue.Queue()
                    with fake._lock:
                        fake._sse.append(qq)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        while True:
                            item = qq.get()
                            if item is None:
                                return
                            self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                self._json(404, {"error": "nope"})

            def do_POST(self):
                self._json(200, {"ok": True})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"

    def set_status(self, model: str, status: str) -> None:
        with self._lock:
            self.models[model] = status

    def set_sleeping(self, model: str, flag: bool) -> None:
        with self._lock:
            (self.sleeping.add if flag else self.sleeping.discard)(model)

    def paths(self, prefix: str = "") -> list[str]:
        with self._lock:
            return [p for p in self.requests if p.startswith(prefix)]

    def sse_push(self, model: str, status: str) -> None:
        with self._lock:
            for qq in self._sse:
                qq.put({"type": "model_status", "model": model, "status": status})

    def down(self) -> None:
        self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            for qq in self._sse:
                qq.put(None)
        self._srv.shutdown()
        self._srv.server_close()
