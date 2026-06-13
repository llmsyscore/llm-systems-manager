"""
Unit tests for stream_pool — the global cap on concurrent long-lived SSE
streams that keeps them from exhausting the synchronous Cheroot worker pool.

Each held SSE response pins one worker for the stream's whole life; a healthy
agent stream (keepalives ≤30s) is never reaped by the read timeout, so without
a cap enough concurrent streams starve control requests and the manager hangs.
These tests pin the acquire/release/guard contract that prevents that.
"""
from __future__ import annotations

import pytest

import stream_pool


@pytest.fixture
def pool(monkeypatch):
    """A fresh pool with a small, deterministic limit (3)."""
    p = stream_pool.StreamPool()
    monkeypatch.setattr(p, "limit", lambda: 3)
    return p


class TestAcquireRelease:
    def test_acquires_up_to_limit_then_refuses(self, pool):
        assert pool.try_acquire() is True
        assert pool.try_acquire() is True
        assert pool.try_acquire() is True
        assert pool.active() == 3
        # Over the limit — refused, count unchanged.
        assert pool.try_acquire() is False
        assert pool.active() == 3

    def test_release_frees_a_slot(self, pool):
        for _ in range(3):
            assert pool.try_acquire() is True
        assert pool.try_acquire() is False
        pool.release()
        assert pool.active() == 2
        assert pool.try_acquire() is True
        assert pool.active() == 3

    def test_release_never_goes_negative(self, pool):
        pool.release()
        pool.release()
        assert pool.active() == 0
        assert pool.try_acquire() is True
        assert pool.active() == 1


class TestLimitFromConfig:
    def test_limit_is_threads_minus_reserve(self, monkeypatch):
        p = stream_pool.StreamPool()
        monkeypatch.setattr(stream_pool.settings.manager, "http_threads", 64,
                            raising=False)
        monkeypatch.setattr(stream_pool.settings.manager,
                            "stream_reserve_threads", 24, raising=False)
        assert p.limit() == 40

    def test_limit_floors_at_one(self, monkeypatch):
        p = stream_pool.StreamPool()
        monkeypatch.setattr(stream_pool.settings.manager, "http_threads", 8,
                            raising=False)
        monkeypatch.setattr(stream_pool.settings.manager,
                            "stream_reserve_threads", 100, raising=False)
        assert p.limit() == 1


class TestReleaseOnResponseClose:
    """Sites release the slot via Flask Response.call_on_close(POOL.release),
    which fires when the WSGI server closes the response — whether or not the
    body generator was ever iterated (a generator-finally leaks a slot when the
    client disconnects before the first byte). This pins that contract."""

    def test_call_on_close_releases_slot(self, pool):
        from werkzeug.wrappers import Response

        assert pool.try_acquire() is True
        resp = Response("snapshot")
        resp.call_on_close(pool.release)
        assert pool.active() == 1
        resp.close()                      # WSGI server closes the response
        assert pool.active() == 0

    def test_call_on_close_releases_without_iterating_body(self, pool):
        """Client disconnects before any chunk is pulled — close() still
        runs the callback, so the slot is freed."""
        from werkzeug.wrappers import Response

        def gen():
            yield b"never pulled"

        assert pool.try_acquire() is True
        resp = Response(gen(), mimetype="text/event-stream")
        resp.call_on_close(pool.release)
        assert pool.active() == 1
        resp.close()                      # body generator never advanced
        assert pool.active() == 0


class TestControlStaysResponsiveWhenStreamsSaturate:
    """The bug: held SSE streams consumed every Cheroot worker, so control
    requests (/health, heartbeat) got none and the manager hung. This proves
    the limiter keeps streams below the pool so control always answers."""

    def _app(self, pool):
        from flask import Flask, Response, jsonify

        app = Flask(__name__)

        @app.route("/sse")
        def sse():
            if not pool.try_acquire():
                return jsonify(error="manager at stream capacity; retry shortly"), 503
            resp = Response(iter((b"data: hi\n\n",)), mimetype="text/event-stream")
            resp.call_on_close(pool.release)
            return resp

        @app.route("/health")
        def health():
            return jsonify(ok=True)

        return app

    def test_streams_get_503_over_cap_control_stays_200(self, pool):
        client = self._app(pool).test_client()
        # Fill the (limit=3) stream pool with held streams — don't close them.
        held = [client.get("/sse") for _ in range(3)]
        assert all(r.status_code == 200 for r in held)
        assert pool.active() == 3
        # Next stream is refused, NOT queued behind a full pool.
        assert client.get("/sse").status_code == 503
        # Control request still answered — this is the whole point of the fix.
        assert client.get("/health").status_code == 200
        # Closing a held stream frees a slot for the next one.
        held[0].close()
        assert pool.active() == 2
        assert client.get("/sse").status_code == 200
