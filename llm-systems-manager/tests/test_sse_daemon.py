"""Phase 1 (#110): sse_daemon token verify + handler lifecycle."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

import provider_state
import sse_daemon

SECRET = b"x" * 32
PATH = "/sse/llama-state"


def _mint(agent_id, path=PATH, ttl=300, secret=SECRET):
    expiry = int(time.time()) + ttl
    sig = hmac.new(secret, f"{agent_id}|{path}|{expiry}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def test_verify_handoff_valid():
    assert sse_daemon._verify_handoff(_mint("ag1"), "ag1", PATH, SECRET) is True


def test_verify_handoff_expired():
    expiry = int(time.time()) - 10
    sig = hmac.new(SECRET, f"ag1|{PATH}|{expiry}".encode(),
                   hashlib.sha256).hexdigest()
    assert sse_daemon._verify_handoff(f"{expiry}.{sig}", "ag1", PATH, SECRET) is False


def test_verify_handoff_tampered_agent():
    # token minted for ag1 must not validate for ag2 (agent comes from URL)
    assert sse_daemon._verify_handoff(_mint("ag1"), "ag2", PATH, SECRET) is False


def test_verify_handoff_garbage():
    assert sse_daemon._verify_handoff("", "ag1", PATH, SECRET) is False
    assert sse_daemon._verify_handoff("nodot", "ag1", PATH, SECRET) is False
    assert sse_daemon._verify_handoff("abc.def", "ag1", PATH, SECRET) is False


async def _read_frame(resp):
    # read until a blank-line-terminated SSE frame (data: ...\n\n)
    buf = b""
    while b"\n\n" not in buf:
        chunk = await asyncio.wait_for(resp.content.read(64), timeout=1.0)
        if not chunk:
            break
        buf += chunk
    return buf.decode()


async def test_stream_initial_broadcast_and_disconnect_cleanup():
    from aiohttp.test_utils import TestClient, TestServer

    st = provider_state._ProviderSampleStore()
    snap = {"state": "sleeping", "model": None}
    app = sse_daemon._build_app(
        secret=SECRET, snapshot_fn=lambda aid: snap,
        lifetime_s=5.0, keepalive_s=0.2,
        is_shutting_down=lambda: False, store=st,
    )
    async with TestClient(TestServer(app)) as client:
        token = _mint("ag1")
        resp = await client.get(f"{PATH}?agent=ag1&token={token}")
        assert resp.status == 200
        first = await _read_frame(resp)
        assert '"state": "sleeping"' in first
        assert sse_daemon.active_count() == 1
        # a broadcast reaches the open stream
        st.broadcast_if_changed("llama", "ag1", {"state": "awake"}, ("state",))
        nxt = await _read_frame(resp)
        assert '"state": "awake"' in nxt
        resp.close()  # client disconnect
    # after disconnect the handler's finally must release the subscription
    for _ in range(50):
        if sse_daemon.active_count() == 0:
            break
        await asyncio.sleep(0.02)
    assert sse_daemon.active_count() == 0


async def test_stream_rejects_bad_token():
    from aiohttp.test_utils import TestClient, TestServer

    st = provider_state._ProviderSampleStore()
    app = sse_daemon._build_app(
        secret=SECRET, snapshot_fn=lambda aid: {"state": "x"},
        lifetime_s=5.0, keepalive_s=0.2,
        is_shutting_down=lambda: False, store=st,
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{PATH}?agent=ag1&token=bogus.sig")
        assert resp.status == 401
