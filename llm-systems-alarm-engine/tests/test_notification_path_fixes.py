"""Notification-path fixes: sender status checks (#223), repo cache-miss
fallback (#251), websocket per-send timeout (#254)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from backend._time import now_utc
from backend.engine.notification_dispatcher import NotificationDispatcher
from backend.models.notification import (
    ChannelSpecificConfig,
    ChannelType,
    DiscordConfig,
    NotificationChannel,
    NotificationChannelUpdate,
    WebhookConfig,
)
from backend.storage.cache import MetricCache
from backend.storage.repositories import NotificationRepository


# ── #251: NotificationRepository DB fallback ────────────────────────────────

class _ChannelDB:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def get_channel(self, channel_id: str):
        return self.items.get(channel_id)

    def write_channel(self, ch: dict) -> None:
        self.items[ch["channel_id"]] = ch


def _mk_channel(name: str = "hook") -> NotificationChannel:
    return NotificationChannel(
        channel_id=uuid4(),
        name=name,
        description=None,
        channel_type=ChannelType.WEBHOOK,
        config=ChannelSpecificConfig(webhook=WebhookConfig(url="http://example.test/hook")),
        enabled=True,
        rule_ids=[],
        created_at=now_utc(),
        last_sent_at=None,
        send_count=0,
        fail_count=0,
    )


async def test_get_channel_falls_back_to_db_on_cache_miss():
    cache, db = MetricCache(), _ChannelDB()
    ch = _mk_channel()
    db.items[str(ch.channel_id)] = ch.to_dict()
    repo = NotificationRepository(cache, db)

    got = await repo.get_channel(str(ch.channel_id))
    assert got is not None and got.channel_id == ch.channel_id
    assert cache.get(f"channel:{ch.channel_id}") is not None


async def test_get_channel_missing_everywhere_returns_none():
    repo = NotificationRepository(MetricCache(), _ChannelDB())
    assert await repo.get_channel(str(uuid4())) is None


async def test_update_channel_falls_back_to_db_on_cache_miss():
    cache, db = MetricCache(), _ChannelDB()
    ch = _mk_channel(name="before")
    db.items[str(ch.channel_id)] = ch.to_dict()
    repo = NotificationRepository(cache, db)

    updated = await repo.update_channel(
        str(ch.channel_id), NotificationChannelUpdate(name="after"))
    assert updated is not None and updated.name == "after"
    assert db.items[str(ch.channel_id)]["name"] == "after"
    assert cache.get(f"channel:{ch.channel_id}")["name"] == "after"


async def test_update_channel_missing_everywhere_returns_none():
    repo = NotificationRepository(MetricCache(), _ChannelDB())
    got = await repo.update_channel(str(uuid4()), NotificationChannelUpdate(name="x"))
    assert got is None


def test_get_delivery_by_id_falls_back_to_db_on_cache_miss():
    from backend.models.notification import NotificationDelivery
    did = uuid4()
    row = {
        "delivery_id": str(did), "config_id": None, "channel_id": None,
        "channel_type": "webhook", "method": "channel",
        "title": "t", "body": "b", "severity": "info", "recipient": "r",
        "success": True, "error_message": None,
        "delivered_at": now_utc().isoformat(),
    }
    db = _ChannelDB()
    db.get_delivery = lambda d_id: row if d_id == str(did) else None
    repo = NotificationRepository(MetricCache(), db)
    got = repo.get_delivery_by_id(did)
    assert isinstance(got, NotificationDelivery)
    assert str(got.delivery_id) == str(did)


# ── #223: webhook/Discord senders check HTTP status ─────────────────────────

class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_delivery(self, **kw) -> None:
        self.calls.append(kw)


def _fake_client_cls(status_code: int):
    class _Resp:
        pass
    _Resp.status_code = status_code

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()
    return _Client


def _alert():
    return SimpleNamespace(
        alert_id=uuid4(), rule_id=uuid4(), rule_name="r",
        metric_source="host", metric_name="cpu", current_value=99.0,
        threshold_value=90.0, severity="critical", status="active",
        message="cpu high", created_at=datetime.now(timezone.utc),
        incident_id=None,
    )


def _webhook_channel():
    return _mk_channel()


def _discord_channel():
    ch = _mk_channel(name="disc")
    return ch.model_copy(update={
        "channel_type": ChannelType.DISCORD,
        "config": ChannelSpecificConfig(
            discord=DiscordConfig(webhook_url="http://example.test/dc")),
    })


async def _send(channel_kind: str, status_code: int, monkeypatch):
    import backend.engine.notification_dispatcher as nd
    monkeypatch.setattr(nd.httpx, "AsyncClient", _fake_client_cls(status_code))
    rec = _Recorder()
    d = NotificationDispatcher(notification_repository=rec)
    alert = _alert()
    if channel_kind == "webhook":
        await d._send_webhook_channels(alert, [_webhook_channel()])
    else:
        await d._send_discord_channels(alert, [_discord_channel()])
    return d, rec, alert


async def test_webhook_2xx_is_success(monkeypatch):
    d, rec, alert = await _send("webhook", 204, monkeypatch)
    assert rec.calls[0]["success"] is True
    assert str(alert.alert_id) in d._nontoast_send_ok


async def test_webhook_5xx_is_failure(monkeypatch):
    d, rec, alert = await _send("webhook", 500, monkeypatch)
    assert rec.calls[0]["success"] is False
    assert "500" in (rec.calls[0]["error_message"] or "")
    assert str(alert.alert_id) not in d._nontoast_send_ok


async def test_discord_2xx_is_success(monkeypatch):
    d, rec, alert = await _send("discord", 200, monkeypatch)
    assert rec.calls[0]["success"] is True
    assert str(alert.alert_id) in d._nontoast_send_ok


async def test_discord_4xx_is_failure(monkeypatch):
    d, rec, alert = await _send("discord", 404, monkeypatch)
    assert rec.calls[0]["success"] is False
    assert "404" in (rec.calls[0]["error_message"] or "")
    assert str(alert.alert_id) not in d._nontoast_send_ok


# ── #254: websocket per-send timeout ────────────────────────────────────────

class _StallWS:
    def __init__(self) -> None:
        self.closed = False

    async def send_text(self, _t: str) -> None:
        await asyncio.Event().wait()

    async def close(self, *a, **k) -> None:
        self.closed = True


class _OkWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, t: str) -> None:
        self.sent.append(t)

    async def close(self, *a, **k) -> None:
        pass


async def test_stalled_broadcast_client_disconnected_others_still_served():
    from backend.api.websocket import WebSocketConnectionManager
    m = WebSocketConnectionManager()
    m._send_timeout = 0.05
    ok, stall = _OkWS(), _StallWS()
    m._connections["stall"] = stall
    m._connections["ok"] = ok
    m.subscribe("stall", "alerts")
    m.subscribe("ok", "alerts")
    task = asyncio.create_task(m._process_queue())
    try:
        await m.broadcast("alerts", {"n": 1})
        await asyncio.wait_for(m._message_queue.join(), timeout=2)
    finally:
        task.cancel()
    assert "stall" not in m._connections
    assert stall.closed is True
    assert len(ok.sent) == 1
    assert m._send_failures == 1


async def test_stalled_targeted_client_disconnected():
    from backend.api.websocket import WebSocketConnectionManager
    m = WebSocketConnectionManager()
    m._send_timeout = 0.05
    stall = _StallWS()
    m._connections["stall"] = stall
    task = asyncio.create_task(m._process_queue())
    try:
        await m.broadcast_to_client("stall", "alerts", {"n": 1})
        await asyncio.wait_for(m._message_queue.join(), timeout=2)
    finally:
        task.cancel()
    assert "stall" not in m._connections
    assert stall.closed is True
