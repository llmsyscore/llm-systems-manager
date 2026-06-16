"""#67: proxied long-running op streams (build/download/bench/autotune) use the
longer lifetime cap so the manager doesn't reap them mid-operation."""
from __future__ import annotations

import time

import proxies
from manager_mod import app


class _FakeUpstream:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def iter_content(self, chunk_size=None):
        while True:
            time.sleep(0.01)
            yield b""

    def close(self):
        pass


def test_thread_pumped_terminates_at_lifetime_cap():
    gen = proxies.thread_pumped(_FakeUpstream(), "/p", keepalive_s=0.05, max_lifetime_s=0.2)
    start = time.monotonic()
    chunks = list(gen)
    assert time.monotonic() - start < 1.0
    assert any(c == b": ka\n\n" for c in chunks)


def _captured_lifetime(monkeypatch, long_running):
    captured = {}

    def fake_thread_pumped(upstream, path, *, keepalive_s=None, max_lifetime_s=None):
        captured["max_lifetime_s"] = max_lifetime_s
        return iter([b""])

    monkeypatch.setattr(proxies, "thread_pumped", fake_thread_pumped)
    monkeypatch.setattr(proxies, "_resolve_target",
                        lambda *a, **k: ({"agent_id": "abcd1234", "token": "t", "hostname": "h"}, None))
    monkeypatch.setattr(proxies.agent_registry, "agent_callback_urls", lambda a: ["http://fake"])
    monkeypatch.setattr(proxies.agent_registry, "agent_tls_kwargs", lambda url: {})
    monkeypatch.setattr(proxies.requests, "get", lambda *a, **k: _FakeUpstream())
    with app.app_context():
        proxies.proxy_stream_to_primary("llama", "/llama/build/stream", long_running=long_running)
    return captured["max_lifetime_s"]


def test_long_running_uses_op_lifetime_cap(monkeypatch):
    assert _captured_lifetime(monkeypatch, True) == proxies._STREAM_OP_MAX_LIFETIME_S


def test_default_uses_standard_lifetime_cap(monkeypatch):
    assert _captured_lifetime(monkeypatch, False) == proxies._STREAM_MAX_LIFETIME_S


def test_op_cap_exceeds_default_cap():
    assert proxies._STREAM_OP_MAX_LIFETIME_S > proxies._STREAM_MAX_LIFETIME_S
