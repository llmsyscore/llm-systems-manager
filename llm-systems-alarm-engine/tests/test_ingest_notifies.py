"""Ingested external alerts reach the notification dispatcher like rule-fired ones."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from backend.api.routes import ingest


class _Req:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()
        self.headers = {"content-type": "application/json"}

    async def body(self) -> bytes:
        return self._raw


def _run(payload: dict, alert):
    sent = []
    mgr = SimpleNamespace(process_alert=lambda ac: alert)
    ingest.set_notification_dispatcher(SimpleNamespace(send_notifications=sent.append))
    try:
        out = asyncio.run(ingest.ingest_external_alert(_Req(payload), alert_mgr=mgr, _auth=None))
    finally:
        ingest.set_notification_dispatcher(None)
    return out, sent


def test_created_alert_is_dispatched():
    alert = SimpleNamespace(alert_id=uuid4())
    out, sent = _run({"name": "Overnight autotune: 1 tuned", "message": "line", "severity": "info",
                      "source": "autotune", "metric": "autotune/batch/abc"}, alert)
    assert out["created"] == 1 and sent == [alert]


def test_deduplicated_alert_is_not_dispatched():
    out, sent = _run({"name": "x", "message": "y", "severity": "info"}, None)
    assert out["deduplicated"] == 1 and sent == []


def test_dispatch_failure_does_not_fail_the_ingest():
    alert = SimpleNamespace(alert_id=uuid4())
    mgr = SimpleNamespace(process_alert=lambda ac: alert)

    def boom(_a):
        raise RuntimeError("no loop")
    ingest.set_notification_dispatcher(SimpleNamespace(send_notifications=boom))
    try:
        out = asyncio.run(ingest.ingest_external_alert(_Req({"name": "x", "message": "y"}), alert_mgr=mgr, _auth=None))
    finally:
        ingest.set_notification_dispatcher(None)
    assert out["created"] == 1
