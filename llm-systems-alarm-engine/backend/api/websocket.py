"""
WebSocket handler for live dashboard updates.

Provides real-time streaming of:
- Active alert count changes
- New alert notifications
- Rule evaluation results
- Metric value updates
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from config.unified_config import settings

logger = logging.getLogger(__name__)


class WebSocketConnectionManager:
    """Manages WebSocket connections and broadcasts events to subscribers."""

    def __init__(self) -> None:
        self._connections: Dict[str, WebSocket] = {}  # client_id -> websocket
        self._subscribers: Dict[str, Set[str]] = {}  # event_type -> set of client_ids
        self._active_clients: Set[str] = set()
        self._message_queue: asyncio.Queue = asyncio.Queue(
            maxsize=settings.alarm_engine.caches.websocket_queue_size,
        )
        self._broadcast_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._started = False
        self._broadcasts_total = 0   # cumulative messages enqueued
        self._sends_total = 0        # cumulative successful send_text calls
        self._send_failures = 0      # cumulative send failures (force disconnects)

    async def start(self) -> None:
        """Start the background broadcast processor."""
        if self._started:
            return
        self._broadcast_task = asyncio.create_task(self._process_queue())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._started = True
        logger.info("WebSocket broadcast processor + heartbeat started")

    async def _heartbeat_loop(self) -> None:
        """One INFO line per interval with current connection + traffic stats."""
        last_b = last_s = last_f = 0
        while True:
            try:
                await asyncio.sleep(settings.alarm_engine.intervals.websocket_heartbeat_log_s)
                db_ = self._broadcasts_total - last_b
                ds_ = self._sends_total - last_s
                df_ = self._send_failures - last_f
                last_b, last_s, last_f = self._broadcasts_total, self._sends_total, self._send_failures
                logger.info(
                    "heartbeat ws: clients=%d subs=%d broadcasts+%d sends+%d send_fail+%d "
                    "(totals b=%d s=%d f=%d) qsize=%d",
                    len(self._connections),
                    sum(len(s) for s in self._subscribers.values()),
                    db_, ds_, df_,
                    self._broadcasts_total, self._sends_total, self._send_failures,
                    self._message_queue.qsize(),
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("ws heartbeat tick failed: %s", e, exc_info=True)

    async def stop(self) -> None:
        """Stop the broadcast processor and close all connections."""
        if self._broadcast_task:
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._started = False

        for client_id in list(self._connections.keys()):
            await self.disconnect(client_id)

    async def connect(self, websocket: WebSocket, client_id: Optional[str] = None) -> str:
        """Accept a WebSocket connection and assign a client ID."""
        await websocket.accept()
        cid = client_id or str(uuid.uuid4())
        self._connections[cid] = websocket
        self._active_clients.add(cid)
        logger.info(f"WebSocket connected: {cid}")
        return cid

    async def disconnect(self, client_id: str) -> None:
        """Remove a WebSocket connection."""
        self._active_clients.discard(client_id)
        # Unsubscribe from all event types
        for event_type in list(self._subscribers.keys()):
            self._subscribers[event_type].discard(client_id)
            if not self._subscribers[event_type]:
                del self._subscribers[event_type]
        self._connections.pop(client_id, None)
        logger.info(f"WebSocket disconnected: {client_id}")

    def subscribe(self, client_id: str, event_type: str) -> None:
        """Subscribe a client to an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = set()
        self._subscribers[event_type].add(client_id)

    def unsubscribe(self, client_id: str, event_type: str) -> None:
        """Unsubscribe a client from an event type."""
        if event_type in self._subscribers:
            self._subscribers[event_type].discard(client_id)
            if not self._subscribers[event_type]:
                del self._subscribers[event_type]

    def subscribe_all(self, client_id: str) -> None:
        """Subscribe a client to all event types."""
        for event_type in ["alerts", "rules", "metrics", "system"]:
            self.subscribe(client_id, event_type)

    def unsubscribe_all(self, client_id: str) -> None:
        """Unsubscribe a client from all event types."""
        for event_type in list(self._subscribers.keys()):
            self.unsubscribe(client_id, event_type)

    async def broadcast(self, event_type: str, data: Dict[str, Any]) -> None:
        """Broadcast an event to all subscribers of an event type."""
        self._broadcasts_total += 1
        await self._message_queue.put({
            "event_type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def broadcast_to_client(self, client_id: str, event_type: str, data: Dict[str, Any]) -> None:
        """Send a targeted event to a specific client."""
        self._broadcasts_total += 1
        await self._message_queue.put({
            "event_type": event_type,
            "data": data,
            "target_client": client_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def _process_queue(self) -> None:
        """Background task to process the message queue and broadcast."""
        while True:
            try:
                msg = await self._message_queue.get()
            except asyncio.CancelledError:
                break

            event_type = msg.get("event_type", "unknown")
            target = msg.get("target_client")
            payload = {
                "event": event_type,
                "data": msg.get("data"),
                "ts": msg.get("timestamp"),
            }
            serialized = json.dumps(payload)

            if target:
                # Send to specific client
                ws = self._connections.get(target)
                if ws:
                    try:
                        await ws.send_text(serialized)
                        self._sends_total += 1
                    except Exception as e:
                        self._send_failures += 1
                        logger.warning("ws targeted send failed for %s: %s", target, e)
                        await self.disconnect(target)
            else:
                # Broadcast to subscribers
                subscribers = self._subscribers.get(event_type, set())
                if not subscribers:
                    # Fallback: broadcast to all connected clients
                    subscribers = set(self._connections.keys())

                for cid in list(subscribers):
                    ws = self._connections.get(cid)
                    if ws:
                        try:
                            await ws.send_text(serialized)
                            self._sends_total += 1
                        except Exception as e:
                            self._send_failures += 1
                            logger.warning("ws broadcast send failed for %s: %s", cid, e)
                            await self.disconnect(cid)

            self._message_queue.task_done()


# ── WebSocket endpoint ──────────────────────────────────────────

_manager: Optional[WebSocketConnectionManager] = None


def set_manager(manager: WebSocketConnectionManager) -> None:
    global _manager
    _manager = manager


async def websocket_endpoint(websocket: WebSocket) -> None:
    """Main WebSocket endpoint handler."""
    if _manager is None:
        await websocket.close(code=4001, reason="WebSocket service not initialized")
        return

    client_id = await _manager.connect(websocket)

    # Subscribe to all events by default
    _manager.subscribe_all(client_id)

    try:
        while True:
            # Listen for client messages (subscribe/unsubscribe commands)
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "event": "error",
                    "data": {"message": "Invalid JSON"},
                    "ts": datetime.now(timezone.utc).isoformat(),
                }))
                continue

            action = msg.get("action", "")
            event_type = msg.get("event_type", "")

            if action == "subscribe":
                if event_type:
                    _manager.subscribe(client_id, event_type)
                else:
                    _manager.subscribe_all(client_id)
            elif action == "unsubscribe":
                if event_type:
                    _manager.unsubscribe(client_id, event_type)
                else:
                    _manager.unsubscribe_all(client_id)
            elif action == "ping":
                await websocket.send_text(json.dumps({
                    "event": "pong",
                    "data": {"ts": datetime.now(timezone.utc).isoformat()},
                    "ts": datetime.now(timezone.utc).isoformat(),
                }))
            else:
                await websocket.send_text(json.dumps({
                    "event": "error",
                    "data": {"message": f"Unknown action: {action}"},
                    "ts": datetime.now(timezone.utc).isoformat(),
                }))

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {client_id}")
    except Exception as e:
        logger.error("WebSocket error for %s: %s", client_id, e, exc_info=True)
    finally:
        await _manager.disconnect(client_id)


def get_manager() -> WebSocketConnectionManager:
    if _manager is None:
        raise RuntimeError("WebSocketConnectionManager not initialized.")
    return _manager


def init_manager() -> WebSocketConnectionManager:
    """Create and return a new WebSocketConnectionManager."""
    global _manager
    _manager = WebSocketConnectionManager()
    return _manager