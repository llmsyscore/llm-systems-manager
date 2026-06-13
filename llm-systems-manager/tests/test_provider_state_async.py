"""Phase 1 (#110): asyncio-queue subscription on the per-agent STORE."""
from __future__ import annotations

import asyncio

import provider_state


async def test_broadcast_delivers_to_async_subscriber():
    st = provider_state._ProviderSampleStore()
    loop = asyncio.get_running_loop()
    q = st.subscribe_async("llama", "ag1", loop)
    # broadcast runs on THIS thread but call_soon_threadsafe schedules on loop;
    # awaiting q.get() lets the scheduled callback run.
    st.broadcast_if_changed("llama", "ag1", {"state": "awake"}, ("state",))
    msg = await asyncio.wait_for(q.get(), timeout=1.0)
    assert msg == 'data: {"state": "awake"}\n\n'


async def test_unsubscribe_stops_delivery():
    st = provider_state._ProviderSampleStore()
    loop = asyncio.get_running_loop()
    q = st.subscribe_async("llama", "ag1", loop)
    st.unsubscribe_async("llama", "ag1", q)
    st.broadcast_if_changed("llama", "ag1", {"state": "awake"}, ("state",))
    with pytest_raises_timeout():
        await asyncio.wait_for(q.get(), timeout=0.2)


async def test_drop_oldest_keeps_latest_under_full_queue():
    st = provider_state._ProviderSampleStore()
    loop = asyncio.get_running_loop()
    q = st.subscribe_async("llama", "ag1", loop)
    # Push more than maxsize(8) distinct states without draining; newest wins.
    for i in range(20):
        st.broadcast_if_changed("llama", "ag1", {"state": f"s{i}"}, ("state",))
    await asyncio.sleep(0.05)  # let scheduled puts run
    last = None
    while not q.empty():
        last = q.get_nowait()
    assert last == 'data: {"state": "s19"}\n\n'


async def test_wake_all_pushes_sentinel_to_async_subscriber():
    st = provider_state._ProviderSampleStore()
    loop = asyncio.get_running_loop()
    q = st.subscribe_async("llama", "ag1", loop)
    st.wake_all(None)
    msg = await asyncio.wait_for(q.get(), timeout=1.0)
    assert msg is None


import contextlib

@contextlib.contextmanager
def pytest_raises_timeout():
    try:
        yield
        raise AssertionError("expected TimeoutError")
    except asyncio.TimeoutError:
        pass
