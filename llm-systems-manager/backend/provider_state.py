"""Per-agent provider sample store.

Replaces 11 singleton globals (`_remote_host_metric`, `_lmstudio_metrics`,
`_llama_state_subscribers`, etc.) with one process-wide `STORE` that holds
per-(provider, agent_id) telemetry, SSE subscribers, online-edge latches,
and SSE-debounce fingerprints. Adding a future provider (vLLM, Ollama)
needs no new globals — just `STORE.put("vllm", agent_id, sample)`.

Storage only — does NOT know about agents.json or which agent is the
default for a provider. Policy lookups (default agent picking) live in
`agent_registry.default_agent_id_for(provider)`.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time

from _best_effort import best_effort  # type: ignore[import-not-found]  # sibling


def _async_put(q: "asyncio.Queue", item) -> None:
    """Put on an asyncio.Queue from its own loop thread; drop-oldest on full so
    the latest idempotent state always wins. MUST run via call_soon_threadsafe."""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass


class _ProviderSampleStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # samples[provider][agent_id] = {"sample": dict, "last_seen": float}
        self._samples: dict[str, dict[str, dict]] = {}
        # online_latch[provider][agent_id] = bool — last known "online" edge
        self._online_latch: dict[str, dict[str, bool]] = {}
        # subscribers[provider][agent_id] = list[queue.Queue]
        self._subscribers: dict[str, dict[str, list[queue.Queue]]] = {}
        # last_published[provider][agent_id] = dict — most recent broadcast
        # payload, used by broadcast_if_changed to debounce on fingerprint
        self._last_published: dict[str, dict[str, dict]] = {}
        # async_subscribers[provider][agent_id] = list[(asyncio loop, asyncio.Queue)]
        # fed via loop.call_soon_threadsafe from producer threads.
        self._async_subscribers: dict[str, dict[str, list[tuple]]] = {}

    # ── sample I/O ──────────────────────────────────────────────────
    def put(self, provider: str, agent_id: str, sample: dict) -> None:
        with self._lock:
            self._samples.setdefault(provider, {})[agent_id] = {
                "sample": sample,
                "last_seen": time.time(),
            }

    def get(self, provider: str, agent_id: str) -> dict | None:
        with self._lock:
            wrap = self._samples.get(provider, {}).get(agent_id)
            return dict(wrap) if wrap else None

    def all_for(self, provider: str) -> dict[str, dict]:
        with self._lock:
            return {aid: dict(w) for aid, w in self._samples.get(provider, {}).items()}

    def evict(self, agent_id: str) -> None:
        """Wipe every provider entry for this agent — samples, latch,
        subscribers, last_published. Wakes any live SSE subscriber for
        this agent with a None sentinel before dropping it, so generators
        exit promptly instead of yielding keepalives until the browser
        disconnects."""
        waking: list[queue.Queue] = []
        with self._lock:
            for prov in list(self._samples.keys()):
                self._samples[prov].pop(agent_id, None)
            for prov in list(self._online_latch.keys()):
                self._online_latch[prov].pop(agent_id, None)
            for prov in list(self._subscribers.keys()):
                qs = self._subscribers[prov].pop(agent_id, None)
                if qs:
                    waking.extend(qs)
            for prov in list(self._last_published.keys()):
                self._last_published[prov].pop(agent_id, None)
            awaking: list[tuple] = []
            for prov in list(self._async_subscribers.keys()):
                pairs = self._async_subscribers[prov].pop(agent_id, None)
                if pairs:
                    awaking.extend(pairs)
        for q in waking:
            with best_effort("evict: wake sync SSE subscriber"):
                q.put_nowait(None)
        for loop, q in awaking:
            try:
                loop.call_soon_threadsafe(_async_put, q, None)
            except RuntimeError:
                pass

    # ── online-edge latch (per-agent log gating) ───────────────────
    def mark_online(self, provider: str, agent_id: str) -> bool:
        """Returns True only on the False→True edge (or first sighting)."""
        with self._lock:
            prev = self._online_latch.setdefault(provider, {}).get(agent_id, False)
            self._online_latch[provider][agent_id] = True
            return not prev

    def mark_offline(self, provider: str, agent_id: str) -> bool:
        """Returns True only on the True→False edge."""
        with self._lock:
            prev = self._online_latch.setdefault(provider, {}).get(agent_id, False)
            self._online_latch[provider][agent_id] = False
            return prev

    # ── SSE fan-out ────────────────────────────────────────────────
    def subscribe(self, provider: str, agent_id: str, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.setdefault(provider, {}).setdefault(agent_id, []).append(q)

    def unsubscribe(self, provider: str, agent_id: str, q: queue.Queue) -> None:
        with self._lock:
            qs = self._subscribers.get(provider, {}).get(agent_id)
            if qs is None:
                return
            try:
                qs.remove(q)
            except ValueError:
                pass

    def subscribe_async(self, provider: str, agent_id: str,
                        loop: "asyncio.AbstractEventLoop") -> "asyncio.Queue":
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        with self._lock:
            self._async_subscribers.setdefault(provider, {}).setdefault(
                agent_id, []).append((loop, q))
        return q

    def unsubscribe_async(self, provider: str, agent_id: str,
                          q: "asyncio.Queue") -> None:
        with self._lock:
            subs = self._async_subscribers.get(provider, {}).get(agent_id)
            if not subs:
                return
            self._async_subscribers[provider][agent_id] = [
                (lp, qq) for (lp, qq) in subs if qq is not q
            ]

    def total_subscriber_count(self) -> int:
        """Total open SSE subscribers across every (provider, agent_id)."""
        with self._lock:
            return sum(len(qs)
                       for prov in self._subscribers.values()
                       for qs in prov.values())

    def wake_all(self, sentinel: object = None) -> None:
        """Push `sentinel` onto every subscriber queue across every
        (provider, agent_id). Used by shutdown to break SSE generators
        out of their q.get(timeout=…) loops."""
        with self._lock:
            queues = [q for prov in self._subscribers.values()
                        for qs in prov.values()
                        for q in qs]
        for q in queues:
            with best_effort("wake_all: wake sync SSE subscriber"):
                q.put_nowait(sentinel)
        with self._lock:
            asubs = [(lp, q) for prov in self._async_subscribers.values()
                     for qs in prov.values() for (lp, q) in qs]
        for loop, q in asubs:
            try:
                loop.call_soon_threadsafe(_async_put, q, sentinel)
            except RuntimeError:
                pass

    def broadcast_if_changed(self, provider: str, agent_id: str,
                             payload: dict,
                             fingerprint_keys: tuple[str, ...]) -> None:
        """Compare payload against the last broadcast for (provider, agent_id).
        Fan out only on diff in fingerprint_keys. Drops dead queues."""
        with self._lock:
            last = self._last_published.setdefault(provider, {}).get(agent_id) or {}
            fp = tuple(payload.get(k) for k in fingerprint_keys)
            last_fp = tuple(last.get(k) for k in fingerprint_keys)
            if fp == last_fp:
                return
            self._last_published[provider][agent_id] = dict(payload)
            qs = list(self._subscribers.get(provider, {}).get(agent_id, []))
        msg = f"data: {json.dumps(payload)}\n\n"
        dead = []
        for q in qs:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        if dead:
            with self._lock:
                live = self._subscribers.get(provider, {}).get(agent_id, [])
                for q in dead:
                    try:
                        live.remove(q)
                    except ValueError:
                        pass
        with self._lock:
            asubs = list(self._async_subscribers.get(provider, {}).get(agent_id, []))
        for loop, q in asubs:
            try:
                loop.call_soon_threadsafe(_async_put, q, msg)
            except RuntimeError:
                pass  # loop closed (shutdown)


STORE = _ProviderSampleStore()
