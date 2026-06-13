"""LLM Systems Manager → Alarm Engine adapter.

Forwards every numeric metric collected by llm-systems-manager to the
alarm engine's /api/alarm/metrics/batch endpoint, where it is dual-written
to the in-memory cache (default 1-hour TTL) and to InfluxDB.

Design goals:
- Sync API so it can be invoked from the manager's Flask thread without
  spinning up an event loop.
- Mechanical recursive flatten of the metric dict — every numeric leaf
  is forwarded automatically, so adding a new metric to the manager
  doesn't require updating a hardcoded forwarding list here.
- A small alias table preserves the established names that existing
  alarm rules target (e.g. cpu/usage_percent, gpu/temp_c) and applies
  unit transforms (bytes/s → Mbps).

The flatten/alias logic itself lives in :mod:`backend.integration.metric_flatten`
so the new HTTP /api/alarm/metrics/ingest endpoint can reuse it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .metric_flatten import flatten as _flatten, resolve as _resolve
from config.unified_config import settings


def _ingest_headers() -> dict:
    """Content-Type + the shared ingest bearer when ingest auth is configured."""
    headers = {"Content-Type": "application/json"}
    tok = (settings.alarm_engine.ingest_token or "").strip()
    if tok and tok != "REPLACE_ME":
        headers["Authorization"] = f"Bearer {tok}"
    return headers

logger = logging.getLogger(__name__)


class LLMSystemsManagerAdapter:
    """Sync HTTP forwarder from llm-systems-manager to the alarm engine."""

    # How long to wait before retrying a disabled adapter (seconds).
    _RETRY_INTERVAL: float = 60.0

    def __init__(
        self,
        alarm_engine_url: str = "http://localhost:8081",
        enabled: bool = True,
        timeout: float = 5.0,
    ) -> None:
        self.alarm_engine_url = alarm_engine_url.rstrip("/")
        self.enabled = enabled
        self.timeout = timeout
        self._session = requests.Session()
        self._disabled_at: float = 0.0  # monotonic time when last disabled

    def health_check(self) -> bool:
        """Probe /health. Returns True if reachable; does NOT disable the adapter."""
        try:
            resp = self._session.get(
                f"{self.alarm_engine_url}/health",
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                logger.info(
                    "Alarm engine reachable at %s — adapter active",
                    self.alarm_engine_url,
                )
                self.enabled = True
                return True
            logger.warning(
                "Alarm engine /health returned %d",
                resp.status_code,
            )
        except requests.RequestException as exc:
            logger.warning(
                "Alarm engine unreachable at %s: %s",
                self.alarm_engine_url,
                exc,
            )
        return False

    def _maybe_reenable(self) -> bool:
        """Re-probe the alarm engine if enough time has passed since last failure."""
        if time.monotonic() - self._disabled_at >= self._RETRY_INTERVAL:
            if self.health_check():
                self.enabled = True
                return True
            self._disabled_at = time.monotonic()
        return False

    def forward(self, metric: dict[str, Any]) -> None:
        """Flatten the metric dict and POST every numeric leaf as a batch."""
        if not self.enabled:
            self._maybe_reenable()
        if not self.enabled:
            return

        ts = metric.get("ts")
        if not ts:
            ts = datetime.now(timezone.utc).isoformat()

        # Hostname identifies which server/device produced the metric — required
        # so alerts and metric dropdowns can show source context. Falls back to
        # "platform" (e.g. "linux") if no explicit host was supplied.
        hostname = metric.get("host") or metric.get("platform")

        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for path, value in _flatten(metric):
            source, metric_name, val, unit = _resolve(path, value)
            point: dict[str, Any] = {
                "source": source,
                "metric_name": metric_name,
                "value": val,
                "timestamp": ts,
            }
            if unit:
                point["unit"] = unit
            if hostname:
                point["hostname"] = hostname
            seen[(source, metric_name)] = point  # last write wins on collisions

        if not seen:
            return

        try:
            self._session.post(
                f"{self.alarm_engine_url}/api/alarm/metrics/batch",
                json={"metrics": list(seen.values())},
                timeout=self.timeout,
                headers=_ingest_headers(),
            )
        except requests.RequestException as exc:
            logger.debug("Alarm engine forward failed (will retry in %ds): %s", self._RETRY_INTERVAL, exc)
            self.enabled = False
            self._disabled_at = time.monotonic()
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected error forwarding metrics to alarm engine")

    def forward_async(self, metric: dict[str, Any]) -> None:
        """Fire-and-forget variant — returns immediately, sends in a daemon thread."""
        threading.Thread(
            target=self.forward,
            args=(metric,),
            daemon=True,
        ).start()
