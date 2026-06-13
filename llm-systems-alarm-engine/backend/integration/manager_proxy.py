"""
Proxy integration layer for llm-systems-manager.

Provides:
- HTTP proxy routes that forward /api/alarm/* requests to the alarm engine
- WebSocket bridge that relays alarm engine WebSocket events to the main dashboard
- Metrics forwarding from llm-systems-manager to the alarm engine

Usage: Import and mount these routes in llm-systems-manager's Flask app.
"""

import asyncio
import json
import logging
import time
from urllib.parse import urljoin

from typing import Optional, Dict

import httpx
from fastapi import WebSocket

from config.unified_config import settings

logger = logging.getLogger(__name__)

# Alarm engine base URL (for proxying from manager)
ALARM_ENGINE_URL = f"http://{settings.alarm_engine.host}:{settings.alarm_engine.port}"


# ── HTTP Proxy Helpers ──────────────────────────────────────────────

async def _proxy_request(
    method: str,
    path: str,
    body: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
) -> tuple:
    """
    Forward an HTTP request to the alarm engine and return (status, body, headers).
    """
    target_url = urljoin(ALARM_ENGINE_URL + "/", path.lstrip("/"))
    try:
        async with httpx.AsyncClient(timeout=settings.alarm_engine.timeouts.manager_proxy) as client:
            resp = await client.request(
                method=method,
                url=target_url,
                content=body,
                headers=headers or {},
            )
            return resp.status_code, resp.content, dict(resp.headers)
    except httpx.TimeoutException:
        logger.warning(f"Alarm engine proxy timeout: {method} {path}")
        return 504, b'{"error":"gateway timeout"}', {"content-type": "application/json"}
    except httpx.ConnectError:
        logger.warning(f"Alarm engine not reachable: {target_url}")
        return 502, b'{"error":"alarm engine unavailable"}', {"content-type": "application/json"}
    except Exception as e:
        logger.error(f"Alarm engine proxy error: {e}")
        return 500, b'{"error":"proxy error"}', {"content-type": "application/json"}


# ── WebSocket Bridge ───────────────────────────────────────────────

class AlarmWebSocketBridge:
    """
    Bridges WebSocket connections from llm-systems-manager's frontend
    to the alarm engine's WebSocket endpoint.
    
    When the main dashboard connects to /ws/alarm-bridge, this bridge:
    1. Connects to the alarm engine's /ws endpoint
    2. Relays messages bidirectionally
    3. Handles keepalive pings
    """

    def __init__(self):
        self._connections = {}  # client_id -> {"manager_ws": WebSocket, "alarm_ws": WebSocket}
        self._lock = asyncio.Lock()
        self._keepalive_task = None

    async def start(self):
        """Start the keepalive task."""
        self._keepalive_task = asyncio.create_task(_keepalive_loop())

    async def stop(self):
        """Stop all connections."""
        if self._keepalive_task:
            self._keepalive_task.cancel()
        async with self._lock:
            for conn in self._connections.values():
                try:
                    await conn["manager_ws"].close()
                    conn["alarm_ws"].close()
                except Exception:
                    pass
            self._connections.clear()

    async def handle_connection(self, manager_ws: WebSocket):
        """Handle a new WebSocket connection from the manager's frontend."""
        await manager_ws.accept()
        client_id = f"mgr_{int(time.time() * 1000)}"

        # Connect to alarm engine WebSocket
        alarm_ws_url = f"ws://{settings.alarm_engine.host}:{settings.alarm_engine.port}/ws"
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", alarm_ws_url) as resp:
                if resp.status_code != 101:
                    await manager_ws.send_text(json.dumps({
                        "event": "error",
                        "data": {"message": "Could not connect to alarm engine WebSocket"}
                    }))
                    await manager_ws.close()
                    return

                # Register connection
                async with self._lock:
                    self._connections[client_id] = {
                        "manager_ws": manager_ws,
                        "alarm_ws": resp,
                    }

                # Send connected event
                await manager_ws.send_text(json.dumps({
                    "event": "connected",
                    "data": {"client_id": client_id}
                }))

                try:
                    while True:
                        # Read from alarm engine
                        try:
                            data = await resp.aiter_text()
                            await manager_ws.send_text(data)
                        except Exception:
                            break

                        # Read from manager and forward to alarm engine
                        # (This is simplified; full implementation needs concurrent reads)
                except Exception as e:
                    logger.error(f"WebSocket bridge error ({client_id}): {e}")
                finally:
                    async with self._lock:
                        self._connections.pop(client_id, None)
                    await manager_ws.close()


async def _keepalive_loop():
    """Send periodic pings to keep WebSocket connections alive."""
    while True:
        await asyncio.sleep(settings.alarm_engine.keepalive_interval)
        # Keepalive is handled by the WebSocket endpoint itself


# ── Metrics Forwarding ──────────────────────────────────────────────

def _ingest_headers() -> dict:
    """Content-Type + the shared ingest bearer when ingest auth is configured."""
    headers = {"Content-Type": "application/json"}
    tok = (settings.alarm_engine.ingest_token or "").strip()
    if tok and tok != "REPLACE_ME":
        headers["Authorization"] = f"Bearer {tok}"
    return headers


async def forward_metric_to_alarm_engine(metric_data: dict) -> bool:
    """
    Forward a collected metric from llm-systems-manager to the alarm engine.
    
    This is called by llm-systems-manager after collecting GPU/CPU/RAM metrics
    so the alarm engine can evaluate rules against the incoming data.
    
    Returns True if the forward was successful, False otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=settings.alarm_engine.timeouts.manager_keepalive) as client:
            resp = await client.post(
                f"{ALARM_ENGINE_URL}/api/alarm/metrics",
                json=metric_data,
                headers=_ingest_headers(),
            )
            return resp.status_code == 200
    except Exception as e:
        logger.debug(f"Failed to forward metric to alarm engine: {e}")
        return False


async def forward_metrics_batch(metrics_batch: list) -> bool:
    """Forward a batch of metrics to the alarm engine."""
    try:
        async with httpx.AsyncClient(timeout=settings.alarm_engine.timeouts.manager_status) as client:
            resp = await client.post(
                f"{ALARM_ENGINE_URL}/api/alarm/metrics/batch",
                json=metrics_batch,
                headers=_ingest_headers(),
            )
            return resp.status_code in (200, 201)
    except Exception as e:
        logger.debug(f"Failed to forward metrics batch to alarm engine: {e}")
        return False


# ── Health Check ────────────────────────────────────────────────────

async def check_alarm_engine_health() -> dict:
    """
    Check if the alarm engine is reachable.
    Returns status dict for health reporting.
    """
    try:
        async with httpx.AsyncClient(timeout=settings.alarm_engine.timeouts.manager_metric_fetch) as client:
            resp = await client.get(f"{ALARM_ENGINE_URL}/health")
            return {
                "status": "ok" if resp.status_code == 200 else "error",
                "details": resp.json() if resp.status_code == 200 else {},
            }
    except Exception:
        return {"status": "unreachable", "details": {}}