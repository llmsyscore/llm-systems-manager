"""#453: the cheroot flush patch must only silence close/GC-time flush noise.
Live write errors (EPIPE on a disconnected SSE client) must propagate so the
stream generator dies and its pool slot frees; swallowing them held every
slot until stream_max_lifetime_s."""
import queue
import socket
import threading
import time

import pytest

import manager_mod  # noqa: F401  # side effect: applies the cheroot patch
from cheroot import makefile


class _RaisingRaw:
    """Raw stub whose write always fails like a disconnected peer."""

    closed = False

    def __init__(self, exc):
        self.exc = exc
        self.raw_closed = False

    def write(self, b):
        raise self.exc

    def writable(self):
        return True

    def seekable(self):
        return False

    def readable(self):
        return False

    def close(self):
        self.raw_closed = True

    def flush(self):
        pass


def _writer(raw):
    w = makefile.BufferedWriter.__new__(makefile.BufferedWriter)
    import _pyio
    _pyio.BufferedWriter.__init__(w, raw)
    return w


def test_live_write_error_propagates():
    w = _writer(_RaisingRaw(BrokenPipeError(32, "Broken pipe")))
    with pytest.raises(OSError):
        w.write(b": keepalive\n\n")


def test_close_time_flush_noise_still_swallowed():
    raw = _RaisingRaw(OSError(9, "Bad file descriptor"))
    w = _writer(raw)
    w._write_buf.extend(b"leftover")
    w.close()  # must not raise, and must still close the raw fd
    assert raw.raw_closed


def test_sse_slot_released_on_client_disconnect():
    """End-to-end on a real cheroot server: closing the client socket frees
    the stream slot within seconds, not stream_max_lifetime_s."""
    from flask import Flask, Response, stream_with_context
    from cheroot.wsgi import Server

    app = Flask(__name__)
    state = {"active": 0}

    def gen():
        q = queue.Queue()
        try:
            yield "data: hello\n\n"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    q.get(timeout=0.25)
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            pass

    @app.route("/stream")
    def stream():
        state["active"] += 1
        resp = Response(stream_with_context(gen()),
                        mimetype="text/event-stream")
        resp.call_on_close(
            lambda: state.__setitem__("active", state["active"] - 1))
        return resp

    srv = Server(("127.0.0.1", 0), app, numthreads=4)
    srv.prepare()
    threading.Thread(target=srv.serve, daemon=True).start()
    port = srv.bind_addr[1]
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(b"GET /stream HTTP/1.1\r\nHost: x\r\n\r\n")
        sock.recv(4096)
        deadline = time.monotonic() + 5
        while state["active"] != 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert state["active"] == 1
        sock.close()
        deadline = time.monotonic() + 10
        while state["active"] != 0 and time.monotonic() < deadline:
            time.sleep(0.25)
        assert state["active"] == 0, \
            "slot not released within 10s of client disconnect"
    finally:
        srv.stop()
